"""Recipe sourcing: web search -> fetch -> schema.org extraction. No model tokens.

The planner agent decides *what* to look for (queries, sites); this module
does the fetching cheaply and returns structured recipes.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Protocol
from urllib.parse import urlparse

import httpx
from recipe_scrapers import scrape_html

from .store import Recipe

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (compatible; grocery-agent/0.1; +https://github.com/gpratyush/grocery-agent)"
MAX_PAGE_BYTES = 3_000_000
NON_RECIPE_PATH = re.compile(r"/(tag|category|categories|collections?|search|author|page)/", re.I)


class SearchProvider(Protocol):
    def search(self, query: str, max_results: int) -> list[str]: ...


class DDGSearch:
    """DuckDuckGo via the ``ddgs`` package. No API key; best-effort."""

    def search(self, query: str, max_results: int) -> list[str]:
        from ddgs import DDGS

        try:
            return [r["href"] for r in DDGS().text(query, max_results=max_results) if r.get("href")]
        except Exception as exc:  # network errors, rate limits
            log.warning("search failed for %r: %s", query, exc)
            return []


class BraveSearch:
    def __init__(self, api_key: str | None = None):
        self.api_key = api_key or os.environ.get("BRAVE_API_KEY", "")

    def search(self, query: str, max_results: int) -> list[str]:
        resp = httpx.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(max_results, 20)},
            headers={"X-Subscription-Token": self.api_key, "Accept": "application/json"},
            timeout=20,
        )
        resp.raise_for_status()
        return [r["url"] for r in resp.json().get("web", {}).get("results", [])]


def make_search(provider: str) -> SearchProvider:
    if provider == "brave":
        return BraveSearch()
    return DDGSearch()


def site_query(query: str, sites: list[str]) -> str:
    if not sites:
        return f"{query} recipe"
    return f"{query} recipe " + " OR ".join(f"site:{s}" for s in sites)


class Fetcher(Protocol):
    def get(self, url: str) -> str | None: ...


class HttpFetcher:
    def __init__(self, timeout: float = 15):
        self.client = httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True)

    def get(self, url: str) -> str | None:
        try:
            resp = self.client.get(url)
            if resp.status_code != 200 or "html" not in resp.headers.get("content-type", "html"):
                return None
            return resp.text[:MAX_PAGE_BYTES]
        except httpx.HTTPError as exc:
            log.info("fetch failed %s: %s", url, exc)
            return None


_NUMBER = re.compile(r"[\d.]+")


def _num(value) -> float | None:
    if value is None:
        return None
    m = _NUMBER.search(str(value))
    return float(m.group()) if m else None


def parse_nutrition(nutrients: dict) -> dict[str, float]:
    keys = {"calories": "calories", "proteinContent": "protein_g", "carbohydrateContent": "carbs_g",
            "fatContent": "fat_g"}
    out = {}
    for src, dst in keys.items():
        v = _num(nutrients.get(src))
        if v is not None:
            out[dst] = v
    return out


def _safe(fn, default=None):
    try:
        v = fn()
        return v if v not in ("", None) else default
    except Exception:
        return default


def extract_recipe(html: str, url: str, cuisine_hint: str = "", query: str = "") -> Recipe | None:
    """Structured extraction via recipe-scrapers (site scrapers + schema.org)."""
    try:
        s = scrape_html(html, org_url=url, supported_only=False)
    except Exception:
        return None
    ingredients = _safe(s.ingredients, [])
    title = _safe(s.title)
    if not title or len(ingredients) < 3:
        return None
    cuisine = _safe(s.cuisine, "") or cuisine_hint
    cuisine = cuisine.split(",")[0].strip().lower() if cuisine else cuisine_hint
    tags = [t.strip().lower() for t in str(_safe(s.category, "")).split(",") if t.strip()]
    return Recipe(
        url=url,
        title=title.strip(),
        ingredients=ingredients,
        cuisine=cuisine,
        servings=_num(_safe(s.yields)),
        total_time=int(_safe(s.total_time, 0) or 0) or None,
        nutrition=parse_nutrition(_safe(s.nutrients, {}) or {}),
        tags=tags,
        source_query=query,
    )


def looks_like_recipe_url(url: str) -> bool:
    p = urlparse(url)
    return p.scheme in ("http", "https") and not NON_RECIPE_PATH.search(p.path) and p.path not in ("", "/")


def source(queries: list[str], sites: list[str], cuisine: str, *, search: SearchProvider, fetcher: Fetcher,
           max_pages: int, known_url, results_per_query: int = 8) -> list[Recipe]:
    """Run queries, fetch up to ``max_pages`` new pages, return extracted recipes."""
    urls: list[str] = []
    for q in queries:
        for u in search.search(site_query(q, sites), results_per_query):
            if u not in urls and looks_like_recipe_url(u) and not known_url(u):
                urls.append(u)
    found: list[Recipe] = []
    for u in urls[:max_pages]:
        html = fetcher.get(u)
        if not html:
            continue
        r = extract_recipe(html, u, cuisine_hint=cuisine, query="; ".join(queries))
        if r:
            found.append(r)
    return found

"""Store prices from the Kroger public API (developer.kroger.com), for one chosen store.

Each canonical ingredient is searched once per store and the answer, including
"no good match", is cached in the store for a week. Anything without a match
keeps its estimated price, so a plan always has a full cost.

Keys: KROGER_CLIENT_ID and KROGER_CLIENT_SECRET in ~/.grocery-agent/.env.
"""

from __future__ import annotations

import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import httpx

from .store import IngredientFacts, Store, StorePrice

log = logging.getLogger(__name__)

API = "https://api.kroger.com/v1"
CACHE_DAYS = 7
SEARCH_LIMIT = 10

G_PER = {"oz": 28.35, "lb": 453.6, "lbs": 453.6, "g": 1.0, "kg": 1000.0}
ML_PER = {"fl oz": 29.57, "gal": 3785.0, "qt": 946.0, "pt": 473.0, "ml": 1.0, "l": 1000.0, "liter": 1000.0,
          "litre": 1000.0}
COUNT_UNITS = {"ct", "count", "each", "ea", "pk", "pack", "bunch", "head"}
_NUM = r"(\d+(?:\.\d+)?|\d+/\d+)"
_SIZE = re.compile(_NUM + r"\s*(fl\.? ?oz|oz|lbs?|kg|g|gal|qt|pt|ml|liter|litre|l|ct|count|each|ea|pk|pack|bunch|head)\b")


class KrogerError(Exception):
    pass


def _num(s: str) -> float:
    if "/" in s:
        a, b = s.split("/")
        return float(a) / float(b)
    return float(s)


def size_grams(size: str, facts: IngredientFacts | None) -> float | None:
    """'16 oz' -> 453.6; '1/2 gal' uses density; '12 ct' uses each_g; '6 ct / 12 fl oz' multiplies."""
    parts = [(_num(n), u.replace(".", "").replace("  ", " ")) for n, u in _SIZE.findall(size.lower())]
    if not parts:
        return None
    multiplier = 1.0
    if len(parts) > 1 and parts[0][1] in COUNT_UNITS:
        multiplier, parts = parts[0][0], parts[1:]
    qty, unit = parts[0]
    unit = "fl oz" if unit.replace(" ", "") == "floz" else unit
    if unit in G_PER:
        grams = qty * G_PER[unit]
    elif unit in ML_PER:
        grams = qty * ML_PER[unit] * ((facts.density if facts else None) or 1.0)
    elif unit in COUNT_UNITS and facts and facts.each_g:
        grams = qty * facts.each_g
    else:
        return None
    return grams * multiplier


def _stem(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def matches(canonical: str, description: str) -> bool:
    """Every word of the ingredient appears in the product name (plurals allowed)."""
    words = {_stem(w) for w in re.findall(r"[a-z]+", description.lower())}
    return all(_stem(w) in words for w in re.findall(r"[a-z]+", canonical.lower()))


def pick(canonical: str, products: list[dict], facts: IngredientFacts | None) -> StorePrice | None:
    """First product in Kroger's relevance order that names the ingredient and has a usable price and size."""
    for p in products:
        name = p.get("description") or ""
        if not matches(canonical, name):
            continue
        for item in p.get("items") or []:
            price = item.get("price") or {}
            regular, promo = price.get("regular") or 0, price.get("promo") or 0
            cost = promo if 0 < promo < regular else regular
            if cost <= 0:
                continue
            if (item.get("soldBy") or "").upper() == "WEIGHT":
                grams = G_PER["lb"]  # priced per pound
            else:
                grams = size_grams(item.get("size") or "", facts)
            if grams:
                return StorePrice(package_g=grams, price=float(cost), product=f"{name} ({item.get('size', '')})".strip())
    return None


class Kroger:
    """Minimal client: client-credentials token, store locator, product search."""

    def __init__(self, client_id: str, client_secret: str, client: httpx.Client | None = None):
        self.auth = (client_id, client_secret)
        self.client = client or httpx.Client(timeout=20)
        self._token: str | None = None
        self._expires = 0.0

    def token(self) -> str:
        if self._token and time.time() < self._expires - 60:
            return self._token
        try:
            r = self.client.post(f"{API}/connect/oauth2/token", auth=self.auth,
                                 data={"grant_type": "client_credentials", "scope": "product.compact"})
            r.raise_for_status()
            body = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise KrogerError(f"couldn't sign in to the Kroger API: {exc}") from exc
        self._token = body["access_token"]
        self._expires = time.time() + float(body.get("expires_in", 1800))
        return self._token

    def _get(self, path: str, params: dict) -> list[dict]:
        try:
            r = self.client.get(f"{API}{path}", params=params, headers={"Authorization": f"Bearer {self.token()}",
                                                                        "Accept": "application/json"})
            r.raise_for_status()
            return r.json().get("data") or []
        except (httpx.HTTPError, ValueError) as exc:
            raise KrogerError(f"Kroger API {path} failed: {exc}") from exc

    def locations(self, zip_code: str, limit: int = 5) -> list[dict]:
        rows = self._get("/locations", {"filter.zipCode.near": zip_code, "filter.limit": limit})
        out = []
        for row in rows:
            a = row.get("address") or {}
            out.append({"id": row["locationId"], "name": row.get("name") or row.get("chain") or "Kroger",
                        "address": ", ".join(x for x in (a.get("addressLine1"), a.get("city"), a.get("state")) if x)})
        return out

    def products(self, term: str, location_id: str, limit: int = SEARCH_LIMIT) -> list[dict]:
        return self._get("/products", {"filter.term": term, "filter.locationId": location_id, "filter.limit": limit})


class KrogerPrices:
    """Price source for the Evaluator: a cached Kroger price per canonical ingredient, or None."""

    source = "kroger"

    def __init__(self, api: Kroger, store: Store, location_id: str, store_name: str = "",
                 cache_days: int = CACHE_DAYS, workers: int = 6):
        self.api = api
        self.store = store
        self.key = f"kroger:{location_id}"
        self.location_id = location_id
        self.store_name = store_name
        self.cache_days = cache_days
        self.workers = workers
        self.failed: str | None = None  # set after an API error; later lookups use estimates

    def _fresh(self, canonical: str) -> tuple[bool, StorePrice | None]:
        row = self.store.store_price(self.key, canonical)
        if row is None:
            return False, None
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.cache_days)
        if datetime.fromisoformat(row[1]) < cutoff:
            return False, None
        return True, row[0]

    def prefetch(self, canonicals: list[str]) -> None:
        """Look up every uncached ingredient, in parallel. Writes happen on the calling thread."""
        if self.failed:
            return
        todo = [c for c in dict.fromkeys(canonicals) if c and len(c) >= 3 and not self._fresh(c)[0]]
        if not todo:
            return

        def fetch(c: str):
            try:
                return c, self.api.products(c, self.location_id), None
            except KrogerError as exc:
                return c, None, exc

        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            results = list(pool.map(fetch, todo))
        for canonical, products, err in results:
            if err is not None:
                if not self.failed:
                    log.warning("%s; using estimated prices", err)
                self.failed = str(err)
                continue
            self.store.set_store_price(self.key, canonical, pick(canonical, products, self.store.facts(canonical)))

    def price(self, canonical: str) -> StorePrice | None:
        fresh, hit = self._fresh(canonical)
        if not fresh:
            self.prefetch([canonical])
            hit = self._fresh(canonical)[1]
        return hit

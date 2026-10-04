"""SQLite store: the recipe pool, the ingredient dictionary, prices and history.

The pool and the dictionary grow across runs, so each week needs fewer new
fetches and fewer model calls than the one before.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse

SCHEMA = """
CREATE TABLE IF NOT EXISTS recipes (
    id TEXT PRIMARY KEY,
    url TEXT UNIQUE NOT NULL,
    title TEXT NOT NULL,
    site TEXT,
    cuisine TEXT,
    servings REAL,
    total_time INTEGER,
    ingredients TEXT NOT NULL,     -- JSON list of raw lines
    nutrition TEXT,                -- JSON per-serving macros from the page, if any
    tags TEXT,                     -- JSON list
    source_query TEXT,
    added_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS recipe_items (
    recipe_id TEXT NOT NULL REFERENCES recipes(id) ON DELETE CASCADE,
    idx INTEGER NOT NULL,
    raw TEXT NOT NULL,
    name TEXT,
    canonical TEXT,
    grams REAL,
    PRIMARY KEY (recipe_id, idx)
);
CREATE TABLE IF NOT EXISTS ingredients (
    canonical TEXT PRIMARY KEY,
    category TEXT,
    each_g REAL,
    density REAL,
    kcal REAL, protein REAL, carbs REAL, fat REAL,   -- per 100 g
    package_g REAL,
    package_price REAL,
    currency TEXT,
    source TEXT,                                      -- seed | llm | user
    updated_at TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
    name TEXT PRIMARY KEY,
    canonical TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pantry_matches (
    staples_key TEXT NOT NULL,     -- hash of the pantry list the verdict was made against
    name TEXT NOT NULL,            -- canonical ingredient, or the parsed name when there is none
    verdict TEXT NOT NULL,         -- covered | maybe | no
    staple TEXT,                   -- the pantry entry it matched
    PRIMARY KEY (staples_key, name)
);
CREATE TABLE IF NOT EXISTS store_prices (
    store TEXT NOT NULL,           -- e.g. kroger:<locationId>
    canonical TEXT NOT NULL,
    product TEXT,                  -- NULL: searched, no usable match (estimate is used)
    package_g REAL,
    price REAL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (store, canonical)
);
CREATE TABLE IF NOT EXISTS substitutes (
    scope TEXT NOT NULL,           -- hash of the pantry list the judgment was made against
    canonical TEXT NOT NULL,
    substitute TEXT NOT NULL,      -- '' marks "asked about this ingredient"
    quality TEXT,                  -- same | close | noticeable
    PRIMARY KEY (scope, canonical, substitute)
);
CREATE TABLE IF NOT EXISTS plans (
    id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    path TEXT,
    recipe_ids TEXT NOT NULL,
    summary TEXT
);
CREATE TABLE IF NOT EXISTS suggestions (
    plan_id TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    suggested_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, recipe_id)
);
CREATE TABLE IF NOT EXISTS feedback (
    plan_id TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    cooked INTEGER,
    liked INTEGER,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY (plan_id, recipe_id)
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def recipe_id(url: str) -> str:
    return hashlib.sha1(url.strip().lower().rstrip("/").encode()).hexdigest()[:10]


@dataclass
class Recipe:
    url: str
    title: str
    ingredients: list[str]
    cuisine: str = ""
    servings: float | None = None
    total_time: int | None = None
    nutrition: dict[str, float] = field(default_factory=dict)
    tags: list[str] = field(default_factory=list)
    source_query: str = ""
    id: str = ""
    site: str = ""

    def __post_init__(self) -> None:
        self.id = self.id or recipe_id(self.url)
        self.site = self.site or urlparse(self.url).netloc.removeprefix("www.")


@dataclass
class IngredientFacts:
    canonical: str
    category: str = "other"
    each_g: float | None = None
    density: float | None = None
    kcal: float | None = None
    protein: float | None = None
    carbs: float | None = None
    fat: float | None = None
    package_g: float | None = None
    package_price: float | None = None
    currency: str = "USD"
    source: str = "llm"


@dataclass
class StorePrice:
    """A real shelf price at one store: one package of `package_g` grams for `price`."""

    package_g: float
    price: float
    product: str = ""


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # check_same_thread=False: LangGraph runs tools on worker threads; callers serialize access.
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ---- recipes -------------------------------------------------------
    def has_url(self, url: str) -> bool:
        return self.db.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id(url),)).fetchone() is not None

    def add_recipe(self, r: Recipe) -> bool:
        cur = self.db.execute(
            "INSERT OR IGNORE INTO recipes VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (r.id, r.url, r.title, r.site, r.cuisine.lower(), r.servings, r.total_time,
             json.dumps(r.ingredients), json.dumps(r.nutrition), json.dumps(r.tags), r.source_query, now()),
        )
        self.db.commit()
        return cur.rowcount > 0

    def _row_to_recipe(self, row: sqlite3.Row) -> Recipe:
        return Recipe(
            id=row["id"], url=row["url"], title=row["title"], site=row["site"], cuisine=row["cuisine"] or "",
            servings=row["servings"], total_time=row["total_time"], ingredients=json.loads(row["ingredients"]),
            nutrition=json.loads(row["nutrition"] or "{}"), tags=json.loads(row["tags"] or "[]"),
            source_query=row["source_query"] or "",
        )

    def get_recipe(self, rid: str) -> Recipe | None:
        row = self.db.execute("SELECT * FROM recipes WHERE id = ?", (rid,)).fetchone()
        return self._row_to_recipe(row) if row else None

    def all_recipes(self) -> list[Recipe]:
        return [self._row_to_recipe(r) for r in self.db.execute("SELECT * FROM recipes ORDER BY added_at")]

    def set_items(self, rid: str, items: list[tuple[str, str, str | None, float | None]]) -> None:
        """items: (raw, name, canonical, grams)"""
        self.db.execute("DELETE FROM recipe_items WHERE recipe_id = ?", (rid,))
        self.db.executemany(
            "INSERT INTO recipe_items VALUES (?,?,?,?,?,?)",
            [(rid, i, raw, name, canon, grams) for i, (raw, name, canon, grams) in enumerate(items)],
        )
        self.db.commit()

    def items(self, rid: str) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM recipe_items WHERE recipe_id = ? ORDER BY idx", (rid,)).fetchall()

    def unnormalized_recipe_ids(self) -> list[str]:
        rows = self.db.execute(
            "SELECT id FROM recipes WHERE id NOT IN (SELECT DISTINCT recipe_id FROM recipe_items)"
        ).fetchall()
        return [r["id"] for r in rows]

    # ---- ingredient dictionary ----------------------------------------
    def alias(self, name: str) -> str | None:
        row = self.db.execute("SELECT canonical FROM aliases WHERE name = ?", (name,)).fetchone()
        return row["canonical"] if row else None

    def set_alias(self, name: str, canonical: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO aliases VALUES (?,?)", (name, canonical))
        self.db.commit()

    def facts(self, canonical: str) -> IngredientFacts | None:
        row = self.db.execute("SELECT * FROM ingredients WHERE canonical = ?", (canonical,)).fetchone()
        if not row:
            return None
        return IngredientFacts(**{k: row[k] for k in row.keys() if k != "updated_at"})

    def upsert_facts(self, f: IngredientFacts, *, overwrite_user: bool = False) -> None:
        existing = self.facts(f.canonical)
        if existing and existing.source == "user" and not overwrite_user:
            return
        self.db.execute(
            "INSERT OR REPLACE INTO ingredients VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (f.canonical, f.category, f.each_g, f.density, f.kcal, f.protein, f.carbs, f.fat,
             f.package_g, f.package_price, f.currency, f.source, now()),
        )
        self.db.commit()

    def all_facts(self) -> list[IngredientFacts]:
        rows = self.db.execute("SELECT * FROM ingredients ORDER BY canonical").fetchall()
        return [IngredientFacts(**{k: r[k] for k in r.keys() if k != "updated_at"}) for r in rows]

    def item_keys(self) -> set[str]:
        """Every ingredient in the pool, as the key pantry matching uses."""
        rows = self.db.execute("SELECT DISTINCT lower(coalesce(canonical, name)) AS k FROM recipe_items")
        return {r["k"] for r in rows if r["k"]}

    # ---- pantry matches -------------------------------------------------
    def pantry_verdicts(self, key: str) -> dict[str, tuple[str, str | None]]:
        rows = self.db.execute("SELECT name, verdict, staple FROM pantry_matches WHERE staples_key = ?", (key,))
        return {r["name"]: (r["verdict"], r["staple"]) for r in rows}

    def set_pantry_verdict(self, key: str, name: str, verdict: str, staple: str | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO pantry_matches VALUES (?,?,?,?)", (key, name, verdict, staple))
        self.db.commit()

    def item_canonicals(self) -> set[str]:
        rows = self.db.execute("SELECT DISTINCT canonical FROM recipe_items WHERE canonical IS NOT NULL")
        return {r["canonical"] for r in rows}

    # ---- substitutions --------------------------------------------------
    def swaps_asked(self, scope: str, canonical: str) -> bool:
        return self.db.execute("SELECT 1 FROM substitutes WHERE scope = ? AND canonical = ? AND substitute = ''",
                               (scope, canonical)).fetchone() is not None

    def mark_swaps_asked(self, scope: str, canonical: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO substitutes VALUES (?,?,'',NULL)", (scope, canonical))
        self.db.commit()

    def set_swap(self, scope: str, a: str, b: str, quality: str) -> None:
        """Swaps are symmetric: store both directions."""
        self.db.executemany("INSERT OR REPLACE INTO substitutes VALUES (?,?,?,?)",
                            [(scope, a, b, quality), (scope, b, a, quality)])
        self.db.commit()

    def swaps(self, scope: str) -> dict[str, dict[str, str]]:
        """canonical -> {substitute: quality}."""
        out: dict[str, dict[str, str]] = {}
        for r in self.db.execute("SELECT * FROM substitutes WHERE scope = ? AND substitute != ''", (scope,)):
            out.setdefault(r["canonical"], {})[r["substitute"]] = r["quality"]
        return out

    # ---- store prices ---------------------------------------------------
    def store_price(self, store: str, canonical: str) -> tuple[StorePrice | None, str] | None:
        """(price or None for "no match", fetched_at), or None when never looked up."""
        row = self.db.execute("SELECT * FROM store_prices WHERE store = ? AND canonical = ?",
                              (store, canonical)).fetchone()
        if row is None:
            return None
        hit = StorePrice(row["package_g"], row["price"], row["product"]) if row["product"] else None
        return hit, row["fetched_at"]

    def set_store_price(self, store: str, canonical: str, price: StorePrice | None) -> None:
        self.db.execute("INSERT OR REPLACE INTO store_prices VALUES (?,?,?,?,?,?)",
                        (store, canonical, price.product if price else None, price.package_g if price else None,
                         price.price if price else None, now()))
        self.db.commit()

    # ---- plans & history ----------------------------------------------
    def record_plan(self, plan_id: str, recipe_ids: list[str], path: str, summary: dict) -> None:
        ts = now()
        self.db.execute("INSERT OR REPLACE INTO plans VALUES (?,?,?,?,?)",
                        (plan_id, ts, path, json.dumps(recipe_ids), json.dumps(summary)))
        self.db.executemany("INSERT OR IGNORE INTO suggestions VALUES (?,?,?)",
                            [(plan_id, rid, ts) for rid in recipe_ids])
        self.db.commit()

    def record_feedback(self, plan_id: str, rid: str, cooked: bool, liked: bool) -> None:
        self.db.execute("INSERT OR REPLACE INTO feedback VALUES (?,?,?,?,?)",
                        (plan_id, rid, int(cooked), int(liked), now()))
        self.db.commit()

    def suggested_ids(self, within_weeks: int | None = None) -> set[str]:
        q, args = "SELECT recipe_id FROM suggestions", ()
        if within_weeks is not None:
            cutoff = (datetime.now(timezone.utc) - timedelta(weeks=within_weeks)).isoformat(timespec="seconds")
            q, args = q + " WHERE suggested_at >= ?", (cutoff,)
        return {r["recipe_id"] for r in self.db.execute(q, args)}

    def feedback_rows(self) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM feedback").fetchall()

    def plan_count(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM plans").fetchone()[0]

    def recent_plans(self, limit: int = 8) -> list[sqlite3.Row]:
        return self.db.execute("SELECT * FROM plans ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()

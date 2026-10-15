"""SQLite storage. One file, safe to include in Unraid appdata backups."""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS parse_cache (
    text TEXT PRIMARY KEY,
    food_key TEXT,
    category TEXT,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS foods (
    key TEXT PRIMARY KEY,           -- canonical grocery item, e.g. "yellow onion"
    display TEXT,
    category TEXT,                  -- produce, meat, seafood, dairy, bakery, pantry, spice, condiment, frozen, other
    staple INTEGER DEFAULT 0,       -- always on hand; only bought when someone says it's low or out
    brand_lock INTEGER DEFAULT 0,   -- 1 = same product only, 0 = any equivalent when out of stock
    buy_as TEXT DEFAULT 'auto',     -- auto | each | bag | weight
    created TEXT
);
CREATE TABLE IF NOT EXISTS aliases (
    alias TEXT PRIMARY KEY,
    key TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS picks (
    key TEXT PRIMARY KEY,
    item_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    price REAL,
    size_qty REAL,                  -- package size in base units (ml, g, count)
    size_dim TEXT,
    sales_unit TEXT,                -- EACH | WEIGHT (Walmart sells some produce by weight)
    approved INTEGER DEFAULT 0,
    updated TEXT,
    source TEXT DEFAULT 'search'    -- search | history | manual
);
CREATE TABLE IF NOT EXISTS purchases (
    item_id TEXT PRIMARY KEY,       -- things this household has bought at Walmart before
    title TEXT,
    url TEXT,
    brand TEXT,
    price REAL,
    size_qty REAL,
    size_dim TEXT,
    sales_unit TEXT,
    seen INTEGER DEFAULT 1,
    updated TEXT
);
CREATE TABLE IF NOT EXISTS stock (
    key TEXT PRIMARY KEY,
    packages REAL,                  -- estimated packages on hand (0.5 = half a bottle)
    confidence REAL DEFAULT 0.5,
    updated TEXT,
    last_bought TEXT,
    restock INTEGER DEFAULT 0       -- someone said "we're out/low": goes on the next order even if no meal needs it
);
CREATE TABLE IF NOT EXISTS recipe_yields (
    recipe_id TEXT PRIMARY KEY,
    servings REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS meal_servings (
    date TEXT NOT NULL,
    entry_type TEXT NOT NULL,
    recipe_id TEXT NOT NULL,
    servings REAL NOT NULL,
    PRIMARY KEY (date, entry_type, recipe_id)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start TEXT NOT NULL,
    created TEXT NOT NULL,
    status TEXT NOT NULL,           -- pending | approved | carted | failed
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    kind TEXT NOT NULL,
    data TEXT
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    data TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meal_orders (
    key TEXT PRIMARY KEY,      -- Mealie meal-plan entry id
    recipe_id TEXT,
    date TEXT,
    ordered TEXT NOT NULL,     -- when the order was checked out
    review_id INTEGER
);
CREATE TABLE IF NOT EXISTS cart_runs (
    week_start TEXT PRIMARY KEY,
    review_id INTEGER NOT NULL,
    state TEXT NOT NULL,
    data TEXT NOT NULL,
    updated TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(stock)")}
        if "restock" not in cols:
            self._conn.execute("ALTER TABLE stock ADD COLUMN restock INTEGER DEFAULT 0")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(picks)")}
        if "source" not in cols:
            self._conn.execute("ALTER TABLE picks ADD COLUMN source TEXT DEFAULT 'search'")
        for table in ("picks", "purchases"):
            cols = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if "image_url" not in cols:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN image_url TEXT")
        cols = {r[1] for r in self._conn.execute("PRAGMA table_info(picks)")}
        for col, kind in {"local_available": "INTEGER DEFAULT 0", "store_id": "TEXT",
                          "fulfillment": "TEXT", "local_reason": "TEXT"}.items():
            if col not in cols:
                self._conn.execute(f"ALTER TABLE picks ADD COLUMN {col} {kind}")
        self._conn.commit()
        self._lock = threading.RLock()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                yield self._conn
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise

    def one(self, sql: str, *args: Any) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(sql, args).fetchone()

    def all(self, sql: str, *args: Any) -> list[sqlite3.Row]:
        with self._lock:
            return self._conn.execute(sql, args).fetchall()

    def run(self, sql: str, *args: Any) -> None:
        with self.tx() as c:
            c.execute(sql, args)

    # ---- foods ----
    def resolve(self, key: str) -> str:
        row = self.one("SELECT key FROM aliases WHERE alias = ?", key)
        return row["key"] if row else key

    def ensure_food(self, key: str, category: str | None, display: str | None = None) -> None:
        self.run(
            "INSERT OR IGNORE INTO foods(key, display, category, created) VALUES (?,?,?,?)",
            key, display or key, category or "other", now_iso(),
        )

    def food(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM foods WHERE key = ?", key)

    def set_food(self, key: str, **fields: Any) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE foods SET {cols} WHERE key = ?", *fields.values(), key)

    def merge_food(self, old: str, new: str) -> None:
        """Fold one food into another (duplicates like 'evoo' -> 'olive oil')."""
        with self.tx() as c:
            c.execute("INSERT OR REPLACE INTO aliases(alias, key) VALUES (?,?)", (old, new))
            c.execute("UPDATE aliases SET key = ? WHERE key = ?", (new, old))
            c.execute("UPDATE parse_cache SET food_key = ? WHERE food_key = ?", (new, old))
            c.execute("DELETE FROM picks WHERE key = ?", (old,))
            c.execute("DELETE FROM stock WHERE key = ?", (old,))
            c.execute("DELETE FROM foods WHERE key = ?", (old,))

    # ---- picks ----
    def pick(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM picks WHERE key = ?", key)

    def save_pick(self, key: str, p: dict, approved: bool, source: str = "search") -> None:
        self.run(
            """INSERT INTO picks(key, item_id, title, url, price, size_qty, size_dim, sales_unit, approved, updated, source,
                                 image_url)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET item_id=excluded.item_id, title=excluded.title, url=excluded.url,
               price=excluded.price, size_qty=excluded.size_qty, size_dim=excluded.size_dim,
               sales_unit=excluded.sales_unit, approved=excluded.approved, updated=excluded.updated,
               source=excluded.source, image_url=excluded.image_url""",
            key, str(p["item_id"]), p.get("title"), p.get("url"), p.get("price"),
            p.get("size_qty"), p.get("size_dim"), p.get("sales_unit"), int(approved), now_iso(), source,
            p.get("image") or p.get("image_url") or self.image_for(str(p["item_id"])),
        )
        self.run("UPDATE picks SET local_available=?, store_id=?, fulfillment=?, local_reason=? WHERE key=?",
                 int(bool(p.get("local_available"))), p.get("store_id"), p.get("fulfillment"), p.get("local_reason"), key)

    def image_for(self, item_id: str) -> str | None:
        for table in ("picks", "purchases"):
            row = self.one(f"SELECT image_url FROM {table} WHERE item_id = ? AND image_url IS NOT NULL LIMIT 1", item_id)
            if row:
                return row["image_url"]
        return None

    def set_image(self, item_id: str, url: str | None) -> None:
        self.run("UPDATE picks SET image_url = ? WHERE item_id = ?", url or "", item_id)

    # ---- purchase history ----
    def save_purchases(self, items: list[dict]) -> int:
        with self.tx() as c:
            for p in items:
                c.execute(
                    """INSERT INTO purchases(item_id, title, url, brand, price, size_qty, size_dim, sales_unit, seen, updated,
                                             image_url)
                       VALUES (?,?,?,?,?,?,?,?,1,?,?)
                       ON CONFLICT(item_id) DO UPDATE SET title=excluded.title, url=excluded.url,
                       brand=COALESCE(excluded.brand, purchases.brand), price=COALESCE(excluded.price, purchases.price),
                       size_qty=excluded.size_qty, size_dim=excluded.size_dim, sales_unit=excluded.sales_unit,
                       seen=purchases.seen + 1, updated=excluded.updated,
                       image_url=COALESCE(excluded.image_url, purchases.image_url)""",
                    (p["item_id"], p["title"], p.get("url"), p.get("brand"), p.get("price"), p.get("size_qty"),
                     p.get("size_dim"), p.get("sales_unit"), now_iso(), p.get("image")),
                )
        return len(items)

    def purchases(self) -> list[dict]:
        return [dict(r) for r in self.all("SELECT * FROM purchases")]

    def purchased_ids(self) -> set[str]:
        return {r["item_id"] for r in self.all("SELECT item_id FROM purchases")}

    def top_brands(self, n: int = 15) -> list[str]:
        rows = self.all("""SELECT brand, COUNT(*) c FROM purchases WHERE brand IS NOT NULL AND brand != ''
                           GROUP BY brand ORDER BY c DESC LIMIT ?""", n)
        return [r["brand"] for r in rows]

    def last_event(self, kind: str) -> str | None:
        row = self.one("SELECT ts FROM events WHERE kind = ? ORDER BY id DESC LIMIT 1", kind)
        return row["ts"] if row else None

    # ---- stock ----
    def stock(self, key: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM stock WHERE key = ?", key)

    def set_stock(self, key: str, packages: float, confidence: float, bought: bool = False) -> None:
        self.run(
            """INSERT INTO stock(key, packages, confidence, updated, last_bought) VALUES (?,?,?,?,?)
               ON CONFLICT(key) DO UPDATE SET packages=excluded.packages, confidence=excluded.confidence,
               updated=excluded.updated, last_bought=COALESCE(excluded.last_bought, stock.last_bought)""",
            key, max(packages, 0.0), confidence, now_iso(), now_iso() if bought else None,
        )

    def set_restock(self, key: str, on: bool) -> None:
        self.run("UPDATE stock SET restock = ? WHERE key = ?", int(on), key)

    def restock_keys(self) -> list[str]:
        return [r["key"] for r in self.all("SELECT key FROM stock WHERE restock = 1")]

    # ---- servings ----
    def recipe_yield(self, recipe_id: str) -> float | None:
        row = self.one("SELECT servings FROM recipe_yields WHERE recipe_id = ?", recipe_id)
        return row["servings"] if row else None

    def set_recipe_yield(self, recipe_id: str, servings: float) -> None:
        self.run("INSERT OR REPLACE INTO recipe_yields(recipe_id, servings) VALUES (?,?)", recipe_id, servings)

    def meal_servings(self, date: str, entry_type: str, recipe_id: str) -> float | None:
        row = self.one(
            "SELECT servings FROM meal_servings WHERE date=? AND entry_type=? AND recipe_id=?",
            date, entry_type, recipe_id,
        )
        return row["servings"] if row else None

    def set_meal_servings(self, date: str, entry_type: str, recipe_id: str, servings: float) -> None:
        self.run(
            "INSERT OR REPLACE INTO meal_servings(date, entry_type, recipe_id, servings) VALUES (?,?,?,?)",
            date, entry_type, recipe_id, servings,
        )

    # ---- reviews ----
    def latest_review(self) -> sqlite3.Row | None:
        return self.one("SELECT * FROM reviews ORDER BY id DESC LIMIT 1")

    def save_review(self, week_start: str, data: dict, status: str = "pending") -> int:
        with self.tx() as c:
            cur = c.execute(
                "INSERT INTO reviews(week_start, created, status, data) VALUES (?,?,?,?)",
                (week_start, now_iso(), status, json.dumps(data)),
            )
            return int(cur.lastrowid)

    def update_review(self, review_id: int, data: dict | None = None, status: str | None = None) -> None:
        if data is not None:
            self.run("UPDATE reviews SET data = ? WHERE id = ?", json.dumps(data), review_id)
        if status is not None:
            self.run("UPDATE reviews SET status = ? WHERE id = ?", status, review_id)

    def log(self, kind: str, **data: Any) -> None:
        self.run("INSERT INTO events(ts, kind, data) VALUES (?,?,?)", now_iso(), kind, json.dumps(data, default=str))

    def settings(self, key: str, default=None):
        row = self.one("SELECT data FROM settings WHERE key = ?", key)
        return json.loads(row["data"]) if row else default

    def save_settings(self, key: str, data: dict) -> None:
        self.run("INSERT OR REPLACE INTO settings(key, data) VALUES (?,?)", key, json.dumps(data))

    def cart_run(self, week_start: str):
        row = self.one("SELECT * FROM cart_runs WHERE week_start = ?", week_start)
        return {**dict(row), "data": json.loads(row["data"])} if row else None

    def save_cart_run(self, week_start: str, review_id: int, state: str, data: dict) -> None:
        self.run("""INSERT INTO cart_runs(week_start, review_id, state, data, updated) VALUES (?,?,?,?,?)
                    ON CONFLICT(week_start) DO UPDATE SET state=excluded.state,
                    data=excluded.data, updated=excluded.updated""",
                 week_start, review_id, state, json.dumps(data), now_iso())

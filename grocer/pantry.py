"""Physical stock, reservations, and explicit meal consumption."""

from __future__ import annotations

from . import units
from .db import now_iso


def _db():
    from . import jobs

    return jobs.db


def _convert(qty, source, target, key):
    if source == target:
        return qty
    pick = _db().pick(key)
    if source == "pkg" and pick:
        source = pick["size_dim"] or "count"
        qty *= pick["size_qty"] or 1
        if source == target:
            return qty
    food = _db().food(key)
    a = units.convert(
        units.Amount(qty, source), target, key, food["category"] if food else "other"
    )
    return a.qty if a else None


def adjust(c, key: str, qty: float, dim: str, kind: str, ref: str, item_id=None):
    row = c.execute("SELECT * FROM stock WHERE key=?", (key,)).fetchone()
    current = 0.0
    confidence = 0.7
    if row and row["quantity"]:
        converted = _convert(row["quantity"], row["dimension"], dim, key)
        if converted is None or (
            dim == "package"
            and row["basis_item_id"]
            and row["basis_item_id"] != item_id
        ):
            # Do not silently reinterpret an incompatible estimate as a different product.
            raise RuntimeError(
                f"Check how much {key} you have at home before recording this change."
            )
        current = converted
    total = max(current + qty, 0)
    pick = _db().pick(key)
    packages = total
    if pick:
        normalized = _convert(total, dim, pick["size_dim"] or "count", key)
        if normalized is not None:
            packages = normalized / (pick["size_qty"] or 1)
    c.execute(
        """INSERT INTO stock(key,packages,confidence,updated,last_bought,restock,quantity,dimension,basis_item_id)
                 VALUES (?,?,?,?,?,0,?,?,?) ON CONFLICT(key) DO UPDATE SET
                 packages=excluded.packages, quantity=excluded.quantity, dimension=excluded.dimension, basis_item_id=excluded.basis_item_id,
                 confidence=excluded.confidence, updated=excluded.updated,
                 last_bought=COALESCE(excluded.last_bought,stock.last_bought),
                 restock=CASE WHEN ? > 0 THEN 0 ELSE stock.restock END""",
        (
            key,
            packages,
            confidence,
            now_iso(),
            now_iso() if qty > 0 else None,
            total,
            dim,
            item_id,
            qty,
        ),
    )
    c.execute(
        "INSERT INTO inventory_events(ts,kind,food_key,quantity,dimension,reference) VALUES (?,?,?,?,?,?)",
        (now_iso(), kind, key, qty, dim, ref),
    )


def credit(c, line: dict, ref: str):
    pick = line["pick"]
    dim, size = pick.get("size_dim") or "count", pick.get("size_qty") or 1
    adjust(c, line["key"], line["qty"] * size, dim, "purchase", ref, pick["item_id"])


def reserve(c, review: dict, review_id: int):
    for meal in review.get("meals", []):
        key = meal.get("key")
        if not key:
            continue
        c.execute(
            """INSERT INTO meal_progress VALUES (?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                     name=excluded.name,date=excluded.date,review_id=excluded.review_id""",
            (key, meal["name"], meal["date"], "planned", review_id),
        )
    for item in review["items"]:
        for key, usage in item.get("meal_usage", {}).items():
            progress = c.execute(
                "SELECT state FROM meal_progress WHERE key=?", (key,)
            ).fetchone()
            if not progress or progress["state"] != "planned":
                continue
            # Usage is physical, and survives a change of package size or chosen product.
            pick = item.get("pick") or {}
            target = pick.get("size_dim")
            total = 0
            for dim, qty in usage.items():
                if qty <= 0:
                    continue
                target = target or dim
                amount = _convert(qty, dim, target, item["key"])
                if amount is not None:
                    total += amount
            if total:
                c.execute(
                    """INSERT INTO reservations VALUES (?,?,?,?) ON CONFLICT(meal_key,food_key)
                             DO UPDATE SET quantity=excluded.quantity,dimension=excluded.dimension""",
                    (key, item["key"], total, target),
                )

        for key in item.get("meal_keys", []):
            progress = c.execute(
                "SELECT state FROM meal_progress WHERE key=?", (key,)
            ).fetchone()
            if progress and progress["state"] == "planned":
                c.execute(
                    "INSERT OR IGNORE INTO reservations VALUES (?,?,?,?)",
                    (key, item["key"], 0, "unknown"),
                )


def reserved_packages(key: str, excluding=()) -> float:
    db = _db()
    pick = db.pick(key)
    if not pick:
        return 0
    total = 0
    for r in db.all("SELECT * FROM reservations WHERE food_key=?", key):
        if r["meal_key"] in excluding:
            continue
        qty = _convert(r["quantity"], r["dimension"], pick["size_dim"] or "count", key)
        if qty is not None:
            total += qty / (pick["size_qty"] or 1)
    return total


def pending_meals() -> list[dict]:
    return [
        dict(r)
        for r in _db().all(
            "SELECT * FROM meal_progress WHERE state='planned' ORDER BY date,key"
        )
    ]


def resolve_meal(key: str, action: str):
    if action not in {"cooked", "skipped"}:
        raise ValueError("Choose cooked or skipped")
    db = _db()
    with db.tx() as c:
        meal = c.execute("SELECT * FROM meal_progress WHERE key=?", (key,)).fetchone()
        if not meal:
            raise ValueError("That dinner has no reserved ingredients")
        if meal["state"] != "planned":
            return  # idempotent double tap
        if action == "cooked":
            for r in c.execute(
                "SELECT * FROM reservations WHERE meal_key=?", (key,)
            ).fetchall():
                pick = db.pick(r["food_key"])
                stock = db.stock(r["food_key"])
                if r["dimension"] == "unknown" or (
                    stock
                    and stock["quantity"]
                    and _convert(
                        stock["quantity"],
                        stock["dimension"],
                        r["dimension"],
                        r["food_key"],
                    )
                    is None
                ):
                    c.execute(
                        "UPDATE stock SET confidence=MIN(confidence,0.3),updated=? WHERE key=?",
                        (now_iso(), r["food_key"]),
                    )
                    c.execute(
                        "INSERT INTO inventory_events(ts,kind,food_key,quantity,dimension,reference) VALUES (?,?,?,?,?,?)",
                        (now_iso(), "cooked_unknown", r["food_key"], 0, "unknown", key),
                    )
                    continue
                adjust(
                    c,
                    r["food_key"],
                    -r["quantity"],
                    r["dimension"],
                    "cooked",
                    key,
                    pick["item_id"] if pick else None,
                )
        c.execute("DELETE FROM reservations WHERE meal_key=?", (key,))
        c.execute("UPDATE meal_progress SET state=? WHERE key=?", (action, key))
    from . import jobs

    jobs.changed()


def reservation_uncertain(key: str, excluding=()) -> bool:
    db = _db()
    pick = db.pick(key)
    if not pick:
        return False
    target = pick["size_dim"] or "count"
    return any(
        r["meal_key"] not in excluding
        and (
            r["dimension"] == "unknown"
            or _convert(r["quantity"], r["dimension"], target, key) is None
        )
        for r in db.all("SELECT * FROM reservations WHERE food_key=?", key)
    )

"""Package calculations and quantity targets used by the durable cart workflow."""
from __future__ import annotations

import math

from . import planner, units


def plan_line(item: dict, chosen: dict) -> dict | None:
    original = item["pick"]
    same = str(original["item_id"]) == str(chosen["item_id"])
    need = planner.Need(key=item["key"], category=item["category"], amounts=item["amounts"])
    ratio = 1.0
    if not same:
        old_size, new_size = original.get("size_qty"), chosen.get("size_qty")
        old_dim, new_dim = original.get("size_dim"), chosen.get("size_dim")
        if old_size and new_size and old_dim and new_dim:
            amount = units.convert(units.Amount(new_size, new_dim), old_dim, item["key"], item["category"])
            if amount is None:
                return None
            ratio = amount.qty / old_size
        elif any((old_size, new_size)):
            return None  # cannot safely infer the replacement's package count
    if item.get("manual_qty") or item.get("use_pk") is None:
        qty = item["qty"] if same else math.ceil(item["qty"] / ratio - 1e-6)
    else:
        qty = math.ceil(max(item["use_pk"] - (item.get("stock") or 0), 0) / ratio - 1e-6)
        qty = max(1, qty)  # explicit restocking can be needed even with a small recipe quantity
    return {"key": item["key"], "pick": chosen, "qty": qty,
            "stock_credit": qty * ratio, "used": item.get("use_pk") or 0}


def targets(lines: list[dict], existing: dict[str, int]) -> dict[str, int]:
    requested = {}
    for line in lines:
        iid = str(line["pick"]["item_id"])
        requested[iid] = requested.get(iid, 0) + line["qty"]
    return {iid: max(qty, existing.get(iid, 0)) for iid, qty in requested.items()}


def deficits(expected: dict[str, int], current: dict[str, int]) -> list[tuple[str, int]]:
    return [(iid, qty - current.get(iid, 0)) for iid, qty in expected.items() if qty > current.get(iid, 0)]

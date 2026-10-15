"""A shopping trip owns its input version, decisions, and meal coverage."""

from __future__ import annotations

import hashlib
import json


def input_version(entries=None, recipes=None) -> str:
    from . import dinners
    from . import jobs as j

    entries = dinners.selected() if entries is None else entries
    snapshot = []
    recipes = {} if recipes is None else dict(recipes)
    for e in entries:
        if e.recipe_id not in recipes:
            recipes[e.recipe_id] = j.mealie.recipe(e.recipe_slug or e.recipe_id)
        recipe = recipes[e.recipe_id]
        snapshot.append(
            {
                "key": dinners.key_of(e),
                "date": e.date,
                "recipe": e.recipe_id,
                "servings": j.db.meal_servings(e.date, e.entry_type, e.recipe_id)
                or j.CONFIG.default_servings,
                "yield": j.db.recipe_yield(e.recipe_id),
                "recipe_content": {
                    "servings": recipe.servings,
                    "yield_text": recipe.yield_text,
                    "ingredients": [i.__dict__ for i in recipe.ingredients],
                },
            }
        )
    value = {
        "meals": sorted(snapshot, key=lambda x: x["key"]),
        "store": j.walmart_settings(),
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def require_current(data: dict) -> None:
    # Old reviews have no trustworthy snapshot. Rebuild once after upgrading.
    if not data.get("input_version") or data["input_version"] != input_version():
        from . import jobs as j

        data["stale"] = True
        row = j.db.latest_review()
        if row and row["status"] in {"pending", "approved"}:
            j.db.update_review(row["id"], data=data, status="pending")
            j.changed()
        raise RuntimeError(
            "The dinners, servings, or store changed. Update the shopping list before sending it."
        )


def coverage(review: dict, plan: dict) -> list[dict]:
    missing = set(
        plan.get("carried", []) + plan.get("unanswered", []) + plan.get("no_pick", [])
    )
    missing_ids = set(map(str, plan.get("missing", [])))
    missing.update(
        l["key"]
        for l in plan.get("lines", [])
        if str(l["pick"]["item_id"]) in missing_ids
    )
    missing.update(i["key"] for i in review["items"] if i["status"] == "skip")
    missing -= set(plan.get("manual_purchases", []))
    result = []
    for meal in review.get("meals", []):
        key = meal.get("key")
        needed = [
            i["key"]
            for i in review["items"]
            if i["key"] in missing
            and (
                key in i.get("meal_keys", [])
                or (not i.get("meal_keys") and meal["name"] in i.get("sources", []))
            )
        ]
        unread = [
            line
            for line in meal.get(
                "unparsed",
                [
                    text
                    for text in review.get("unparsed", [])
                    if text.startswith(meal["name"] + ":")
                ],
            )
            if line not in plan.get("manual_purchases", [])
        ]
        if meal.get("yield_question"):
            needed.append("Recipe servings need checking")
        result.append(
            {
                "key": key,
                "name": meal["name"],
                "date": meal["date"],
                "missing": sorted(set(needed + unread)),
                "complete": not needed and not unread,
            }
        )
    return result


def owned_removals(plan: dict, contents: dict[str, int]) -> tuple[set[str], list[str]]:
    """Removing whole lines is safe only for unchanged, entirely app-owned quantities."""
    before, added = plan.get("cart_before"), plan.get("added_quantities")
    if before is None or added is None:
        return set(), list((plan.get("targets") or {}).keys())
    remove, protected = set(), []
    for iid, qty in added.items():
        current = contents.get(iid, 0)
        if not current:
            continue
        if before.get(iid, 0) == 0 and current == qty:
            remove.add(iid)
        else:
            protected.append(iid)
    return remove, protected

"""Choosing which dinners an order covers, and planning/swapping dinners in Mealie.

Mealie's meal plan stays the source of truth. This module adds three things on top:
- which planned dinners have already been ordered (marked at checkout)
- which ones you've ticked for the next order (default: the coming unordered week)
- a weighted random picker that favors recipes you haven't had in a while
"""
from __future__ import annotations

import random
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

WINDOW_DAYS = 14        # how far ahead the Dinners page looks
HISTORY_DAYS = 120      # how far back "last eaten" looks
MAX_WEIGHT = 60         # a recipe not eaten in 60+ days is as likely as one never eaten


def _j():
    from . import jobs
    return jobs


def key_of(e) -> str:
    return e.id or f"{e.date}|{e.entry_type}|{e.recipe_id}"


def _default_end(start: date) -> date:
    """Through the next Saturday, or the one after if that's under 3 days of dinners."""
    end = start + timedelta(days=(5 - start.weekday()) % 7)
    return end + timedelta(days=7) if (end - start).days < 2 else end


def _plan(start: date, end: date) -> list:
    j = _j()
    return [e for e in j.mealie.mealplan(start, end) if e.entry_type in j.CONFIG.entry_types and e.recipe_id]


def ordered() -> dict[str, str]:
    return {r["key"]: r["ordered"] for r in _j().db.all("SELECT key, ordered FROM meal_orders")}


def _choices() -> dict[str, bool]:
    return _j().db.settings("meal_choice", {}) or {}


def _is_selected(e, done: dict, choice: dict, auto_end: date) -> bool:
    k = key_of(e)
    if k in done:
        return False
    if k in choice:
        return choice[k]
    return date.fromisoformat(e.date) <= auto_end


def upcoming() -> tuple[list, dict, dict, date]:
    j = _j()
    t = j.today()
    entries = sorted(_plan(t, t + timedelta(days=WINDOW_DAYS - 1)), key=lambda e: e.date)
    done, choice = ordered(), _choices()
    first = next((date.fromisoformat(e.date) for e in entries if key_of(e) not in done), t)
    return entries, done, choice, _default_end(first)


def selected() -> list:
    entries, done, choice, auto_end = upcoming()
    return [e for e in entries if _is_selected(e, done, choice, auto_end)]


def selected_keys() -> list[str]:
    return sorted(key_of(e) for e in selected())


def overview() -> dict:
    j = _j()
    entries, done, choice, auto_end = upcoming()
    t = j.today()
    planned_days = {e.date for e in entries}
    meals = []
    for e in entries:
        k = key_of(e)
        meals.append({
            "key": k, "date": e.date, "entry_type": e.entry_type, "recipe_id": e.recipe_id,
            "slug": e.recipe_slug, "name": e.title, "ordered": done.get(k),
            "selected": _is_selected(e, done, choice, auto_end),
            "servings": j.db.meal_servings(e.date, e.entry_type, e.recipe_id) or float(j.CONFIG.default_servings),
            "url": j.mealie.recipe_url(j.CONFIG.mealie_public_url, e.recipe_slug) if e.recipe_slug else None,
        })
    empty = [(t + timedelta(days=i)).isoformat() for i in range(8)
             if (t + timedelta(days=i)).isoformat() not in planned_days]
    return {"meals": meals, "empty": empty, "default_servings": j.CONFIG.default_servings,
            "rotation_tag": j.CONFIG.dinner_tag}


def choose(keys: list[str], on: bool) -> None:
    choice = _choices()
    for k in keys:
        choice[k] = bool(on)
    _j().db.save_settings("meal_choice", choice)


def mark_ordered(keys: list[str], meals: list[dict], review_id: int | None, c=None) -> None:
    """Called inside the checkout transaction."""
    now = datetime.now(ZoneInfo("UTC")).isoformat(timespec="seconds")
    by_key = {m.get("key"): m for m in meals}
    for k in keys:
        m = by_key.get(k) or {}
        c.execute("INSERT OR REPLACE INTO meal_orders(key, recipe_id, date, ordered, review_id) VALUES (?,?,?,?,?)",
                  (k, m.get("recipe_id"), m.get("date"), now, review_id))


# ---------------------------------------------------------------- the rotation and the picker

def rotation() -> tuple[list[dict], str]:
    """Recipes the picker may choose. From the Mealie tag; if nobody has tagged any yet,
    the dinners that were planned in the last few months."""
    j = _j()
    tag = j.mealie.tag(j.CONFIG.dinner_tag)
    if tag:
        tagged = j.mealie.recipes(tag_id=tag["id"])
        if tagged:
            return tagged, "tag"
    t = j.today()
    seen: dict[str, dict] = {}
    for e in _plan(t - timedelta(days=HISTORY_DAYS), t + timedelta(days=WINDOW_DAYS)):
        seen.setdefault(e.recipe_id, {"id": e.recipe_id, "slug": e.recipe_slug, "name": e.title})
    return list(seen.values()), "recent"


def _last_eaten() -> dict[str, date]:
    j = _j()
    t = j.today()
    last: dict[str, date] = {}
    for e in _plan(t - timedelta(days=HISTORY_DAYS), t):
        d = date.fromisoformat(e.date)
        if e.recipe_id not in last or d > last[e.recipe_id]:
            last[e.recipe_id] = d
    return last


def pick(exclude: set[str], rng: random.Random | None = None) -> dict | None:
    """Weighted random: the longer since you last had it, the likelier (never had = most likely)."""
    pool, _ = rotation()
    pool = [r for r in pool if r["id"] not in exclude]
    if not pool:
        return None
    t = _j().today()
    last = _last_eaten()
    weights = [min(MAX_WEIGHT, (t - last[r["id"]]).days) if r["id"] in last else MAX_WEIGHT for r in pool]
    weights = [max(w, 1) for w in weights]
    return (rng or random).choices(pool, weights=weights, k=1)[0]


def _planned_ids() -> set[str]:
    t = _j().today()
    return {e.recipe_id for e in _plan(t - timedelta(days=6), t + timedelta(days=WINDOW_DAYS))}


def swap(key: str) -> dict:
    """Re-roll one planned dinner, in Mealie too."""
    entries, done, *_ = upcoming()
    e = next((x for x in entries if key_of(x) == key), None)
    if not e:
        raise ValueError("That dinner isn't in the plan anymore")
    if key in done:
        raise ValueError("That dinner was already ordered")
    if not e.id:
        raise ValueError("Mealie didn't give this dinner an id, so it can't be swapped here")
    new = pick(_planned_ids() | {e.recipe_id})
    if not new:
        raise ValueError("No other dinners to choose from. Tag more recipes in Mealie.")
    return _replace(e, new["id"], key)


def set_dinner(day: str, recipe_id: str, key: str | None = None) -> dict:
    """Put a specific recipe on a day (replacing that day's dinner if key is given)."""
    j = _j()
    if key:
        entries, done, *_ = upcoming()
        e = next((x for x in entries if key_of(x) == key), None)
        if not e:
            raise ValueError("That dinner isn't in the plan anymore")
        if key in done:
            raise ValueError("That dinner was already ordered")
        return _replace(e, recipe_id, key)
    j.mealie.add_mealplan(date.fromisoformat(day), "dinner", recipe_id)
    return {"date": day, "recipe_id": recipe_id}


def _replace(e, recipe_id: str, old_key: str) -> dict:
    j = _j()
    was = _choices().get(old_key)
    j.mealie.add_mealplan(date.fromisoformat(e.date), e.entry_type, recipe_id)
    j.mealie.delete_mealplan(e.id)
    if was is not None:          # keep it ticked/unticked like the dinner it replaced
        new = next((x for x in _plan(date.fromisoformat(e.date), date.fromisoformat(e.date))
                    if x.recipe_id == recipe_id and x.entry_type == e.entry_type), None)
        if new:
            choose([key_of(new)], was)
    j.db.log("dinner_swapped", date=e.date, old=e.recipe_id, new=recipe_id)
    return {"date": e.date, "recipe_id": recipe_id}


def fill_day(day: str) -> dict:
    """One weighted random dinner on an empty day."""
    j = _j()
    if any(e.date == day for e in _plan(date.fromisoformat(day), date.fromisoformat(day))):
        raise ValueError("That day already has a dinner")
    r = pick(_planned_ids())
    if not r:
        raise ValueError(f"No dinners to choose from. Tag some recipes \"{j.CONFIG.dinner_tag}\" in Mealie.")
    j.mealie.add_mealplan(date.fromisoformat(day), "dinner", r["id"])
    return {"date": day, "recipe_id": r["id"], "name": r["name"]}


def fill_empty(days: int = 7) -> dict:
    """Plan a dinner on every empty day from tomorrow through `days` ahead (what HA used to do)."""
    j = _j()
    t = j.today()
    planned = {e.date for e in _plan(t, t + timedelta(days=days))}
    used = _planned_ids()
    added = []
    for i in range(1, days + 1):
        d = (t + timedelta(days=i)).isoformat()
        if d in planned:
            continue
        # avoid anything planned nearby; if that leaves nothing, just avoid repeats within this run
        r = pick(used) or pick({a["id"] for a in added})
        if not r:
            break
        j.mealie.add_mealplan(date.fromisoformat(d), "dinner", r["id"])
        used.add(r["id"])
        added.append({"date": d, "name": r["name"], "id": r["id"]})
    if added:
        j.db.log("dinners_planned", added=added)
    return {"added": added}


def recipe_choices(q: str = "") -> dict:
    j = _j()
    pool, source = rotation()
    in_rotation = {r["id"] for r in pool} if source == "tag" else set()
    found = j.mealie.recipes(search=q) if q else pool
    return {"rotation_source": source, "tag": j.CONFIG.dinner_tag,
            "recipes": [{**r, "in_rotation": r["id"] in in_rotation} for r in found[:60]]}


def set_rotation(slug: str, on: bool) -> None:
    j = _j()
    tag = j.mealie.tag(j.CONFIG.dinner_tag, create=True)
    j.mealie.set_recipe_tag(slug, tag, on)


def image_file(recipe_id: str) -> Path | None:
    j = _j()
    if not recipe_id.replace("-", "").isalnum():
        return None
    folder = Path(j.CONFIG.data_dir) / "meal-images"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{recipe_id}.webp"
    if path.exists():
        return path
    try:
        data = j.mealie.recipe_image(recipe_id)
    except Exception:  # noqa: BLE001
        return None
    if not data:
        return None
    path.write_bytes(data)
    return path

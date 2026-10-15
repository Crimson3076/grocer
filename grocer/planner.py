"""Turn a week of planned meals into scaled grocery needs, then into cart quantities."""
from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass, field

from . import units
from .db import DB, now_iso
from .llm import LLM
from .mealie import PlanEntry, Recipe
from .parse import ParsedLine, norm_key, parse_line, singular

log = logging.getLogger(__name__)

# how many packages each answer means
STOCK_LEVELS = {"out": 0.0, "low": 0.25, "medium": 0.5, "high": 0.85}
# categories where a missing stock estimate means "ask", not "buy"
ASK_CATEGORIES = {"spice", "oil", "condiment", "pantry"}
FALLBACK_YIELD = 4.0


@dataclass
class Meal:
    date: str
    entry_type: str
    recipe_id: str
    name: str
    servings: float
    recipe_servings: float | None
    yield_question: bool
    factor: float
    key: str = ''
    unparsed: list[str] = field(default_factory=list)


@dataclass
class Need:
    key: str
    category: str
    amounts: dict[str, float] = field(default_factory=dict)  # dim -> base qty
    unquantified: bool = False
    optional_only: bool = True
    sources: list[str] = field(default_factory=list)
    lines: list[str] = field(default_factory=list)
    meal_keys: list[str] = field(default_factory=list)
    meal_usage: dict[str, dict[str, float]] = field(default_factory=dict)

    def add(self, amount: units.Amount | None, source: str, line: str, optional: bool) -> None:
        if amount is None:
            self.unquantified = True
        else:
            self.amounts[amount.dim] = self.amounts.get(amount.dim, 0.0) + amount.qty
        if source not in self.sources:
            self.sources.append(source)
        self.lines.append(line)
        if not optional:
            self.optional_only = False

    def text(self) -> str:
        parts = [units.fmt(units.Amount(q, d)) + ("" if d != "count" else "") for d, q in self.amounts.items()]
        if self.unquantified:
            parts.append("some")
        return " + ".join(parts) or "some"


def yield_is_trustworthy(recipe: Recipe, max_trusted: int) -> bool:
    if not recipe.servings or recipe.servings <= 0:
        return False
    if recipe.servings > max_trusted:
        return False
    if re.search(r"\d+\s*(?:-|to)\s*\d+", recipe.yield_text or ""):
        return False
    return True


def build_meals(entries: list[PlanEntry], recipes: dict[str, Recipe], db: DB,
                default_servings: int, max_trusted: int) -> list[Meal]:
    meals = []
    for e in entries:
        if not e.recipe_id or e.recipe_id not in recipes:
            continue
        r = recipes[e.recipe_id]
        override = db.recipe_yield(r.id)
        trusted = yield_is_trustworthy(r, max_trusted)
        base = override or (r.servings if trusted else FALLBACK_YIELD)
        if not trusted and not override and r.servings and re.search(r"(\d+)\s*(?:-|to)\s*\d+", r.yield_text or ""):
            base = float(re.search(r"(\d+)", r.yield_text).group(1))  # ranges: use the low end
        servings = db.meal_servings(e.date, e.entry_type, r.id) or float(default_servings)
        meals.append(Meal(
            date=e.date, entry_type=e.entry_type, recipe_id=r.id, name=r.name,
            servings=servings, recipe_servings=r.servings,
            yield_question=not trusted and not override,
            factor=servings / base,
            key=e.id or f'{e.date}|{e.entry_type}|{e.recipe_id}',
        ))
    return meals


# words that only describe the form or cut, safe for the model to drop ("salmon filet" -> "salmon")
FORM_WORDS = {"filet", "fillet", "portion", "piece", "whole", "leaf", "leave", "bulb", "head", "bunch", "sprig",
              "raw", "plain", "regular", "fresh", "can", "jar", "large", "small", "medium"}


def keep_specific(text: str, item: str) -> str:
    """The model sometimes shortens a product to a generic word ("bbq sauce" -> "sauce",
    "chicken thigh" -> "chicken"). If its answer only drops words from the input, keep the input."""
    t, i = text.split(), item.split()
    dropped = set(t) - set(i)
    if len(i) < len(t) and set(i) <= set(t) and len(i) == 1 and not dropped <= FORM_WORDS:
        return text
    return item


# one product even though the name has "and" in it
ONE_PRODUCT = {"half and half", "mac and cheese", "macaroni and cheese", "sweet and sour", "salt and vinegar",
               "pork and bean", "surf and turf", "peanut butter and jelly", "bread and butter pickle"}
SALT_PEPPER = re.compile(r"^(?:\w+ )?salt and (?:\w+ )?pepper$")


def split_pairs(pl: ParsedLine) -> list[ParsedLine]:
    """'salt and pepper (to taste)' and '1/4 tsp each salt and pepper' are two things to buy."""
    if pl.food in ONE_PRODUCT or any(pl.food.startswith(p + " ") or pl.food.endswith(" " + p) for p in ONE_PRODUCT):
        return [pl]
    # "1 tsp salt and pepper" is still both, whatever the amount says
    if SALT_PEPPER.match(pl.food) or ((pl.qty is None or pl.each) and re.search(r" and | & |, ", pl.food)):
        parts = [p.strip() for p in re.split(r",\s*(?:and\s+)?| and | & ", pl.food) if p.strip()]
        if 1 < len(parts) <= 4 and all(len(p.split()) <= 3 for p in parts):
            return [ParsedLine(text=pl.text, qty=pl.qty, unit=pl.unit, food=p, optional=pl.optional) for p in parts]
    return [pl]


def canonicalize(lines: list[ParsedLine], db: DB, llm: LLM | None) -> dict[str, tuple[str, str]]:
    """cleaned food text -> (canonical key, category). Cached in SQLite; LLM only for new text."""
    result: dict[str, tuple[str, str]] = {}
    unknown = []
    for pl in lines:
        if not pl.food or pl.food in result:
            continue
        row = db.one("SELECT food_key, category FROM parse_cache WHERE text = ?", pl.food)
        if row:
            result[pl.food] = (db.resolve(row["food_key"]), row["category"])
        else:
            unknown.append(pl.food)
    unknown = sorted(set(unknown))
    if unknown and llm is not None:
        existing = [r["key"] for r in db.all("SELECT key FROM foods WHERE category != 'none' ORDER BY key")]
        try:
            for i in range(0, len(unknown), 25):
                for o in llm.canonicalize(unknown[i:i + 25], existing=existing):
                    key, cat = norm_key(keep_specific(o["input"], o["item"])), o["category"]
                    db.run("INSERT OR REPLACE INTO parse_cache(text, food_key, category, updated) VALUES (?,?,?,?)",
                           o["input"], key, cat, now_iso())
                    result[o["input"]] = (db.resolve(key), cat)
        except Exception as e:  # noqa: BLE001  model down: fall back to the cleaned text, retry next run
            log.warning("canonicalize failed, using cleaned names: %s", e)
    for f in unknown:
        result.setdefault(f, (f, "other"))
    for key, cat in set(result.values()):
        if cat != "none":
            db.ensure_food(key, cat)
            # a fresh reading replaces a placeholder category from before
            db.run("UPDATE foods SET category = ? WHERE key = ? AND category IN ('none', 'other') AND ? != 'other'",
                   cat, key, cat)
    return result


def gather_needs(meals: list[Meal], recipes: dict[str, Recipe], db: DB, llm: LLM | None) -> tuple[dict[str, Need], list[str]]:
    parsed: list[tuple[Meal, ParsedLine]] = []
    unparsed: list[str] = []
    for m in meals:
        for ing in recipes[m.recipe_id].ingredients:
            if ing.food:  # Mealie already parsed it
                words = norm_key(ing.food).split()
                if words:
                    words[-1] = singular(words[-1])
                pl = ParsedLine(text=ing.text, qty=ing.qty, unit=ing.unit, food=" ".join(words))
            else:
                pl = parse_line(ing.text)
            if not pl.food:
                unread = f"{m.name}: {ing.text}"
                unparsed.append(unread)
                m.unparsed.append(unread)
                continue
            parsed.extend((m, part) for part in split_pairs(pl))
    canon = canonicalize([pl for _, pl in parsed], db, llm)
    needs: dict[str, Need] = {}
    for m, pl in parsed:
        key, cat = canon.get(pl.food, (pl.food, "other"))
        n = needs.setdefault(key, Need(key=key, category=cat))
        amt = pl.amount()
        if amt is not None:
            amt = units.Amount(amt.qty * m.factor, amt.dim)
        n.add(amt, m.name, pl.text, pl.optional)
        if m.key not in n.meal_keys:
            n.meal_keys.append(m.key)
        if amt is not None:
            usage = n.meal_usage.setdefault(m.key, {})
            usage[amt.dim] = usage.get(amt.dim, 0) + amt.qty
    return needs, unparsed


def packages_needed(need: Need, size_qty: float | None, size_dim: str | None, sales_unit: str | None) -> float | None:
    """How many packages the week calls for. None = can't tell (unquantified or unconvertible)."""
    if not need.amounts:
        return None
    if not size_qty or not size_dim:
        size_qty, size_dim = 1.0, "count"
    if (sales_unit or "").upper() == "WEIGHT":
        # sold by weight but carted per item: count whole items
        size_qty, size_dim = 1.0, "count"
    total = 0.0
    unconverted = False
    for dim, qty in need.amounts.items():
        if dim == "pkg":                 # "1 can crushed tomatoes" vs a 28 oz can
            total += qty
            continue
        conv = units.convert(units.Amount(qty, dim), size_dim, need.key, need.category)
        if conv is None:
            # "12 lasagna noodles" vs a 16 oz box: one package covers a recipe's worth
            if dim == "count":
                unconverted = True
                continue
            return None if total == 0 else total
        total += conv.qty / size_qty
    if unconverted:
        total = max(total, 1.0)
    return total


def decide(need: Need, pick: dict | None, stock_packages: float | None, staple: bool) -> tuple[str, int]:
    """Return (status, quantity to add to cart)."""
    if need.category == "none":
        return "none", 0
    if staple:
        if stock_packages is not None and stock_packages <= STOCK_LEVELS["low"]:
            return "buy", 1
        return "staple", 0
    if pick is None:
        return "nopick", 0
    pk = packages_needed(need, pick.get("size_qty"), pick.get("size_dim"), pick.get("sales_unit"))
    if pk is None:
        # "oil for frying", "salt to taste": depends on what's in the house
        if stock_packages is None:
            return ("check", 0) if need.category in ASK_CATEGORIES else ("buy", 1)
        return ("covered", 0) if stock_packages > STOCK_LEVELS["low"] else ("buy", 1)
    if stock_packages is None and need.category in ASK_CATEGORIES:
        return "check", 0
    have = stock_packages or 0.0
    qty = math.ceil(max(pk - have, 0.0) - 1e-6)
    # shelf-stable things get restocked when the week would leave them low
    # ("oil for frying" with a low bottle, a nearly empty jar of honey)
    if qty == 0 and need.category in ASK_CATEGORIES:
        if have - pk <= STOCK_LEVELS["low"] + 1e-9:
            qty = 1
    if qty <= 0:
        return "covered", 0
    return "buy", qty

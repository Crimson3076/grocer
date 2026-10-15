"""Unit handling: normalise recipe and package quantities to ml, g or count.

Everything is reduced to one of three dimensions:
  volume -> millilitres, weight -> grams, count -> items
Conversion between dimensions uses rough densities and average item weights.
Accuracy only needs to be good enough to decide how many packages to buy.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

VOLUME = {
    "ml": 1.0, "milliliter": 1.0, "millilitre": 1.0,
    "l": 1000.0, "liter": 1000.0, "litre": 1000.0,
    "tsp": 4.929, "teaspoon": 4.929, "t": 4.929,
    "tbsp": 14.787, "tablespoon": 14.787, "tbs": 14.787, "tbl": 14.787,
    "cup": 236.6, "c": 236.6,
    "fl oz": 29.57, "fluid ounce": 29.57,
    "pint": 473.2, "pt": 473.2,
    "quart": 946.4, "qt": 946.4,
    "gallon": 3785.4, "gal": 3785.4,
    "pinch": 0.31, "dash": 0.62,
}
WEIGHT = {
    "g": 1.0, "gram": 1.0, "kg": 1000.0, "kilogram": 1000.0,
    "oz": 28.35, "ounce": 28.35,
    "lb": 453.6, "lbs": 453.6, "pound": 453.6,
}
# units that mean "one store package"; matched straight to a package count
CONTAINER = {"can", "jar", "package", "pkg", "pack", "pk", "box", "bag", "bottle", "carton", "container", "loaf"}
COUNT = {
    "each": 1, "ea": 1, "piece": 1, "pc": 1, "whole": 1, "ct": 1, "count": 1,
    "clove": 1, "slice": 1, "filet": 1, "fillet": 1, "breast": 1, "thigh": 1,
    "drumstick": 1, "can": 1, "jar": 1, "package": 1, "pkg": 1, "pack": 1, "pk": 1,
    "bunch": 1, "head": 1, "stalk": 1, "sprig": 1, "stick": 1, "bag": 1, "bottle": 1,
    "box": 1, "loaf": 1, "carton": 1, "container": 1, "bun": 1, "tortilla": 1, "egg": 1, "large": 1, "medium": 1, "small": 1,
}

# grams per millilitre, used when a recipe gives volume and the package gives weight (or vice versa)
DENSITY = {
    "oil": 0.92, "olive oil": 0.92, "vegetable oil": 0.92, "butter": 0.96, "honey": 1.42,
    "maple syrup": 1.32, "soy sauce": 1.15, "water": 1.0, "milk": 1.03, "cream": 1.0,
    "heavy cream": 1.0, "sour cream": 1.0, "flour": 0.53, "sugar": 0.85, "brown sugar": 0.9,
    "salt": 1.2, "kosher salt": 0.6, "rice": 0.85, "cheese": 0.48, "shredded cheese": 0.48,
    "cheddar cheese": 0.48, "parmesan": 0.4, "breadcrumbs": 0.45, "panko": 0.25,
    "ketchup": 1.15, "bbq sauce": 1.15, "mayonnaise": 0.95, "cornstarch": 0.55,
    # loose chopped or shredded produce is light for its volume
    "cilantro": 0.17, "parsley": 0.17, "basil": 0.15, "spinach": 0.12, "lettuce": 0.25,
    "iceberg lettuce": 0.25, "green onion": 0.4, "onion": 0.65, "bell pepper": 0.6, "tomato": 0.75,
    "grape tomato": 0.65, "jalapeno": 0.6, "carrot": 0.55, "celery": 0.5, "mushroom": 0.3,
}
SPICE_DENSITY = 0.5

# average grams per whole item, for produce and countable things
ITEM_WEIGHT = {
    "onion": 225, "yellow onion": 225, "red onion": 200, "garlic": 50,  # a bulb
    "jalapeno": 14,
    "tomato": 120, "plum tomato": 62, "lime": 67, "lemon": 100, "potato": 213,
    "sweet potato": 130, "bell pepper": 170, "carrot": 60, "avocado": 150, "egg": 50,
    "celery": 40, "cucumber": 300, "zucchini": 200, "chicken breast": 225,
    "chicken thigh": 115, "chicken drumstick": 110, "salmon": 170, "salmon filet": 170,
    "butter": 113,  # one stick
    "iceberg lettuce": 540, "lettuce": 540, "cilantro": 50, "parsley": 50, "green onion": 15,
    "bun": 60, "hamburger bun": 60, "tortilla": 25, "corn tortilla": 25, "flour tortilla": 45,
}

UNIT_ALIASES = {
    "tablespoons": "tbsp", "tbsps": "tbsp", "tbsp.": "tbsp", "tablespoon": "tbsp", "tb": "tbsp",
    "teaspoons": "tsp", "tsps": "tsp", "tsp.": "tsp", "teaspoon": "tsp",
    "cups": "cup", "ounces": "oz", "ounce": "oz", "oz.": "oz", "pounds": "lb", "pound": "lb",
    "lbs.": "lb", "lb.": "lb", "lbs": "lb", "grams": "g", "gram": "g", "kilograms": "kg",
    "milliliters": "ml", "millilitres": "ml", "liters": "l", "litres": "l",
    "fluid ounces": "fl oz", "fluid ounce": "fl oz", "fl. oz": "fl oz", "fl.oz": "fl oz",
    "pints": "pint", "quarts": "quart", "gallons": "gallon", "pinches": "pinch", "dashes": "dash",
    "cloves": "clove", "slices": "slice", "cans": "can", "jars": "jar", "packages": "package",
    "packs": "pack", "bunches": "bunch", "heads": "head", "stalks": "stalk", "sprigs": "sprig",
    "sticks": "stick", "filets": "filet", "fillets": "fillet", "breasts": "breast",
    "thighs": "thigh", "drumsticks": "drumstick", "pieces": "piece", "counts": "count",
}


def canon_unit(unit: str | None) -> str | None:
    if not unit:
        return None
    u = unit.strip().lower()
    u = UNIT_ALIASES.get(u, u)
    if u.endswith(".") and u[:-1] in VOLUME | WEIGHT | COUNT:
        u = u[:-1]
    if u in VOLUME or u in WEIGHT or u in COUNT:
        return u
    return None


def dimension(unit: str | None) -> str:
    u = canon_unit(unit)
    if u is None:
        return "count"
    if u in VOLUME:
        return "volume"
    if u in WEIGHT:
        return "weight"
    return "count"


@dataclass
class Amount:
    qty: float  # in base units: ml, g, count, or store packages
    dim: str    # volume | weight | count | pkg

    def __add__(self, other: "Amount") -> "Amount":
        assert self.dim == other.dim
        return Amount(self.qty + other.qty, self.dim)


def to_base(qty: float, unit: str | None) -> Amount:
    u = canon_unit(unit)
    if u is None:
        return Amount(qty, "count")
    if u in VOLUME:
        return Amount(qty * VOLUME[u], "volume")
    if u in WEIGHT:
        return Amount(qty * WEIGHT[u], "weight")
    if u in CONTAINER:
        return Amount(qty, "pkg")
    if u == "clove":  # a garlic clove; bulbs are what you buy
        return Amount(qty * 5.0, "weight")
    if u == "stick":  # butter sticks are a weight, really
        return Amount(qty * 113.0, "weight")
    return Amount(qty * COUNT[u], "count")


def _lookup(table: dict, food: str) -> float | None:
    f = food.lower()
    if f in table:
        return table[f]
    # longest key contained in the food name ("shredded cheddar cheese" -> "cheddar cheese")
    best = None
    for key in table:
        if key in f and (best is None or len(key) > len(best)):
            best = key
    return table[best] if best else None


def density(food: str, category: str | None = None) -> float:
    d = _lookup(DENSITY, food)
    if d is not None:
        return d
    if category == "spice":
        return SPICE_DENSITY
    return 1.0


def item_weight(food: str) -> float | None:
    return _lookup(ITEM_WEIGHT, food)


def convert(amount: Amount, target_dim: str, food: str, category: str | None = None) -> Amount | None:
    """Convert an amount to another dimension. Returns None if no sensible conversion exists."""
    if amount.dim == target_dim:
        return amount
    dens = density(food, category)
    each = item_weight(food)
    grams: float | None = None
    if amount.dim == "weight":
        grams = amount.qty
    elif amount.dim == "volume":
        grams = amount.qty * dens
    elif amount.dim == "count" and each:
        grams = amount.qty * each
    if grams is None:
        return None
    if target_dim == "weight":
        return Amount(grams, "weight")
    if target_dim == "volume":
        return Amount(grams / dens, "volume")
    if target_dim == "count" and each:
        return Amount(grams / each, "count")
    return None


# ---------- package sizes from product titles ----------

_SIZE_RE = re.compile(
    r"(?P<n>\d+(?:\.\d+)?)\s*(?:-\s*)?(?P<u>fl\.?\s?oz|fluid ounces?|oz|ounces?|lbs?|pounds?|kg|g|grams?|ml|l|liters?|litres?|gal(?:lon)?s?|qt|quarts?|pt|pints?|ct|count|pk|pack|each|ea)\b",
    re.I,
)
_MULTI_RE = re.compile(r"(?P<k>\d+)\s*(?:x|pack of|ct of|-pack|pk)\s*(?P<n>\d+(?:\.\d+)?)\s*(?P<u>fl\.?\s?oz|oz|g|ml|lb)", re.I)


def package_size(title: str) -> Amount | None:
    """Best-effort package size from a listing title, e.g. 'Great Value Olive Oil, 51 fl oz'."""
    t = title.replace("\u00a0", " ")
    m = _MULTI_RE.search(t)
    if m:
        unit = re.sub(r"\s+", " ", m.group("u").lower().replace(".", ""))
        base = to_base(float(m.group("n")), unit)
        return Amount(base.qty * int(m.group("k")), base.dim)
    found = list(_SIZE_RE.finditer(t))
    if not found:
        return None
    # prefer weight/volume over count when both appear ("12 ct, 10 oz")
    ranked = sorted(found, key=lambda m: 0 if m.group("u").lower() in {"ct", "count", "pk", "pack", "each", "ea"} else 1)
    m = ranked[-1]
    unit = re.sub(r"\s+", " ", m.group("u").lower().replace(".", ""))
    unit = {"fl oz": "fl oz", "floz": "fl oz"}.get(unit, unit)
    unit = UNIT_ALIASES.get(unit, unit)
    if unit in {"gals", "gal"}:
        unit = "gallon"
    if unit in {"liters", "litres", "liter", "litre"}:
        unit = "l"
    return to_base(float(m.group("n")), unit)


def fmt(amount: Amount) -> str:
    if amount.dim == "pkg":
        q = amount.qty
        return (f"{q:.0f}" if abs(q - round(q)) < 0.05 else f"{q:.1f}") + " pkg"
    if amount.dim == "volume":
        if amount.qty >= 236:
            return f"{amount.qty / 236.6:.1f} cup"
        if amount.qty >= 14:
            return f"{amount.qty / 14.787:.1f} tbsp"
        return f"{amount.qty / 4.929:.1f} tsp"
    if amount.dim == "weight":
        if amount.qty >= 453:
            return f"{amount.qty / 453.6:.1f} lb"
        return f"{amount.qty / 28.35:.1f} oz"
    q = amount.qty
    return f"{q:.0f}" if abs(q - round(q)) < 0.05 else f"{q:.1f}"

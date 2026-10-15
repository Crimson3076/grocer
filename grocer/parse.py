"""Parse free-text recipe ingredient lines like '1/2 medium yellow onion, diced'.

Mealie recipes imported from the web are often unparsed (no food/unit links),
so this does the work deterministically. A local LLM later maps the cleaned
food text to a canonical grocery item (see llm.canonicalize), with caching.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from . import units

WORD_NUMBERS = {"a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
                "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12, "half": 0.5, "dozen": 12}
SIZE_WORDS = {"small", "medium", "large", "extra-large", "jumbo", "big"}
PREP_WORDS = {
    "diced", "minced", "chopped", "sliced", "peeled", "seeded", "grated", "crushed", "melted",
    "softened", "beaten", "divided", "drained", "rinsed", "trimmed", "cubed", "halved", "quartered",
    "finely", "roughly", "thinly", "freshly", "fresh", "packed", "heaping", "level", "room", "temperature",
    "cooked", "uncooked", "boneless", "skinless", "about", "approximately", "plus", "more",
}
DROP_PHRASES = [
    r"\bfor (?:frying|serving|garnish|greasing|the pan|dusting)\b", r"\bto taste\b", r"\bas needed\b",
    r"\bif desired\b", r"\boptional\b", r"\(optional\)", r"\bor more\b", r"\bdivided\b",
]
NO_SINGULAR = {"molasses", "hummus", "asparagus", "swiss", "couscous", "lettuce", "grits", "oats",
               "brussels", "chips", "noodles", "pants", "series", "species", "cheese", "rice"}

_NUM = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d*\.\d+|\d+)"
_QTY_RE = re.compile(rf"^(?P<q1>{_NUM})(?:\s*(?:-|to|–)\s*(?P<q2>{_NUM}))?\s*")
_PAREN_EACH_RE = re.compile(rf"\(\s*(?P<n>{_NUM})\s*-?\s*(?P<u>[a-zA-Z. ]+?)\s+each\s*\)", re.I)
_PAREN_AMT_RE = re.compile(rf"\(\s*(?P<n>{_NUM})\s*-?\s*(?P<u>fl\.? ?oz|oz|ounces?|lbs?|pounds?|g|grams?|ml|cups?)\.?\s*\)", re.I)


@dataclass
class ParsedLine:
    text: str
    qty: float | None
    unit: str | None
    food: str            # cleaned food text, e.g. "yellow onion"
    optional: bool = False
    each: bool = False   # "1/4 tsp each salt and pepper": qty is per item

    def amount(self) -> units.Amount | None:
        if self.qty is None:
            return None
        return units.to_base(self.qty, self.unit)


def _num(s: str) -> float:
    s = s.strip()
    if " " in s and "/" in s:
        whole, frac = s.split(None, 1)
        return float(whole) + _num(frac)
    if "/" in s:
        a, b = s.split("/", 1)
        return float(a) / float(b) if float(b) else 0.0
    return float(s)


def _normalise(text: str) -> str:
    t = text.strip().replace("\u00a0", " ")
    t = re.sub(r"(\d)([½¼¾⅓⅔⅛⅜⅝⅞])", r"\1 \2", t)           # 1½ -> 1 ½
    # NFKD turns ½ into 1⁄2, ³/₄ into 3/4 and jalapeño into jalapen + accent
    t = unicodedata.normalize("NFKD", t)
    t = "".join(c for c in t if not unicodedata.combining(c)).replace("\u2044", "/")
    t = re.sub(r"(\d)\s*-\s*(\d+/\d+)", r"\1 \2", t)          # 1-1/2 -> 1 1/2
    t = re.sub(r"^[\-\*\u2022•]\s*", "", t)
    return t


def singular(word: str) -> str:
    w = word.lower()
    if w in NO_SINGULAR or len(w) <= 3:
        return w
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    if w.endswith("oes"):
        return w[:-2]
    if w.endswith(("ches", "shes", "sses", "xes")):
        return w[:-2]
    if w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w


_SYNONYMS = {"barbecue": "bbq", "barbeque": "bbq", "bar-b-q": "bbq"}


def norm_key(text: str) -> str:
    """One spelling per ingredient: 'All-Purpose Flour' and 'all purpose flour' are the same thing."""
    t = text.lower().replace("&", " and ")
    t = re.sub(r"[-_/]+", " ", t)
    t = re.sub(r"[^a-z0-9' ]", " ", t)
    words = [_SYNONYMS.get(w, w) for w in t.split()]
    return " ".join(words)


def clean_food(text: str) -> str:
    t = text.lower()
    t = re.sub(r"\([^)]*\)", " ", t)          # parentheticals
    # "zest from 1 orange", "juice of 2 limes": you buy the fruit
    t = re.sub(r"^\s*(?:the\s+)?(?:zest|juice|zest and juice)\s+(?:from|of)\s+(?:\d+\s+|an?\s+|half\s+(?:an?\s+)?)?", "", t)
    # "salt & pepper", "salt + pepper", "salt and/or pepper" are two things; "thighs/breasts" is a choice
    t = re.sub(r"\s*\band\s*/\s*or\b\s*", " and ", t)
    t = re.sub(r"\s*[&+]\s*", " and ", t)
    t = re.sub(r"\bsalt\s*/\s*pepper\b", "salt and pepper", t)
    t = re.sub(r"(?<=[a-z])\s*/\s*(?=[a-z])", " or ", t)
    for p in DROP_PHRASES:
        t = re.sub(p, " ", t)
    # prep notes usually follow a comma, but "boneless, skinless chicken" puts them first:
    # take the first comma segment that still has a real word in it
    segs = [x.strip() for x in t.split(",")]
    # a real list ends "..., x and y"; "chicken thighs, bone-in, skin-on" is one thing plus prep notes
    if len(segs) >= 2 and re.search(r"\band\b", segs[-1]):
        tail = re.split(r"\s+and\s+", segs[-1].strip(), maxsplit=1)
        items = [x for x in segs[:-1] + tail if x]
        if 3 <= len(items) <= 5 and all(
                x.split() and not (set(x.split()) & PREP_WORDS) and len(x.split()) <= 3 for x in items):
            t = " and ".join(items)
    words: list[str] = []
    for seg in t.split(","):
        seg = re.sub(r"^\s*of\s+", "", seg.strip())
        words = [w for w in seg.split() if w not in PREP_WORDS and w not in SIZE_WORDS]
        while words and words[0] in {"and", "or", "&", "of"}:
            words.pop(0)
        if words:
            break
    while words and words[-1] in {"and", "or", "&"}:
        words.pop()
    # "butter or margarine", "chicken thighs or breasts": buy the first choice
    if "or" in words[1:]:
        cut = words.index("or", 1)
        first, rest = words[:cut], words[cut + 1:]
        # "cheddar or colby jack cheese": the last word belongs to both
        # ...but "ground beef or ground turkey" already names both, so nothing is shared
        if len(rest) > 1 and rest[-1] not in first and rest[0] not in first:
            first = first + [rest[-1]]
        words = first
    words = [re.sub(r"[^a-z0-9\-' ]", "", w) for w in words]
    words = [w for w in words if w]
    if words:
        words[-1] = singular(words[-1])
    return norm_key(" ".join(words).replace("halfandhalf", "half and half"))


def parse_line(text: str) -> ParsedLine:
    raw = text
    t = _normalise(text)
    t = re.sub(r"\bhalf\s*(?:and|&|\+)\s*half\b", "halfandhalf", t, flags=re.I)
    optional = bool(re.search(r"\boptional\b", t, re.I))

    # "1 plum or 1/4 cup grape tomatoes" -> keep the first alternative only
    alt = re.split(rf"\s+or\s+(?={_NUM}\s)", t, maxsplit=1)
    alt_noun = None
    if len(alt) > 1:
        t = alt[0]
        # the food noun usually lives at the end of the second alternative
        second = clean_food(alt[1])
        alt_noun = second.split()[-1] if second else None

    qty: float | None = None
    unit: str | None = None

    m = _QTY_RE.match(t)
    if m:
        q1 = _num(m.group("q1"))
        q2 = _num(m.group("q2")) if m.group("q2") else None
        qty = max(q1, q2) if q2 else q1
        rest = t[m.end():]
    else:
        first, _, after = t.partition(" ")
        if first.lower() in WORD_NUMBERS:
            qty = float(WORD_NUMBERS[first.lower()])
            rest = after
        elif first.lower() in {"pinch", "dash"}:
            qty, unit, rest = 1.0, first.lower(), after
        else:
            rest = t

    # "4 (6 oz each) salmon filets" -> 24 oz
    pe = _PAREN_EACH_RE.search(rest)
    if qty is not None and pe and units.canon_unit(pe.group("u")):
        each = _num(pe.group("n"))
        qty, unit = qty * each, units.canon_unit(pe.group("u"))
        rest = rest[: pe.start()] + rest[pe.end():]
    elif qty is not None and unit is None:
        # unit word(s) right after the quantity
        tokens = rest.split()
        for n in (2, 1):
            cand = " ".join(tokens[:n]).rstrip(".,")
            cu = units.canon_unit(cand)
            if cu and cu not in SIZE_WORDS:
                unit = cu
                rest = " ".join(tokens[n:])
                break
        # "2 cups (8 ounces) shredded cheese" -> prefer the weight in parentheses
        pa = _PAREN_AMT_RE.search(rest)
        if pa and units.dimension(unit) == "volume":
            pu = units.canon_unit(pa.group("u"))
            if pu and units.dimension(pu) == "weight":
                qty, unit = _num(pa.group("n")), pu

    # "1/4 tsp each salt and pepper": the amount applies to each item
    each = False
    m_each = re.match(r"\s*(?:of\s+)?each\s+(?:of\s+)?", rest, re.I)
    if m_each:
        each, rest = True, rest[m_each.end():]
    if each:
        # a list follows: clean each item on its own (the usual comma rule would cut the list)
        body = re.sub(r"\([^)]*\)", " ", rest)
        items = [clean_food(x) for x in re.split(r",\s*(?:and\s+)?|\s+and\s+|\s+&\s+", body)]
        food = " and ".join(x for x in items if x)
    else:
        food = clean_food(rest)
    # "1 plum or 1/4 cup grape tomatoes" -> "plum tomato"
    if alt_noun and len(food.split()) == 1 and food != alt_noun:
        food = f"{food} {alt_noun}"
    if qty is not None and qty <= 0:
        qty = None
    return ParsedLine(text=raw, qty=qty, unit=unit, food=food, optional=optional, each=each)

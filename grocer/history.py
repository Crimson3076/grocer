"""Match recipe ingredients to things the household has bought before."""
from __future__ import annotations

import re

from .parse import singular

STOP = {"of", "and", "or", "the", "a", "with", "fresh", "great", "value", "oz", "lb", "fl", "ct", "count", "pack"}


def _tokens(text: str) -> set[str]:
    words = re.findall(r"[a-z]+", text.lower())
    return {singular(w) for w in words if w not in STOP and len(w) > 1}


def candidates(key: str, purchases: list[dict], limit: int = 8) -> list[dict]:
    """Past purchases whose titles contain the ingredient's words, best first."""
    want = _tokens(key)
    if not want:
        return []
    scored = []
    for p in purchases:
        have = _tokens(p.get("title") or "")
        hit = len(want & have) / len(want)
        # every word for 1-2 word items ("ketchup", "corn tortilla"); most words for longer ones
        if hit >= 1.0 or (len(want) >= 3 and hit >= 0.66):
            scored.append((hit, p.get("seen") or 1, p))
    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [dict(p, bought_before=True) for _, _, p in scored[:limit]]


def near_misses(key: str, purchases: list[dict], exclude: set[str], limit: int = 8) -> list[dict]:
    """Past purchases sharing at least one word with the ingredient but not considered a candidate."""
    want = _tokens(key)
    out = []
    for p in purchases:
        if p["item_id"] in exclude:
            continue
        shared = want & _tokens(p.get("title") or "")
        if shared:
            out.append((len(shared), p.get("seen") or 1, dict(p, shared=sorted(shared))))
    out.sort(key=lambda t: (t[0], t[1]), reverse=True)
    return [p for _, _, p in out[:limit]]

"""Conservative adapters for Walmart's page data. Unknown stock is never local stock.

Walmart does not provide this app with a supported inventory API. These adapters
accept explicit fulfillment records and reject unrecognized page layouts.
"""
from __future__ import annotations

import json
import re

AVAILABLE = {"IN_STOCK", "AVAILABLE"}


def value(v) -> str:
    if isinstance(v, dict):
        v = v.get("value") or v.get("availabilityStatus") or v.get("status")
    return str(v or "").upper()


def selected_store(data: dict, fulfillment: str = "PICKUP") -> str | None:
    """Only named session/location records count. Product recommendations do not."""
    wanted = {"pickupstore", "selectedstore"} if fulfillment == "PICKUP" else {"deliverystore", "deliveryfromstore"}
    found = set()

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if key.lower() in wanted and isinstance(child, dict):
                    sid = child.get("storeId") or child.get("id") or child.get("storeNumber")
                    if sid is not None and str(sid).isdigit():
                        found.add(str(sid))
                elif key.lower() not in {"product", "products", "items", "itemstacks", "modules", "recommendations"}:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)

    walk(data)
    return next(iter(found)) if len(found) == 1 else None


def local_stock(node: dict, store_id: str, fulfillment: str, session_store: str | None) -> tuple[bool, str]:
    if not store_id:
        return False, "Choose a Walmart store first"
    if session_store and session_store != store_id:
        return False, "The Walmart browser is using a different store"
    mode = fulfillment.upper()
    records = []
    for field in ("fulfillmentOptions", "fulfillmentSummary"):
        entries = node.get(field)
        if isinstance(entries, list):
            records.extend(e for e in entries if isinstance(e, dict))
    matched = False
    for option in records:
        kind = value(option.get("type") or option.get("fulfillmentType") or option.get("fulfillment"))
        if kind != mode:
            continue
        # Reject an unscoped global delivery record or stock at another store.
        nested_store = option.get("store")
        store = option.get("storeId") or (nested_store.get("storeId") if isinstance(nested_store, dict) else None)
        if str(store or session_store or "") != store_id:
            continue
        # DELIVERY must explicitly be store fulfillment, never ordinary parcel shipping.
        method = value(option.get("fulfillmentMethod") or option.get("accessType"))
        if mode == "DELIVERY" and method in {"SHIPPING", "SHIP", "FC"}:
            continue
        matched = True
        stock = value(option.get("availabilityStatus") or option.get("availability") or option.get("availabilityStatusV2"))
        # Walmart's fulfillmentSummary lists the methods a store offers without any status: listed means offered
        if stock in AVAILABLE or not stock:
            return True, f"Available for {mode.lower()} at store #{store_id}"
    if matched:
        return False, f"Unavailable for {mode.lower()} at store #{store_id}"
    kinds = {value(o.get("type") or o.get("fulfillmentType") or o.get("fulfillment")) for o in records}
    if kinds and kinds <= {"SHIPPING", "SHIP", "FC"}:
        return False, "Unavailable locally: this one only ships"
    return False, f"Local {mode.lower()} availability could not be verified for store #{store_id}"


def _cart_roots(data: dict) -> list[dict]:
    carts = []

    def find(node):
        if isinstance(node, dict):
            for key, child in node.items():
                if re.search(r"recommend|sponsor|carousel|similar|p13n", key, re.I):
                    continue
                if key.lower() in {"cart", "cartdata", "getcart", "cartresponse"} and isinstance(child, dict):
                    carts.append(child)
                else:
                    find(child)
        elif isinstance(node, list):
            for child in node:
                find(child)

    find(data)
    return carts


def cart_quantities(data: dict) -> dict[str, int] | None:
    """Read explicit cart line items, never recommendation IDs from the whole page.

    None means unverifiable. {} is a positively identified empty cart.
    """
    carts = _cart_roots(data)
    if not carts:
        return None
    snapshots = []
    for cart in carts:
        totals, seen = {}, set()
        recognized, invalid = False, False

        def lines(node):
            nonlocal recognized, invalid
            if isinstance(node, dict):
                product = node.get("product") if isinstance(node.get("product"), dict) else {}
                iid = node.get("usItemId") or product.get("usItemId")
                if iid is not None:
                    recognized = True
                    qty = node.get("quantity")
                    if not str(iid).isdigit() or isinstance(qty, bool) or not isinstance(qty, (int, float)) or qty < 0 or int(qty) != qty:
                        invalid = True
                    else:
                        line_id = node.get("lineItemId") or node.get("id")
                        identity = str(line_id) if line_id else None
                        if identity is None or identity not in seen:
                            totals[str(iid)] = totals.get(str(iid), 0) + int(qty)
                        if identity:
                            seen.add(identity)
                    return
                for key, child in node.items():
                    if re.search(r"recommend|sponsor|carousel|similar|p13n", key, re.I):
                        continue
                    if key.lower() in {"lineitems", "items", "cartitems"} and child == []:
                        recognized = True
                    lines(child)
            elif isinstance(node, list):
                for child in node:
                    lines(child)

        lines(cart)
        if not recognized or invalid:
            return None
        snapshots.append(totals)
    # Conflicting snapshots indicate stale or ambiguous cart data.
    return snapshots[0] if all(s == snapshots[0] for s in snapshots) else None


def cart_item_checks(data: dict, store_id: str, mode: str) -> dict[str, dict]:
    """Inspect only real cart lines. Presence alone does not establish local stock."""
    observations: dict[str, list[dict]] = {}
    session = selected_store(data, mode)
    context_keys = {"storeId", "fulfillmentType", "fulfillmentMethod", "accessType"}

    def walk(node, inherited):
        if isinstance(node, list):
            for child in node:
                walk(child, inherited)
        elif isinstance(node, dict):
            context = dict(inherited, **{k: v for k, v in node.items() if k in context_keys})
            product = node.get("product") if isinstance(node.get("product"), dict) else {}
            iid = node.get("usItemId") or product.get("usItemId")
            if iid is not None and "quantity" in node:
                combined = {**context, **product, **node}
                status = value(combined.get("availabilityStatusV2") or combined.get("availabilityStatus"))
                kind = value(context.get("fulfillmentType") or product.get("fulfillmentType"))
                method = value(context.get("fulfillmentMethod") or context.get("accessType")
                               or product.get("fulfillmentMethod") or product.get("accessType"))
                reason, available = "Check pickup or delivery availability in Walmart", None
                if status in {"OUT_OF_STOCK", "UNAVAILABLE", "NOT_AVAILABLE"} or combined.get("isAvailable") is False:
                    available, reason = False, "Walmart says this item is unavailable"
                elif combined.get("isMarketplaceItem") or value(combined.get("offerType")) in {"MARKETPLACE", "ONLINE_ONLY"}:
                    available, reason = False, "This cart item is from another seller"
                elif kind in {"SHIPPING", "SHIP", "FC"} or method in {"SHIPPING", "SHIP", "FC"}:
                    available, reason = False, "This cart item is set to shipping"
                elif kind in {"PICKUP", "DELIVERY"} and kind != mode:
                    available, reason = False, "This cart item uses a different pickup or delivery method"
                elif (session and session != store_id) or (context.get("storeId") and str(context["storeId"]) != store_id):
                    available, reason = False, "The cart is using a different store"
                else:
                    if kind == mode and context.get("storeId"):
                        combined["fulfillmentOptions"] = [{"type": kind, "storeId": context["storeId"],
                                                           "availabilityStatus": status or "AVAILABLE",
                                                           "fulfillmentMethod": method}]
                    local, local_reason = local_stock(combined, store_id, mode, session)
                    timing = fulfillment(combined, mode)
                    if local and timing["ok"]:
                        available, reason = True, timing["label"]
                    elif "only ships" in local_reason or "Unavailable for" in local_reason or (timing.get("seller") and "walmart" not in timing["seller"].lower()):
                        available, reason = False, timing["label"] if timing.get("seller") and "walmart" not in timing["seller"].lower() else local_reason
                observations.setdefault(str(iid), []).append({"available": available, "reason": reason})
                return
            for key, child in node.items():
                if not re.search(r"recommend|sponsor|carousel|similar|p13n", key, re.I):
                    walk(child, context)

    for cart in _cart_roots(data):
        walk(cart, {})
    result = {}
    for iid, entries in observations.items():
        rejected = next((e for e in entries if e["available"] is False), None)
        result[iid] = rejected or (entries[0] if all(e["available"] is True for e in entries)
                                  else {"available": None, "reason": "Check pickup or delivery availability in Walmart"})
    return result


# ---------------------------------------------------------------- can we get it today or tomorrow?

_FF_KEY = re.compile(r"fulfil|badge|sla|deliver|pickup|ship|arriv|intent|availab|speed", re.I)
_WHEN = [("today", re.compile(r"\btoday\b|\btonight\b|\bin \d+\s*(?:min|minutes|hr|hrs|hour|hours)\b|as soon as|same.?day", re.I)),
         ("tomorrow", re.compile(r"\btomorrow\b|next.?day|\b1 day\b", re.I))]
_LATER = re.compile(r"\b(?:[2-9]|\d{2,})\+?\s*(?:-\s*\d+\s*)?days?\b|\bmon|\btue|\bwed|\bthu|\bfri|\bsat|\bsun|"
                    r"\bjan|\bfeb|\bmar|\bapr|\bmay\b|\bjun|\bjul|\baug|\bsep|\boct|\bnov|\bdec", re.I)


def _phrases(node, depth: int = 0, inside: bool = False) -> list[str]:
    """Short text badges about pickup/delivery/shipping, e.g. "Pickup today", "Free shipping, arrives in 3 days"."""
    out: list[str] = []
    if depth > 6:
        return out
    if isinstance(node, dict):
        mine = [v for k, v in node.items() if isinstance(v, str) and 0 < len(v) < 80
                and (inside or _FF_KEY.search(k)) and not v.startswith("http")]
        if mine:
            out.append(" ".join(mine))
        for k, v in node.items():
            if isinstance(v, (dict, list)) and (inside or _FF_KEY.search(str(k))):
                out += _phrases(v, depth + 1, True)
            elif isinstance(v, (dict, list)) and depth < 1 and str(k) not in {"product", "products", "items"}:
                out += _phrases(v, depth + 1, False)
    elif isinstance(node, list):
        for v in node:
            out += _phrases(v, depth + 1, inside)
    return out


def _seller(node: dict) -> str:
    s = node.get("sellerName") or node.get("sellerDisplayName") or ""
    if not s and isinstance(node.get("seller"), dict):
        s = node["seller"].get("name") or node["seller"].get("displayName") or ""
    if not s and isinstance(node.get("sellerInfo"), dict):
        s = node["sellerInfo"].get("sellerName") or ""
    return str(s)


def _when(text: str) -> str | None:
    for word, rx in _WHEN:
        if rx.search(text):
            return word
    return "later" if _LATER.search(text) else None


def fulfillment(node: dict, requested: str | None = None) -> dict:
    """How soon this listing can be had, from whatever Walmart put in its data.

    ok is True only for pickup or delivery today/tomorrow,
    sold by Walmart itself. Marketplace sellers and anything with no such evidence are not ok."""
    seller = _seller(node)
    kind = str(node.get("fulfillmentType") or node.get("offerType") or "").upper()
    marketplace = (bool(seller) and "walmart" not in seller.lower()) or bool(node.get("isMarketplaceItem")) \
        or kind in {"MARKETPLACE", "ONLINE_ONLY"}
    best: dict | None = None
    order = {"pickup": 0, "delivery": 1, "shipping": 2}

    def offer(method: str, when: str | None, label: str) -> None:
        nonlocal best
        if method == "shipping" or (requested and method != requested.lower()):
            return
        if when not in {"today", "tomorrow"}:
            return
        cand = {"method": method, "when": when, "label": label}
        if best is None or (when, order[method]) < (best["when"], order[best["method"]]):
            best = cand

    phrases = _phrases(node)
    ship_later = None
    for p in phrases:
        low = p.lower()
        when = _when(low)
        if "pickup" in low or "pick up" in low or "curbside" in low:
            offer("pickup", when, f"Pickup {when}" if when in {"today", "tomorrow"} else "Pickup")
        elif "deliver" in low and "ship" not in low:
            offer("delivery", when, f"Delivery {when}" if when in {"today", "tomorrow"} else "Delivery")
        elif "ship" in low or "arrives" in low:
            if when in {"today", "tomorrow"}:
                offer("shipping", when, f"Shipping, arrives {when}")
            else:
                ship_later = p.strip()

    # structured records without a day: pickup/delivery available at a store counts as today/tomorrow
    if best is None and not any(_when(p) is not None for p in phrases):
        for field in ("fulfillmentOptions", "fulfillmentSummary"):
            for o in node.get(field) or []:
                if not isinstance(o, dict):
                    continue
                t = value(o.get("type") or o.get("fulfillmentType") or o.get("fulfillment"))
                status = value(o.get("availabilityStatus") or o.get("availability") or o.get("availabilityStatusV2"))
                method = value(o.get("fulfillmentMethod") or o.get("accessType"))
                if t in {"PICKUP", "DELIVERY"} and (status in AVAILABLE or not status) \
                        and method not in {"SHIPPING", "SHIP", "FC"}:
                    offer(t.lower(), "tomorrow", "Pickup" if t == "PICKUP" else "Delivery")

    if marketplace:
        return {"ok": False, "method": None, "when": None, "seller": seller,
                "label": f"Sold and shipped by {seller}" if seller else "Ships from another seller"}
    if best:
        return {"ok": True, **best, "seller": seller or "Walmart"}
    return {"ok": False, "method": "shipping" if ship_later else None, "when": "later" if ship_later else None,
            "seller": seller, "label": f"Shipping only ({ship_later})" if ship_later else "No pickup or delivery today or tomorrow"}


def describe_keys(node: dict) -> dict:
    """For the log: the fulfillment-ish fields of one listing, so the parser can be checked against real data."""
    return {k: (v if isinstance(v, (str, int, float, bool)) else json.dumps(v)[:300])
            for k, v in node.items() if _FF_KEY.search(k) or k in {"sellerName", "sellerId", "offerType"}}

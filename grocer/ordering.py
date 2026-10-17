"""Durable, resumable cart work. Browser actions never count as purchases."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from .cart import deficits, plan_line, targets
from .db import now_iso


def cached_availability(db, iid, locally_available, minutes, settings):
    """Only actual, recent observations count, never a pick edit timestamp."""
    now = datetime.now(timezone.utc)
    for row in db.all("SELECT * FROM picks WHERE item_id=? ORDER BY availability_checked_at DESC", str(iid)):
        pick = dict(row)
        if (str(pick.get("store_id") or "") != settings["store_id"]
                or pick.get("fulfillment") != settings["fulfillment"]):
            continue
        try:
            checked = datetime.fromisoformat(pick.get("availability_checked_at") or "")
            age = (now - checked).total_seconds()
        except (TypeError, ValueError):
            return None
        if locally_available(pick) and 0 <= age < minutes * 60 and checked.date() == now.date():
            return pick
        return None
    return None


def fill(force: bool = False) -> dict:
    from . import jobs as j

    row = j.db.latest_review()
    if not row:
        raise RuntimeError("Build a grocery list first")
    review_id, week = row["id"], row["week_start"]
    run = j.db.cart_run(week, review_id)
    if (run and run["state"] in {"carted", "checked_out"}) or row["status"] in {"carted", "checked_out"}:
        return {"skipped": "already carted"}
    if row["status"] == "pending" and not (force or j.CONFIG.auto_cart_if_unreviewed):
        j.ha.notify("Groceries", "The grocery review hasn't been approved yet.", tag=j.REVIEW_TAG,
                    url=f"{j.CONFIG.public_url}/review")
        return {"skipped": "not approved"}
    data = json.loads(row["data"])
    if not run:
        from .trips import require_current
        require_current(data)
    j.configure_browser()
    if data.get("walmart") and data["walmart"] != j.walmart_settings():
        raise RuntimeError("The store has changed. Rebuild the list before filling the cart.")
    if not run and data.get("week_end", week) < j.today().isoformat():
        raise RuntimeError("Those dinners are already past. Update the list first.")
    try:
        j.browser.ensure_logged_in()
        j.browser.ensure_store()
        checked = {}

        def live_item(iid):
            iid = str(iid)
            if iid not in checked:
                saved = j.db.one("SELECT * FROM picks WHERE item_id=? AND local_available=1 AND store_id=? AND fulfillment=? LIMIT 1",
                                 iid, j.walmart_settings()["store_id"], j.walmart_settings()["fulfillment"])
                cached = dict(saved) if not run and j.CONFIG.cart_verify_after_add and saved else None
                cached = cached or (None if run else cached_availability(
                    j.db, iid, j.locally_available, j.CONFIG.cart_availability_minutes, j.walmart_settings()))
                if cached:
                    checked[iid] = cached
                    j.log.info("Cart availability: reusing saved store product for %s", iid)
                else:
                    checked[iid] = j.browser.item(iid)
                    live = checked[iid]
                    # Refresh only actual observations; a retry never uses this cache.
                    if live:
                        j.db.run("""UPDATE picks SET local_available=?, store_id=?, fulfillment=?,
                                    local_reason=?, availability_checked_at=? WHERE item_id=?""",
                                 int(bool(live.get("local_available"))), live.get("store_id"),
                                 live.get("fulfillment"), live.get("local_reason"),
                                 live.get("availability_checked_at"), iid)
                    else:
                        j.db.run("UPDATE picks SET local_available=0, availability_checked_at=NULL WHERE item_id=?", iid)
            return checked[iid]

        if run:
            plan = run["data"]
        else:
            plan = {"lines": [], "swaps": [], "carried": [], "unanswered": [], "no_pick": [],
                    "store": j.walmart_settings(), "verify_after_add": j.CONFIG.cart_verify_after_add}
            for item in data["items"]:
                if item["status"] == "check":
                    plan["unanswered"].append(item["key"])
                    continue
                if item["status"] == "nopick":
                    plan["no_pick"].append(item["key"])
                    continue
                if item["status"] != "buy" or not item.get("pick"):
                    continue
                pick = item["pick"]
                chosen = dict(pick)
                j.progress(step=f"Checking {item['key']} is still available")
                live = live_item(pick["item_id"])
                if j.locally_available(live):
                    chosen.update({k: live[k] for k in ("local_available", "store_id", "fulfillment", "local_reason", "price") if k in live})
                else:
                    food = j.db.food(item["key"])
                    chosen = None if food and food["brand_lock"] else j._substitute(item, pick["item_id"])
                if not chosen or not j.locally_available(chosen):
                    plan["carried"].append(item["key"])
                    continue
                if str(chosen["item_id"]) != str(pick["item_id"]):
                    replacement = live_item(chosen["item_id"])
                    if not j.locally_available(replacement):
                        plan["carried"].append(item["key"])
                        continue
                    chosen = replacement
                line = plan_line(item, chosen)
                if not line:
                    plan["carried"].append(item["key"])
                    continue
                if chosen["item_id"] != pick["item_id"]:
                    plan["swaps"].append(f"{item['key']} -> {chosen['title']} ({line['qty']} packages)")
                    if live is None:
                        j.db.save_pick(item["key"], chosen, approved=False)
                plan["lines"].append(line)
            current = j.browser.cart_contents()
            plan['cart_before'] = dict(current) if current is not None else None
            plan['added_quantities'] = {}
            if current is None:
                # First fill: nothing has been added by us yet, so there is nothing to double.
                # Go ahead, but remember the cart couldn't be read so a retry won't add again blindly.
                j.log.warning("couldn't read the Walmart cart; filling without a quantity check")
                plan["unverified"] = True
                current = {}
            plan["targets"] = targets(plan["lines"], current)
            plan["covered"] = [{"key": i["key"], "used": i.get("use_pk") or 0}
                               for i in data["items"] if i["status"] == "covered" and not i.get("manual_qty")]
            # Commit intent before any browser navigation that could mutate the cart.
            j.db.save_cart_run(week, review_id, "adding", plan)
        fresh = not run
        if not fresh:
            current = j.browser.cart_contents()
        if current is None:
            if not fresh:
                raise RuntimeError("Some items may already be in your cart, and the cart couldn't be read to check. "
                                   "Look at the Walmart cart and add anything missing by hand.")
            current = {}
        remaining = deficits(plan["targets"], current)
        # Reuse saved store products on new fills; retries check only missing quantities live.
        # A retry keeps its saved quantities and refuses unavailable products.
        for iid, _ in remaining:
            if not j.locally_available(live_item(iid)):
                raise RuntimeError("A product is no longer available at your store. Check the Walmart cart before retrying.")
        if not run:
            require_current(data)
        j.progress(step="Adding missing quantities to your Walmart cart")
        j.browser.add_to_cart(remaining)
        after = j.browser.cart_contents()
        plan.pop("unverified", None)
        plan["fulfillment_unverified"] = []
        plan["cart_issues"] = {}
        if after is None:
            plan["unverified"] = True
            missing = []      # Sent, but unconfirmed until the person checks Walmart.
            plan["fulfillment_unverified"] = list(plan["targets"])
        else:
            missing = [iid for iid, _ in deficits(plan["targets"], after)]
        if after is not None and plan.get("verify_after_add"):
            checks = getattr(j.browser, "last_cart_checks", {})
            for iid in plan["targets"]:
                if iid in missing:
                    continue
                check = checks.get(iid, {})
                if check.get("available") is False:
                    missing.append(iid)
                    plan["cart_issues"][iid] = check.get("reason", "Walmart could not fulfill this item")
                    j.db.run("UPDATE picks SET local_available=0, availability_checked_at=NULL WHERE item_id=?", iid)
                elif check.get("available") is not True:
                    plan["fulfillment_unverified"].append(iid)
                else:
                    j.db.run("UPDATE picks SET availability_checked_at=? WHERE item_id=? AND store_id=? AND fulfillment=?",
                             now_iso(), iid, j.walmart_settings()["store_id"], j.walmart_settings()["fulfillment"])
            if plan["fulfillment_unverified"]:
                plan["unverified"] = True
        if plan.get("unverified"):
            plan["verification_note"] = ("Check the quantities and pickup or delivery availability in Walmart before checkout.")
        if after is not None and plan.get('cart_before') is not None:
            added = plan.setdefault('added_quantities', {})
            for iid, qty in remaining:
                confirmed = min(qty, max(after.get(iid, 0) - current.get(iid, 0), 0))
                added[iid] = added.get(iid, 0) + confirmed
        state = "partial" if missing or plan.get("unverified") else "carted"
        plan.pop("error", None)
        plan["missing"] = missing
        plan["added"] = sum(1 for line in plan["lines"] if str(line["pick"]["item_id"]) not in missing)
        plan['unread_lines'] = data.get('unparsed', [])
        plan['left_for_later'] = [i['key'] for i in data['items'] if i['status']=='skip']
        plan["unresolved"] = list(dict.fromkeys(plan["carried"] + plan["unanswered"] + plan["no_pick"] + plan['left_for_later'] + plan['unread_lines']))
        from .trips import coverage
        plan['coverage'] = coverage(data, plan)
        plan["estimated_total"] = round(sum((line["pick"].get("price") or 0) * line["qty"] for line in plan["lines"]), 2)
        plan["unknown_prices"] = [line["key"] for line in plan["lines"] if line["pick"].get("price") is None]
        j.db.save_cart_run(week, review_id, state, plan)
        data["cart_result"] = plan
        j.db.update_review(review_id, data=data, status=state)
        j.db.log("carted", id=review_id, missing=missing, swaps=plan["swaps"])
        j.ha.clear(j.REVIEW_TAG)
        msg = (f"{plan['added']} grocery lines sent to your Walmart cart. Check quantities and pickup or delivery availability before checking out."
               if plan.get("unverified") else
               f"{plan['added']} grocery lines verified in your Walmart cart. Review it and check out manually.")
        if missing or plan["unresolved"] or plan.get("unverified"):
            msg += f" {len(missing) + len(plan['unresolved']) + len(plan.get('fulfillment_unverified', []))} items need attention on the review page."
        j.ha.notify("Grocery cart needs review" if missing or plan["unresolved"] or plan.get("unverified") else "Grocery cart is ready",
                    msg, tag=j.STATUS_TAG, url=f"{j.CONFIG.public_url}/review",
                    actions=[{"action": "URI", "title": "Open cart", "uri": "https://www.walmart.com/cart"},
                             {"action": "GROCER_DONE", "title": "Checked out"}])
        return {k: plan[k] for k in ("added", "swaps", "carried", "unanswered", "missing", "no_pick")}
    except Exception as e:
        run = j.db.cart_run(week, review_id)
        if run:
            plan = run["data"]
            plan["error"] = j._friendly(e)
            j.db.save_cart_run(week, review_id, "needs_check", plan)
            data["cart_result"] = plan
        j.db.update_review(review_id, data=data, status="failed")
        why = j._friendly(e)
        j.ha.notify("Grocery cart needs you", why, tag=j.STATUS_TAG, url=f"{j.CONFIG.public_url}/review",
                    actions=[{"action": "GROCER_RETRY", "title": "Retry"},
                             {"action": "URI", "title": "Open browser", "uri": j.CONFIG.vnc_url}])
        j.raise_alert()
        return {"failed": why}


def confirm_checkout(quantities: dict[str, int] | None = None, manual_keys: list[str] | None = None) -> None:
    from . import jobs as j
    from . import pantry
    from .trips import coverage

    with j.db.tx() as c:
        row = j.db.latest_review()
        if not row:
            raise RuntimeError('No shopping trip to confirm')
        run = j.db.cart_run(row['week_start'], row['id'])
        if run and run['state'] == 'checked_out':
            return
        if not run or (run['state'] != 'carted' and quantities is None):
            raise RuntimeError('Check the cart quantities before confirming checkout.')
        plan = run['data']
        review = json.loads(row['data'])
        original = {line['key']: line for line in plan['lines']}
        manual_keys = set(manual_keys or [])
        if manual_keys - set(plan.get('unresolved', [])):
            raise ValueError('Only confirm manual purchases for unresolved ingredients')
        plan['manual_purchases'] = sorted(manual_keys)
        if quantities is not None:
            if run['state'] != 'carted' and set(quantities) != set(original):
                raise ValueError('Confirm a purchased quantity for every product in this interrupted trip')
            if set(quantities) - set(original):
                raise ValueError('Choose quantities only for products in this trip')
            if any(isinstance(q, bool) or not isinstance(q, int) or q < 0 or q > 999 for q in quantities.values()):
                raise ValueError('Purchased quantities must be whole numbers from 0 to 999')
        bought_lines = []
        for line in plan['lines']:
            qty = quantities.get(line['key'], line['qty']) if quantities is not None else line['qty']
            if qty < line['qty']:
                plan['carried'] = list(set(plan['carried'] + [line['key']]))
            if qty:
                purchased = dict(line, qty=qty)
                pantry.credit(c, purchased, row['trip_id'])
                bought_lines.append(purchased)
        plan['purchased_lines'] = bought_lines
        plan['fulfillment_unverified'] = []
        actual = {line['key']: line['qty'] for line in bought_lines}
        plan['missing'] = [iid for iid in plan.get('missing', []) if any(
            str(line['pick']['item_id']) == str(iid) and actual.get(line['key'], 0) < line['qty']
            for line in plan['lines'])]
        plan['coverage'] = coverage(review, plan)
        pantry.reserve(c, review, row['id'])
        complete = [m['key'] for m in plan['coverage'] if m['complete']]
        from . import dinners
        dinners.mark_ordered(complete, review.get('meals', []), row['id'], c)
        for key in manual_keys:
            j.db.set_restock(key, False)
        for key in {k for m in plan['coverage'] for k in m['missing']}:
            # Carry actual grocery keys forward; human-readable warnings are not foods.
            if key in {i['key'] for i in review['items']}:
                if not j.db.stock(key):
                    j.db.set_stock(key, 0, 0.5)
                j.db.set_restock(key, True)
        c.execute("UPDATE cart_runs SET state='checked_out', data=?, updated=? WHERE week_start=?",
                  (json.dumps(plan), now_iso(), run['week_start']))
        review['cart_result'] = plan
        j.db.update_review(row['id'], data=review, status='checked_out')
    j.ha.clear(j.STATUS_TAG)
    j.changed()
    j.db.log('checked_out', trip=row['trip_id'])

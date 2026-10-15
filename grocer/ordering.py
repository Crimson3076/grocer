"""Durable, resumable cart work. Browser actions never count as purchases."""
from __future__ import annotations

import json

from .cart import deficits, plan_line, targets
from .db import now_iso


def fill(force: bool = False) -> dict:
    from . import jobs as j

    row = j.db.latest_review()
    if not row:
        raise RuntimeError("Build a grocery list first")
    review_id, week = row["id"], row["week_start"]
    run = j.db.cart_run(week)
    if (run and run["state"] in {"carted", "checked_out"}) or row["status"] in {"carted", "checked_out"}:
        return {"skipped": "already carted"}
    if row["status"] == "pending" and not (force or j.CONFIG.auto_cart_if_unreviewed):
        j.ha.notify("Groceries", "The grocery review hasn't been approved yet.", tag=j.REVIEW_TAG,
                    url=f"{j.CONFIG.public_url}/review")
        return {"skipped": "not approved"}
    data = json.loads(row["data"])
    j.configure_browser()
    if data.get("walmart") and data["walmart"] != j.walmart_settings():
        raise RuntimeError("The store has changed. Rebuild the list before filling the cart.")
    if not run and data.get("week_end", week) < j.today().isoformat():
        raise RuntimeError("Those dinners are already past. Update the list first.")
    try:
        j.browser.ensure_logged_in()
        j.browser.ensure_store()
        if run:
            plan = run["data"]
        else:
            plan = {"lines": [], "swaps": [], "carried": [], "unanswered": [], "no_pick": [],
                    "store": j.walmart_settings()}
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
                saved = j.db.pick(item["key"])
                if saved and str(saved["item_id"]) == str(pick["item_id"]) and j.recently_checked(item["key"], 7 * 24):
                    plan["lines"].append(plan_line(item, chosen) or {})   # checked today; the cart read-back catches the rest
                    if not plan["lines"][-1]:
                        plan["lines"].pop()
                        plan["carried"].append(item["key"])
                    continue
                j.progress(step=f"Checking {item['key']} is still available")
                live = j.browser.item(pick["item_id"])
                if j.locally_available(live):
                    chosen.update({k: live[k] for k in ("local_available", "store_id", "fulfillment", "local_reason", "price") if k in live})
                else:
                    food = j.db.food(item["key"])
                    chosen = None if food and food["brand_lock"] else j._substitute(item, pick["item_id"])
                if not chosen or not j.locally_available(chosen):
                    plan["carried"].append(item["key"])
                    continue
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
        current = j.browser.cart_contents()
        if current is None:
            if not fresh:
                raise RuntimeError("Some items may already be in your cart, and the cart couldn't be read to check. "
                                   "Look at the Walmart cart and add anything missing by hand.")
            current = {}
        remaining = deficits(plan["targets"], current)
        # no second product-page check here: reading the cart back afterwards shows anything that didn't go in
        j.progress(step="Adding missing quantities to your Walmart cart")
        j.browser.add_to_cart(remaining)
        after = j.browser.cart_contents()
        if after is None:
            plan["unverified"] = True
            missing = []      # sent, but we can't see the cart to confirm; the message says to check it
        else:
            missing = [iid for iid, _ in deficits(plan["targets"], after)]
        state = "partial" if missing else "carted"
        plan.pop("error", None)
        plan["missing"] = missing
        plan["added"] = sum(1 for line in plan["lines"] if str(line["pick"]["item_id"]) not in missing)
        plan["unresolved"] = plan["carried"] + plan["unanswered"] + plan["no_pick"]
        plan["estimated_total"] = round(sum((line["pick"].get("price") or 0) * line["qty"] for line in plan["lines"]), 2)
        plan["unknown_prices"] = [line["key"] for line in plan["lines"] if line["pick"].get("price") is None]
        j.db.save_cart_run(week, review_id, state, plan)
        data["cart_result"] = plan
        j.db.update_review(review_id, data=data, status=state)
        j.db.log("carted", id=review_id, missing=missing, swaps=plan["swaps"])
        j.ha.clear(j.REVIEW_TAG)
        msg = (f"{plan['added']} grocery lines sent to your Walmart cart. The cart couldn't be read back, so give it a look before checking out."
               if plan.get("unverified") else
               f"{plan['added']} grocery lines verified in your Walmart cart. Review it and check out manually.")
        if missing or plan["unresolved"]:
            msg += f" {len(missing) + len(plan['unresolved'])} items need attention on the review page."
        j.ha.notify("Grocery cart needs review" if missing or plan["unresolved"] else "Grocery cart is ready",
                    msg, tag=j.STATUS_TAG, url=f"{j.CONFIG.public_url}/review",
                    actions=[{"action": "URI", "title": "Open cart", "uri": "https://www.walmart.com/cart"},
                             {"action": "GROCER_DONE", "title": "Checked out"}])
        return {k: plan[k] for k in ("added", "swaps", "carried", "unanswered", "missing", "no_pick")}
    except Exception as e:
        run = j.db.cart_run(week)
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


def confirm_checkout() -> None:
    from . import jobs as j

    # The entire inventory update and the completion marker share one transaction.
    with j.db.tx() as c:
        row = c.execute("SELECT * FROM reviews ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            run = c.execute("SELECT * FROM cart_runs WHERE week_start = ?", (row["week_start"],)).fetchone()
            if run and run["state"] not in {"carted", "checked_out"}:
                raise RuntimeError("The cart has unverified quantities. Retry its check before confirming checkout.")
            if run and run["state"] == "carted":
                data = json.loads(run["data"])
                credits = {line["key"]: (line["stock_credit"], line["used"]) for line in data["lines"]}
                for item in data.get("covered", []):
                    credits.setdefault(item["key"], (0, item["used"]))
                for key, (bought, used) in credits.items():
                    stock = c.execute("SELECT packages FROM stock WHERE key = ?", (key,)).fetchone()
                    packages = max((stock["packages"] if stock else 0) + bought - used, 0)
                    ts = now_iso()
                    c.execute("""INSERT INTO stock(key, packages, confidence, updated, last_bought, restock)
                                 VALUES (?,?,?,?,?,0) ON CONFLICT(key) DO UPDATE SET packages=excluded.packages,
                                 confidence=excluded.confidence, updated=excluded.updated,
                                 last_bought=COALESCE(excluded.last_bought, stock.last_bought),
                                 restock=CASE WHEN ? > 0 THEN 0 ELSE stock.restock END""",
                              (key, packages, 0.7, ts, ts if bought else None, bought))
                c.execute("UPDATE cart_runs SET state='checked_out', updated=? WHERE week_start=?", (now_iso(), row["week_start"]))
                # these dinners are bought; the Dinners page shows them as ordered from now on
                from . import dinners
                review = json.loads(row["data"])
                dinners.mark_ordered(review.get("meal_keys", []), review.get("meals", []), row["id"], c)
                c.execute("UPDATE reviews SET status='checked_out' WHERE id=?", (row["id"],))
            elif not run and row["status"] == "carted":
                # Old releases already credited stock at cart time. Do not credit it again.
                c.execute("UPDATE reviews SET status='checked_out' WHERE id=?", (row["id"],))
    j.ha.clear(j.STATUS_TAG)
    j.changed()
    j.db.log("checked_out")

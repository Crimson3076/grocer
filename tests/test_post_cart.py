"""Saved products go straight to the cart; uncertain stock is never called verified."""
import json

import pytest
import test_flow  # noqa: F401  sets up the isolated test data directory before app imports
from test_safety import build, household  # noqa: F401

from grocer.availability import cart_item_checks


def test_old_saved_product_reused_while_building(household):
    c, browser, db = household
    build(c)
    db.run("UPDATE picks SET updated='2020-01-01T00:00:00+00:00', availability_checked_at=NULL")
    browser.item = lambda iid: pytest.fail("Reopened a saved product while building")
    review = build(c)
    assert any(i["status"] == "buy" for i in review["items"])


@pytest.mark.parametrize("available", [False, None])
def test_post_cart_problem_requires_review_without_product_visits(household, available):
    c, browser, db = household
    build(c)
    iid = db.pick("corn tortilla")["item_id"]
    original = browser.cart_contents
    def contents():
        result = original()
        if iid in result:
            browser.last_cart_checks[iid] = {"available": available, "reason": "Shipping only"}
        return result
    browser.cart_contents = contents
    browser.item = lambda iid: pytest.fail("Opened product page before adding")
    result = c.post("/api/cart?force=true&wait=true").json()
    plan = db.cart_run("2026-10-11")["data"]
    assert db.latest_review()["status"] == "partial"
    if available is False:
        assert iid in result["missing"]
        assert not db.pick("corn tortilla")["local_available"]
    else:
        assert iid in plan["fulfillment_unverified"]
        assert plan["unverified"]
    assert any(not m["complete"] for m in plan["coverage"])
    assert c.post("/api/checked-out").status_code >= 400
    quantities = {line["key"]: (0 if str(line["pick"]["item_id"]) == iid and available is False else line["qty"])
                  for line in plan["lines"]}
    assert c.post("/api/checked-out", json={"quantities": quantities}).status_code == 200
    updated = json.loads(db.latest_review()["data"])["cart_result"]
    assert not updated["fulfillment_unverified"]


def cart_line(**fields):
    return {"quantity": 1, "product": {"usItemId": "7", **fields}}


@pytest.mark.parametrize("fields,expected", [
    ({}, None),
    ({"availabilityStatus": "OUT_OF_STOCK"}, False),
    ({"fulfillmentType": "SHIPPING"}, False),
    ({"fulfillmentOptions": [{"type": "SHIPPING", "availabilityStatus": "IN_STOCK"}]}, False),
    ({"sellerName": "Other Seller"}, False),
    ({"isMarketplaceItem": True}, False),
    ({"fulfillmentOptions": [{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}]}, True),
    ({"fulfillmentOptions": [{"type": "PICKUP", "storeId": "9999", "availabilityStatus": "IN_STOCK"}]}, None),
])
def test_cart_fulfillment_evidence(fields, expected):
    result = cart_item_checks({"cart": {"lineItems": [cart_line(**fields)]}}, "1234", "PICKUP")
    assert result["7"]["available"] is expected


def test_cart_group_shipping_overrides_product_pickup_offer():
    data = {"cart": {"fulfillmentGroups": [{"fulfillmentType": "SHIPPING", "lineItems": [
        cart_line(fulfillmentOptions=[{"type": "PICKUP", "storeId": "1234"}])]}],
        "recommendations": [{"quantity": 1, "usItemId": "8"}]}}
    checks = cart_item_checks(data, "1234", "PICKUP")
    assert checks["7"]["available"] is False
    assert "8" not in checks


def test_cart_group_provides_store_and_method():
    data = {"cart": {"fulfillmentGroups": [{"fulfillmentType": "PICKUP", "storeId": "1234",
                                             "lineItems": [cart_line()]}]}}
    assert cart_item_checks(data, "1234", "PICKUP")["7"]["available"] is True
    assert cart_item_checks(data, "1234", "DELIVERY")["7"]["available"] is False
    assert cart_item_checks(data, "9999", "PICKUP")["7"]["available"] is False


def test_cached_build_does_not_import_history_or_open_account(household):
    c, browser, db = household
    build(c)
    db.run("DELETE FROM events WHERE kind='history_synced'")
    calls = []
    browser.purchase_history = lambda: (calls.append("history") or [])
    browser.ensure_logged_in = lambda: calls.append("account")
    build(c)
    assert not calls


def test_cart_uses_prepared_snapshot_instead_of_account_and_store_pages(household):
    c, browser, _ = household
    build(c)
    calls = []
    original = browser.cart_contents
    browser.prepare_cart = lambda: (calls.append("prepare") or original())
    browser.cart_contents = lambda: (calls.append("after") or original())
    browser.ensure_logged_in = lambda: pytest.fail("Opened account page")
    browser.ensure_store = lambda: pytest.fail("Opened home page")
    result = c.post("/api/cart?force=true&wait=true").json()
    assert "failed" not in result
    assert calls == ["prepare", "after"]


def test_costs_are_exposed_on_review_and_dinners(household):
    c, _, _ = household
    build(c)
    review = c.get("/api/review").json()
    assert len(review["meal_costs"]) == len(review["meals"])
    dinners = c.get("/api/dinners").json()
    assert all("cost" in m for m in dinners["meals"])


def test_one_missing_product_does_not_force_full_history_import(household):
    c, browser, db = household
    build(c)
    db.run("DELETE FROM events WHERE kind='history_synced'")
    db.run("DELETE FROM picks WHERE key='corn tortilla'")
    calls = []
    browser.purchase_history = lambda: (calls.append("history") or [])
    build(c)
    assert not calls

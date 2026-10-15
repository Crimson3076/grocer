"""Regression coverage for local inventory, cart recovery and purchase accounting."""
import json
from datetime import date

import pytest
from fastapi.testclient import TestClient

from grocer import app as appmod, jobs
from grocer.availability import cart_quantities, local_stock, selected_store
from grocer.cart import plan_line
from grocer.db import DB
from grocer.walmart import listing_from_node
from test_flow import FakeBrowser, FakeHA, FakeLLM, FakeMealie, CleanupMealie


@pytest.fixture
def household(monkeypatch, tmp_path):
    jobs.WORKER.submit(lambda: None).result(timeout=10)
    db = DB(tmp_path / "grocer.db")
    browser = FakeBrowser()
    monkeypatch.setattr(jobs, "db", db)
    monkeypatch.setattr(jobs, "browser", browser)
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs.CONFIG, "walmart_store_id", "1234")
    monkeypatch.setattr(jobs.CONFIG, "walmart_fulfillment", "PICKUP")
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (date(2026, 10, 11), date(2026, 10, 17)))
    monkeypatch.setattr(jobs, "today", lambda: date(2026, 10, 10))
    c = TestClient(appmod.app)
    yield c, browser, db
    jobs.WORKER.submit(lambda: None).result(timeout=10)
    db._conn.close()


@pytest.mark.parametrize("options,session,mode,allowed", [
    ([{"type": "SHIPPING", "availabilityStatus": "IN_STOCK"}], "1234", "PICKUP", False),
    ([{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "OUT_OF_STOCK"},
      {"type": "SHIPPING", "availabilityStatus": "IN_STOCK"}], "1234", "PICKUP", False),
    ([{"type": "PICKUP", "storeId": "9999", "availabilityStatus": "IN_STOCK"}], "1234", "PICKUP", False),
    ([{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}], None, "PICKUP", True),
    ([{"type": "PICKUP", "availabilityStatus": "IN_STOCK"}], "1234", "PICKUP", True),
    ([{"type": "PICKUP", "availabilityStatus": "IN_STOCK"}], None, "PICKUP", False),
    ([{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}], "9999", "PICKUP", False),
    ([{"type": "DELIVERY", "storeId": "1234", "availabilityStatus": "IN_STOCK"}], "1234", "DELIVERY", True),
    ([{"type": "DELIVERY", "storeId": "1234", "availabilityStatus": "IN_STOCK", "fulfillmentMethod": "SHIPPING"}], "1234", "DELIVERY", False),
    ([], "1234", "PICKUP", False),
])
def test_only_explicit_local_stock(options, session, mode, allowed):
    node = {"availabilityStatus": "IN_STOCK", "fulfillmentOptions": options}
    assert local_stock(node, "1234", mode, session)[0] is allowed
    assert local_stock(node, "", mode, session)[0] is False
    listing = listing_from_node(dict(node, usItemId="5", name="Milk, 1 gallon"),
                                store_id="1234", fulfillment=mode, session_store=session)
    assert listing["local_available"] is allowed


@pytest.mark.parametrize("extra,session", [
    ({"fulfillmentOptions": [{"type": "PICKUP", "storeId": "9999", "availabilityStatus": "IN_STOCK"}]}, "1234"),
    ({"fulfillmentOptions": [{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}]}, "9999"),
    ({"fulfillmentOptions": [{"type": "DELIVERY", "storeId": "1234", "availabilityStatus": "IN_STOCK"}]}, "1234"),
    ({"fulfillmentBadge": "Free shipping, arrives tomorrow", "sellerName": "Walmart.com"}, "1234"),
    ({"fulfillmentBadge": "Pickup today"}, None),
    ({"fulfillmentOptions": [{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}],
      "fulfillmentBadge": "Pickup in 3 days"}, "1234"),
])
def test_listing_rejects_wrong_store_method_and_time(extra, session):
    p = listing_from_node({"usItemId": "7", "name": "Milk", "availabilityStatus": "IN_STOCK", **extra},
                          store_id="1234", fulfillment="PICKUP", session_store=session)
    assert not p["local_available"] and not p["local_confirmed"]


@pytest.mark.parametrize("fields", [
    {}, {"local_available": False}, {"local_available": True},
    {"local_available": True, "store_id": "9999", "fulfillment": "PICKUP"},
    {"local_available": True, "store_id": "1234", "fulfillment": "DELIVERY"},
])
def test_unknown_and_mismatched_saved_picks_are_rejected(household, fields):
    assert not jobs.locally_available({"item_id": "7", **fields})


def test_search_never_promotes_unverified_results():
    from pathlib import Path
    from grocer.walmart import WalmartBrowser
    w = WalmartBrowser(Path("/tmp/unused-grocer-profile"))
    w.store_id = "1234"
    w._ready = lambda: None
    w._typed_search = lambda q: ({}, {"7": {
        "usItemId": "7", "name": "Milk", "availabilityStatus": "IN_STOCK",
        "fulfillmentBadge": "Pickup today"}})
    assert w._search("milk") == []


def test_legacy_saved_pick_is_rechecked_before_cart_add(household):
    c, browser, db = household
    build(c)
    iid = db.pick("corn tortilla")["item_id"]
    assert jobs.recently_checked("corn tortilla", 7 * 24)
    db.run("UPDATE picks SET availability_checked_at=NULL")
    original = browser.item
    checked = []

    def item(key):
        checked.append(key)
        return dict(original(key), local_available=False) if key == iid else original(key)

    browser.item = item
    original_search = browser.search
    browser.search = lambda q, limit=12: [] if q == "corn tortilla" else original_search(q, limit)
    result = c.post("/api/cart?force=true&wait=true").json()
    assert "corn tortilla" in result["carried"]
    assert checked.count(iid) == 1
    assert iid not in browser.cart_item_ids()


def test_retry_rechecks_remaining_product_before_mutation(household):
    c, browser, db = household
    build(c)
    original_add = browser.add_to_cart

    def interrupted(lines):
        original_add(lines[:1])
        raise RuntimeError("interrupted")

    browser.add_to_cart = interrupted
    c.post("/api/cart?force=true&wait=true")
    before = list(browser.carted)
    browser.add_to_cart = original_add
    original_item = browser.item
    browser.item = lambda iid: dict(original_item(iid), local_available=False)
    result = c.post("/api/cart?force=true&wait=true").json()
    assert "no longer available" in result["failed"]
    assert browser.carted == before
    assert db.cart_run("2026-10-11")["state"] == "needs_check"


def test_store_context_excludes_product_suggestions():
    data = {"props": {"pageProps": {"location": {"pickupStore": {"storeId": "1234"}},
                                  "products": [{"selectedStore": {"storeId": "9999"}}]}}}
    assert selected_store(data) == "1234"
    data["other"] = {"selectedStore": {"storeId": "8888"}}
    assert selected_store(data) is None


def test_cart_parser_does_not_count_recommendations():
    data = {"cart": {"lineItems": [{"quantity": 2, "product": {"usItemId": "100001"}}],
                     "recommendations": [{"quantity": 10, "usItemId": "200001"}]}}
    assert cart_quantities(data) == {"100001": 2}
    assert cart_quantities({"cart": {"lineItems": []}}) == {}
    assert cart_quantities({"recommendations": [{"usItemId": "100001"}]}) is None
    assert cart_quantities({"cart": {"lineItems": [{"usItemId": "100001", "quantity": 1.5}]}}) is None


def build(c):
    r = c.post("/api/review/build?wait=true")
    assert r.status_code == 200
    review = r.json()["review"]
    assert review["status"] == "pending"
    return review


def test_no_credit_until_checkout_and_checkout_is_idempotent(household):
    c, browser, db = household
    review = build(c)
    result = c.post("/api/cart?force=true&wait=true").json()
    assert result["added"] > 0
    assert db.stock("corn tortilla") is None
    assert c.post("/api/approve").status_code == 409
    assert c.post("/api/checked-out").status_code == 200
    stock = dict(db.stock("corn tortilla"))
    assert stock["packages"] > 0
    assert c.post("/api/checked-out").status_code == 200
    assert dict(db.stock("corn tortilla")) == stock
    assert db.latest_review()["status"] == "checked_out"
    # Unanswered ingredients and an unknown recipe yield keep these dinners unfinished.
    assert db.all('SELECT key FROM meal_orders') == []
    coverage = db.cart_run('2026-10-11')['data']['coverage']
    assert all(not m['complete'] and m['missing'] for m in coverage)


def test_rebuild_and_force_do_not_duplicate_completed_cart(household):
    c, browser, db = household
    review = build(c)
    c.post("/api/cart?force=true&wait=true")
    first = list(browser.carted)
    assert c.post("/api/review/build?wait=true").json()["review"]["id"] == review["id"]
    assert c.post("/api/cart?force=true&wait=true").json() == {"skipped": "already carted"}
    assert browser.carted == first


def test_partial_mutation_is_resumed_without_double_add(household):
    c, browser, db = household
    build(c)
    original = browser.add_to_cart
    calls = []

    def interrupt(lines):
        calls.append(list(lines))
        original(lines[:2])
        raise RuntimeError("connection lost after two additions")

    browser.add_to_cart = interrupt
    failed = c.post("/api/cart?force=true&wait=true").json()
    assert "failed" in failed
    assert c.get("/api/status").json()["last"]["state"] == "failed"
    first = set(browser.cart_item_ids())
    browser.add_to_cart = lambda lines: (calls.append(list(lines)), original(lines))
    result = c.post("/api/cart?force=true&wait=true").json()
    assert not result["missing"]
    assert not (first & {iid for iid, _ in calls[1]})
    target = db.cart_run("2026-10-11")["data"]["targets"]
    assert browser.cart_contents() == target
    assert db.stock("corn tortilla") is None


def test_unreadable_cart_fills_once_but_retry_never_adds_blindly(household):
    c, browser, db = household
    build(c)
    browser.cart_contents = lambda: None
    original = browser.add_to_cart

    def interrupt(lines):
        original(lines[:2])
        raise RuntimeError("connection lost after two additions")

    browser.add_to_cart = interrupt
    assert "failed" in c.post("/api/cart?force=true&wait=true").json()
    sent = list(browser.carted)
    browser.add_to_cart = original
    # the retry can't see what's already there, so it refuses rather than doubling
    assert "failed" in c.post("/api/cart?force=true&wait=true").json()
    assert browser.carted == sent


def test_unreadable_cart_first_fill_goes_ahead_and_says_so(household):
    c, browser, db = household
    build(c)
    browser.cart_contents = lambda: None
    result = c.post("/api/cart?force=true&wait=true").json()
    assert result["added"] > 0 and browser.carted
    assert db.cart_run("2026-10-11")["data"]["unverified"] is True


def test_empty_verified_cart_is_not_treated_as_success(household):
    c, browser, db = household
    build(c)
    browser.add_to_cart = lambda lines: None
    result = c.post("/api/cart?force=true&wait=true").json()
    assert result["missing"] and result["added"] == 0
    assert db.latest_review()["status"] == "partial"
    assert c.post("/api/checked-out").status_code == 409
    assert db.stock("corn tortilla") is None


def test_cart_already_contains_requested_items(household):
    c, browser, db = household
    review = build(c)
    wanted = next(i for i in review["items"] if i["key"] == "corn tortilla")
    browser.carted = [(wanted["pick"]["item_id"], wanted["qty"])]
    c.post("/api/cart?force=true&wait=true")
    assert browser.cart_contents()[wanted["pick"]["item_id"]] == wanted["qty"]


def test_manual_qty_survives_stock_and_preference_edits(household):
    c, browser, db = household
    build(c)
    assert c.post("/api/qty", json={"key": "butter", "qty": 4}).status_code == 200
    c.post("/api/stock", json={"food": "butter", "level": "high"})
    c.post("/api/food", json={"key": "butter", "brand_lock": True})
    item = next(i for i in c.get("/api/review").json()["items"] if i["key"] == "butter")
    assert item["qty"] == 4 and item["manual_qty"]


def test_low_staple_without_saved_product_gets_a_product(household):
    c, browser, db = household
    db.ensure_food("kosher salt", "spice")
    db.set_food("kosher salt", staple=1)
    db.set_stock("kosher salt", 0, 0.9)
    db.set_restock("kosher salt", True)
    review = build(c)
    salt = next(i for i in review["items"] if i["key"] == "kosher salt")
    assert salt["status"] == "buy" and salt["pick"]["local_available"]


def test_substitution_recalculates_package_count_and_stock_credit():
    item = {"key": "ground beef", "category": "meat", "amounts": {"weight": 907.2},
            "use_pk": 2, "stock": 0, "qty": 2,
            "pick": {"item_id": "1", "size_qty": 453.6, "size_dim": "weight"}}
    smaller = {"item_id": "2", "size_qty": 226.8, "size_dim": "weight"}
    line = plan_line(item, smaller)
    assert line["qty"] == 4 and line["stock_credit"] == 2
    bigger = {"item_id": "3", "size_qty": 907.2, "size_dim": "weight"}
    line = plan_line(item, bigger)
    assert line["qty"] == 1 and line["stock_credit"] == 2


def test_unknown_substitute_size_is_not_guessed():
    item = {"key": "beef", "category": "meat", "amounts": {}, "qty": 2,
            "pick": {"item_id": "1", "size_qty": 453.6, "size_dim": "weight"}}
    assert plan_line(item, {"item_id": "2"}) is None


def test_manual_link_and_alternative_reject_shipping_only(household):
    c, browser, db = household
    review = build(c)
    before = dict(db.pick("ground beef"))
    browser.item = lambda iid: listing_from_node({"usItemId": iid, "name": "Shipping only", "availabilityStatus": "IN_STOCK",
                                                  "fulfillmentOptions": [{"type": "SHIPPING", "availabilityStatus": "IN_STOCK"}]},
                                                 store_id="1234")
    assert c.post("/api/swap", json={"key": "ground beef", "url": "999999"}).status_code == 400
    assert c.post("/api/alternative", json={"key": "ground beef", "index": 0}).status_code == 400
    assert dict(db.pick("ground beef")) == before


def test_search_drops_nonlocal_products(household):
    c, browser, db = household
    local = browser._listing("Butter, 16 oz", "100001")
    remote = dict(local, item_id="100002", local_available=False)
    wrong_store = dict(local, item_id="100003", store_id="9999")
    browser.search = lambda q, limit=10: [remote, local, wrong_store]
    result = c.get("/api/search", params={"q": "butter"}).json()["results"]
    assert [p["item_id"] for p in result] == ["100001"]


def test_history_link_rechecks_local_availability(household):
    c, browser, db = household
    db.save_purchases([{"item_id": "100001", "title": "Butter"}])
    browser.item = lambda iid: {"item_id": iid, "local_available": False, "local_reason": "Shipping only"}
    assert c.post("/api/use-purchase", json={"key": "butter", "item_id": "100001"}).status_code == 400
    assert db.pick("butter") is None


def test_settings_invalidate_local_picks_and_approval(household):
    c, browser, db = household
    build(c)
    c.post("/api/approve")
    assert c.post("/api/settings/walmart", json={"store_id": "9999", "fulfillment": "PICKUP"}).status_code == 200
    review = c.get("/api/review").json()
    assert review["status"] == "pending"
    assert all(not i["pick"]["local_available"] for i in review["items"] if i.get("pick"))
    assert c.post("/api/approve").status_code == 409


def test_backups_must_succeed_before_merging(household, monkeypatch):
    c, browser, db = household
    m = CleanupMealie()
    m.backup = lambda: False
    monkeypatch.setattr(jobs, "mealie", m)
    job = c.post("/api/mealie/cleanup/apply?wait=true", json={"groups": [{"keep": "id0", "same": ["id1"]}]}).json()
    assert job["state"] == "failed" and "backup failed" in job["error"]
    assert m.merged == []


@pytest.mark.parametrize("path,body", [
    ("/api/yield", {"recipe_id": "t1", "servings": 0}),
    ("/api/yield", {"recipe_id": "t1", "servings": -4}),
    ("/api/servings", {"recipe_id": "t1", "date": "2026-10-11", "servings": 0}),
    ("/api/qty", {"key": "butter", "qty": -1}),
    ("/api/qty", {"key": "butter", "qty": 1.5}),
    ("/api/alternative", {"key": "butter", "index": -1}),
])
def test_invalid_numeric_inputs_are_rejected(household, path, body):
    c, browser, db = household
    assert c.post(path, json=body).status_code == 422


def test_no_store_does_not_offer_purchases(household, monkeypatch):
    c, browser, db = household
    monkeypatch.setattr(jobs.CONFIG, "walmart_store_id", "")
    review = build(c)
    assert not any(i["status"] == "buy" for i in review["items"])


@pytest.mark.parametrize("extra,ok,label", [
    ({"fulfillmentBadgeGroups": [{"text": "Pickup", "slaText": "today"}, {"text": "Delivery", "slaText": "today"}]},
     True, "Pickup today"),
    ({"fulfillmentBadgeGroups": [{"text": "Delivery", "slaText": "tomorrow"}]}, False, "No pickup"),
    ({"badges": {"groups": [{"name": "fulfillment", "members": [{"key": "FF_PICKUP", "text": "Pickup in 2 hours"}]}]}},
     True, "Pickup today"),
    ({"fulfillmentSummary": [{"fulfillment": "PICKUP", "storeId": "1234", "availabilityStatus": "AVAILABLE"}]},
     True, "Pickup"),
    ({"sellerName": "Walmart.com", "fulfillmentBadge": "Free shipping, arrives tomorrow"}, False, "No pickup"),
    ({"sellerName": "Jet Deals LLC", "fulfillmentBadgeGroups": [{"text": "Free shipping, arrives in 3 days"}]},
     False, "Sold and shipped by Jet Deals LLC"),
    ({"sellerName": "Grocery Outlet Co", "fulfillmentBadgeGroups": [{"text": "Pickup", "slaText": "today"}]},
     False, "Sold and shipped by Grocery Outlet Co"),
    ({"sellerName": "Walmart.com", "fulfillmentBadgeGroups": [{"text": "Free shipping, arrives in 3+ days"}]},
     False, "Shipping only"),
    ({}, False, "No pickup or delivery today or tomorrow"),
])
def test_only_today_or_tomorrow_from_walmart(extra, ok, label):
    node = {"usItemId": "5", "name": "French's Classic Yellow Mustard, 20oz (Pack of 48)",
            "availabilityStatusV2": {"value": "IN_STOCK"}, **extra}
    node["fulfillmentOptions"] = [{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}]
    # The no-evidence case must not inherit the structured method from the other cases.
    if not extra:
        node.pop("fulfillmentOptions")
    p = listing_from_node(node, store_id="1234", session_store="1234")
    assert p["local_available"] is ok
    if extra:
        assert p["local_reason"].startswith(label)
    out = dict(node, availabilityStatusV2={"value": "OUT_OF_STOCK"})
    assert not listing_from_node(out, store_id="1234", session_store="1234")["local_available"]


def test_reset_does_not_change_stock_or_walmart_cart(household):
    c, browser, db = household
    build(c)
    c.post("/api/cart?force=true&wait=true")
    before = list(browser.carted)
    assert c.post("/api/cart/reset").status_code == 200
    assert browser.carted == before and db.cart_run("2026-10-11") is None
    assert db.stock("corn tortilla") is None


def test_voice_restock_for_an_existing_staple_looks_up_a_product(household):
    c, browser, db = household
    build(c)
    salt = next(i for i in c.get("/api/review").json()["items"] if i["key"] == "kosher salt")
    assert salt["status"] == "staple" and salt["pick"] is None
    c.post("/api/stock", json={"food": "kosher salt", "level": "out"})
    jobs.WORKER.submit(lambda: None).result(timeout=10)
    salt = next(i for i in c.get("/api/review").json()["items"] if i["key"] == "kosher salt")
    assert salt["status"] == "buy" and salt["pick"]["local_available"]


def test_revalidation_removes_an_unavailable_saved_pick(household):
    c, browser, db = household
    build(c)
    db.run("UPDATE picks SET updated='2020-01-01T00:00:00+00:00' WHERE key='corn tortilla'")
    real_item = browser.item
    iid = db.pick("corn tortilla")["item_id"]
    browser.item = lambda key: dict(real_item(key), local_available=False) if key == iid else real_item(key)
    real_search = browser.search
    browser.search = lambda q, limit=12: [] if q == "corn tortilla" else real_search(q, limit)
    review = build(c)
    item = next(i for i in review["items"] if i["key"] == "corn tortilla")
    assert item["status"] == "nopick" and item["pick"] is None


def test_same_product_in_separate_cart_lines_is_counted_once_per_line():
    cart = {"lineItems": [{"id": "a", "usItemId": "100001", "quantity": 1},
                          {"id": "b", "usItemId": "100001", "quantity": 2}]}
    assert cart_quantities({"cart": cart, "cartData": cart}) == {"100001": 3}
    assert cart_quantities({"cart": cart, "cartData": {"lineItems": []}}) is None


def test_store_records_without_a_status_count_as_offered():
    """Walmart's fulfillmentSummary lists methods per store with no status field (seen in real search data)."""
    node = {"usItemId": "7", "name": "Great Value Yellow Mustard, 20 oz", "availabilityStatusV2": {"value": "IN_STOCK"},
            "fulfillmentSummary": [{"fulfillment": "DELIVERY", "storeId": "3370", "fulfillmentMethods": ["UNSCHEDULED"]},
                                   {"fulfillment": "PICKUP", "storeId": "3370"}]}
    p = listing_from_node(node, store_id="3370", fulfillment="DELIVERY", session_store="3370")
    assert p["local_available"] and p["local_reason"] in {"Delivery", "Pickup"}
    gone = dict(node, fulfillmentSummary=[{"fulfillment": "DELIVERY", "storeId": "3370", "availabilityStatus": "OUT_OF_STOCK"}])
    assert not listing_from_node(gone, store_id="3370", fulfillment="DELIVERY", session_store="3370")["local_available"]


def test_search_keeps_the_most_detailed_copy_of_a_product():
    from grocer.walmart import _walk
    thin = {"usItemId": "7", "name": "Mustard"}
    rich = {"usItemId": "7", "name": "Mustard", "fulfillmentSummary": [{"fulfillment": "PICKUP"}], "priceInfo": {}}
    found = {}
    _walk({"ads": [thin], "searchResult": {"items": [rich]}}, found)
    assert found["7"] is rich


def test_one_button_send_confirm_and_choices(household):
    c, browser, db = household
    review = build(c)
    new = [i["key"] for i in review["items"] if i["status"] == "buy" and i.get("new_pick")]
    if new:
        assert c.post("/api/confirm-pick", json={"key": new[0]}).status_code == 200
        assert not next(i for i in c.get("/api/review").json()["items"] if i["key"] == new[0])["new_pick"]
    ch = c.get("/api/choices", params={"key": "ground beef"}).json()
    assert ch["key"] == "ground beef" and "bought_before" in ch and "alternatives" in ch
    # one button: approves and fills, even with the list still pending
    result = c.post("/api/send?wait=true").json()
    assert result["added"] > 0 and browser.carted
    assert db.latest_review()["status"] == "carted"


def test_empty_cart_needs_checkout_confirmation(household, monkeypatch):
    c, browser, db = household
    build(c)
    c.post("/api/send?wait=true")
    # still in the cart, recently: just wait
    assert jobs.check_checkout() == "waiting"
    assert db.latest_review()["status"] == "carted"
    # a day later, still there: ask once
    db.run("UPDATE cart_runs SET updated = '2020-01-01T00:00:00+00:00'")
    assert jobs.check_checkout() == "asked"
    assert any("Did you check out" in t for t, _ in jobs.ha.sent)
    assert jobs.check_checkout() == "waiting"          # not asked again right away
    # Removing items is indistinguishable from checkout. Neither credits stock.
    browser.cart_contents = lambda: {}
    assert jobs.check_checkout() == "waiting"  # the recent prompt is not repeated
    assert db.latest_review()["status"] == "carted"
    assert db.stock("corn tortilla") is None
    db.run("DELETE FROM events WHERE kind LIKE 'checkout_asked:%'")
    assert jobs.check_checkout() == "asked"
    assert "no longer in the cart" in jobs.ha.sent[-1][1]
    assert db.latest_review()["status"] == "carted"
    assert db.stock("corn tortilla") is None
    assert c.post("/api/checked-out").status_code == 200
    assert db.latest_review()["status"] == "checked_out"
    assert db.stock("corn tortilla") is not None


def _removable(browser):
    def remove(ids=None):
        gone = [i for i, _ in browser.carted if ids is None or str(i) in ids]
        browser.carted[:] = [(i, q) for i, q in browser.carted if str(i) not in {str(g) for g in gone}]
        return {"removed": sorted(set(str(g) for g in gone)), "left": browser.cart_contents()}
    browser.remove_from_cart = remove


def test_clear_only_what_the_app_added(household):
    c, browser, db = household
    _removable(browser)
    build(c)
    browser.carted.append(("999999", 1))                       # something you added yourself
    c.post("/api/send?wait=true")
    assert db.latest_review()["status"] == "carted"
    last = c.post("/api/cart/clear?wait=true", json={"scope": "ours"}).json()
    assert last["state"] == "done" and "out of your Walmart cart" in last["message"]
    assert [i for i, _ in browser.carted] == ["999999"]        # yours stays
    assert db.latest_review()["status"] == "pending"           # the list can be changed and sent again
    assert db.cart_run("2026-10-11") is None
    assert c.post("/api/send?wait=true").json()["added"] > 0   # and sending again works


def test_clear_everything_and_nothing_to_clear(household):
    c, browser, db = household
    _removable(browser)
    build(c)
    last = c.post("/api/cart/clear?wait=true", json={"scope": "ours"}).json()
    assert last["state"] == "failed" and "hasn't put anything" in last["error"]
    browser.carted.append(("999999", 2))
    c.post("/api/send?wait=true")
    last = c.post("/api/cart/clear?wait=true", json={"scope": "all"}).json()
    assert last["state"] == "done" and browser.carted == []
    assert c.post("/api/cart/clear", json={"scope": "bogus"}).status_code == 422


def test_new_fill_reuses_verified_products_and_reads_cart_twice(household):
    c, browser, db = household
    build(c)
    calls = []
    original = browser.cart_contents
    browser.cart_contents = lambda: (calls.append("cart") or original())
    browser.item = lambda iid: pytest.fail("Recent verified product was reopened")
    result = c.post("/api/cart?force=true&wait=true").json()
    assert "failed" not in result
    assert result["added"] > 0
    assert calls == ["cart", "cart"]


@pytest.mark.parametrize("stamp,store,method,available", [
    ("old", "1234", "PICKUP", True),
    ("future", "1234", "PICKUP", True),
    (None, "1234", "PICKUP", True),
    ("bad", "1234", "PICKUP", True),
    ("now", "9999", "PICKUP", True),
    ("now", "1234", "DELIVERY", True),
    ("now", "1234", "PICKUP", False),
])
def test_cart_cache_rejects_invalid_observations(household, stamp, store, method, available):
    from datetime import datetime, timedelta, timezone

    from grocer.ordering import cached_availability
    _, _, db = household
    now = datetime.now(timezone.utc)
    stamps = {"old": (now - timedelta(hours=2)).isoformat(),
              "future": (now + timedelta(minutes=5)).isoformat(), "now": now.isoformat()}
    db.save_pick("milk", {"item_id": "7", "local_available": available, "store_id": store,
                          "fulfillment": method, "availability_checked_at": stamps.get(stamp, stamp)}, True)
    assert cached_availability(db, "7", jobs.locally_available, 60, jobs.walmart_settings()) is None


def test_saving_a_cached_pick_does_not_extend_availability(household):
    from grocer.ordering import cached_availability
    _, browser, db = household
    p = browser._listing("Milk", "7")
    p["availability_checked_at"] = "2020-01-01T00:00:00+00:00"
    db.save_pick("milk", p, False)
    db.save_pick("milk", dict(db.pick("milk")), True)
    assert cached_availability(db, "7", jobs.locally_available, 60, jobs.walmart_settings()) is None


def test_cart_cache_can_be_disabled(household, monkeypatch):
    c, browser, _ = household
    build(c)
    monkeypatch.setattr(jobs.CONFIG, "cart_availability_minutes", 0)
    checked = []
    original = browser.item
    browser.item = lambda iid: (checked.append(iid) or original(iid))
    result = c.post("/api/cart?force=true&wait=true").json()
    assert "failed" not in result
    assert checked
    assert len(checked) == len(set(checked))


def test_49_verified_items_need_no_product_page_visits(household):
    from copy import deepcopy
    c, browser, db = household
    build(c)
    row = db.latest_review()
    data = json.loads(row["data"])
    template = next(i for i in data["items"] if i["status"] == "buy")
    data["items"] = []
    for n in range(49):
        item = deepcopy(template)
        item["key"] = f"grocery {n}"
        item["pick"] = browser._listing("Milk, 1 gallon", str(90000 + n))
        db.save_pick(item["key"], item["pick"], True)
        data["items"].append(item)
    db.update_review(row["id"], data=data)
    browser.item = lambda iid: pytest.fail("Verified product was reopened")
    result = c.post("/api/cart?force=true&wait=true").json()
    assert result["added"] == 49
    assert len(browser.cart_item_ids()) == 49


def test_bulk_add_49_items_uses_three_requests():
    from grocer.walmart import WalmartBrowser
    browser = object.__new__(WalmartBrowser)
    urls = []
    browser._goto = urls.append
    browser._pause = lambda factor: None
    browser.add_to_cart([(str(n), 1) for n in range(49)])
    assert len(urls) == 3
    assert [len(u.split("items=")[1].split(",")) for u in urls] == [20, 20, 9]

"""Trip and inventory invariants, using local fakes only."""

import dataclasses
import json

import pytest
from test_safety import build
from test_safety import household as household

from grocer import (
    cleanup,
    dinners,
    jobs,
    pantry,
    readiness,
    task_history,
)
from grocer.db import DB, now_iso
from grocer.trips import owned_removals


def purchase(c):
    build(c)
    result = c.post("/api/send?wait=true")
    assert result.status_code == 200
    assert c.post("/api/checked-out", json={}).status_code == 200
    return c.get("/api/review").json()


def test_stale_selection_blocks_all_new_cart_paths(household):
    c, b, db = household
    r = build(c)
    dinners.choose(r["meal_keys"], False)
    assert c.post("/api/approve").status_code == 409
    for route in ("/api/send?wait=true", "/api/cart?force=true&wait=true"):
        response = c.post(route)
        assert (
            response.status_code == 409
            or c.get("/api/status").json()["last"]["state"] == "failed"
        )
    assert b.carted == []


@pytest.mark.parametrize("change", ["servings", "yield", "store", "recipe"])
def test_input_versions_cover_all_shopping_inputs(household, monkeypatch, change):
    c, b, db = household
    build(c)
    if change == "servings":
        db.set_meal_servings("2026-10-11", "dinner", "t1", 6)
    if change == "yield":
        db.set_recipe_yield("t1", 7)
    if change == "store":
        db.save_settings("walmart", {"store_id": "9999", "fulfillment": "PICKUP"})
    if change == "recipe":
        original = jobs.mealie.recipe
        monkeypatch.setattr(
            jobs.mealie,
            "recipe",
            lambda slug: dataclasses.replace(original(slug), ingredients=[]),
        )
    with pytest.raises(RuntimeError, match="changed"):
        jobs.send_to_walmart()
    assert b.carted == []


def test_a_second_trip_can_use_the_same_start_date(household):
    c, b, db = household
    first = purchase(c)
    second = build(c)
    assert second["week_start"] == first["week_start"]
    assert second["trip_id"] != first["trip_id"]
    assert db.cart_run(second["week_start"]) is None
    assert db.cart_run(first["week_start"], first["id"])["state"] == "checked_out"
    assert db.one("SELECT COUNT(*) n FROM trips")["n"] == 2


def test_rebuild_keeps_draft_trip_identity(household):
    c, b, db = household
    first = build(c)
    second = build(c)
    assert first["trip_id"] == second["trip_id"]
    assert first["id"] != second["id"]


def test_restock_without_dinners_is_a_real_trip(household):
    c, b, db = household
    dinners.choose(dinners.selected_keys(), False)
    assert c.post("/api/groceries", json={"text": "milk"}).status_code == 200
    r = build(c)
    assert r["meals"] == []
    jobs.ha.sent.clear()
    jobs.notify_review(r)
    assert any("Groceries-only trip" in message for title, message in jobs.ha.sent)
    assert any(i["key"] == "milk" and i["status"] == "buy" for i in r["items"])
    assert c.post("/api/send?wait=true").json()["added"] >= 1


def test_grocery_add_does_not_zero_an_existing_estimate(household):
    c, b, db = household
    build(c)
    db.set_stock("butter", 2, 0.9)
    before = db.stock("butter")["quantity"]
    c.post("/api/groceries", json={"text": "butter"})
    assert db.stock("butter")["quantity"] == before
    assert db.stock("butter")["restock"]


def test_shared_cart_quantities_are_never_removed(household):
    c, b, db = household
    r = build(c)
    iid = next(i["pick"]["item_id"] for i in r["items"] if i["key"] == "corn tortilla")
    b.carted = [(iid, 2)]
    c.post("/api/send?wait=true")
    called = []

    def remove(ids):
        called.extend(ids)
        gone = [i for i, q in b.carted if i in ids]
        b.carted = [(i, q) for i, q in b.carted if i not in ids]
        return {"removed": gone, "left": b.cart_contents()}

    b.remove_from_cart = remove
    result = jobs.clear_cart("ours")
    assert iid not in called
    assert b.cart_contents()[iid] == 2
    assert not result["protected"]  # app added no packages of that product


@pytest.mark.parametrize(
    "baseline,added,current,remove,protected",
    [
        ({}, {"7": 2}, {"7": 2}, {"7"}, []),
        ({"7": 2}, {"7": 1}, {"7": 3}, set(), ["7"]),
        ({}, {"7": 2}, {"7": 3}, set(), ["7"]),
        ({}, {"7": 2}, {}, set(), []),
    ],
)
def test_undo_handles_manual_cart_changes(baseline, added, current, remove, protected):
    assert owned_removals(
        {"cart_before": baseline, "added_quantities": added}, current
    ) == (remove, protected)


def test_legacy_cart_ownership_is_not_guessed():
    assert owned_removals({"targets": {"7": 1}}, {"7": 1}) == (set(), ["7"])


def test_unknown_cart_does_not_erase_saved_plan(household):
    c, b, db = household
    build(c)
    c.post("/api/send?wait=true")
    b.cart_contents = lambda: None
    with pytest.raises(RuntimeError, match="Nothing was removed"):
        jobs.clear_cart("ours")
    assert db.cart_run("2026-10-11") is not None


def test_purchase_adds_stock_without_consuming_dinners(household):
    c, b, db = household
    purchase(c)
    assert db.stock("corn tortilla")["packages"] == 1
    assert pantry.reserved_packages("corn tortilla") == pytest.approx(0.2)
    assert pantry.pending_meals()
    assert db.all("SELECT * FROM inventory_events WHERE kind='cooked'") == []


def test_cooking_and_skipping_are_idempotent(household):
    c, b, db = household
    r = purchase(c)
    tacos = next(m for m in r["meals"] if m["recipe_id"] == "t1")
    before = db.stock("corn tortilla")["quantity"]
    assert (
        c.post(
            "/api/meals/resolve", json={"key": tacos["key"], "action": "cooked"}
        ).status_code
        == 200
    )
    after = db.stock("corn tortilla")["quantity"]
    assert after == pytest.approx(before - 6)
    c.post("/api/meals/resolve", json={"key": tacos["key"], "action": "cooked"})
    assert db.stock("corn tortilla")["quantity"] == after
    salmon = next(m for m in r["meals"] if m["recipe_id"] == "s1")
    before = db.stock("butter")["quantity"]
    c.post("/api/meals/resolve", json={"key": salmon["key"], "action": "skipped"})
    assert db.stock("butter")["quantity"] == before
    assert db.all("SELECT * FROM reservations WHERE meal_key=?", salmon["key"]) == []


def test_stock_is_independent_of_product_package_size(household):
    c, b, db = household
    build(c)
    pick = dict(db.pick("corn tortilla"))
    db.set_stock("corn tortilla", 0.5, 0.9)
    assert db.stock("corn tortilla")["quantity"] == 15
    db.save_pick("corn tortilla", dict(pick, item_id="new", size_qty=60), approved=True)
    assert db.stock("corn tortilla")["quantity"] == 15
    assert db.stock("corn tortilla")["packages"] == pytest.approx(0.25)


def test_reservations_reduce_availability_for_other_meals(household):
    c, b, db = household
    r = purchase(c)
    selected = next(i for i in r["items"] if i["key"] == "corn tortilla")
    item = dict(selected, meal_keys=["some-other-dinner"])
    fresh = jobs.recompute_item(item)
    assert fresh["stock"] == pytest.approx(0.8)
    assert fresh["reserved"] == pytest.approx(0.2)


def test_unfinished_dinners_remain_selected_and_missing_groceries_carry(household):
    c, b, db = household
    r = purchase(c)
    assert all(not m["complete"] for m in r["cart_result"]["coverage"])
    assert set(dinners.selected_keys()) == set(r["meal_keys"])
    assert {"honey", "salsa", "vegetable oil"} <= set(db.restock_keys())


def test_purchase_quantities_can_exclude_products(household):
    c, b, db = household
    build(c)
    c.post("/api/send?wait=true")
    assert (
        c.post(
            "/api/checked-out", json={"quantities": {"corn tortilla": 0}}
        ).status_code
        == 200
    )
    assert db.stock("corn tortilla")["quantity"] == 0
    assert db.stock("corn tortilla")["restock"]
    result = c.get("/api/review").json()["cart_result"]
    assert "corn tortilla" in result["carried"]


@pytest.mark.parametrize("quantity", [-1, 1.5, True, 1000])
def test_purchase_quantities_validate_before_changes(household, quantity):
    c, b, db = household
    build(c)
    c.post("/api/send?wait=true")
    assert (
        c.post(
            "/api/checked-out", json={"quantities": {"corn tortilla": quantity}}
        ).status_code
        == 422
    )
    assert db.stock("corn tortilla") is None


def test_manual_purchase_confirmation_covers_missing_ingredients(household):
    c, b, db = household
    db.set_recipe_yield("t1", 4)
    build(c)
    c.post("/api/send?wait=true")
    unresolved = c.get("/api/review").json()["cart_result"]["unresolved"]
    assert (
        c.post("/api/checked-out", json={"manual_keys": unresolved}).status_code == 200
    )
    r = c.get("/api/review").json()
    assert all(m["complete"] for m in r["cart_result"]["coverage"])
    assert set(dinners.ordered()) == set(r["meal_keys"])


def test_nested_transaction_rolls_back_every_inventory_change(household):
    c, b, db = household
    with pytest.raises(RuntimeError):
        with db.tx():
            db.ensure_food("rollback-food", "other")
            db.set_stock("rollback-food", 3, 0.9)
            raise RuntimeError("abort")
    assert db.food("rollback-food") is None and db.stock("rollback-food") is None


def test_task_interruption_survives_database_reopen(household, tmp_path, monkeypatch):
    c, b, db = household
    task_history.record(
        db,
        {"id": "resume1", "kind": "build", "label": "Update list"},
        jobs.build_review,
        (),
        {"notify": False},
    )
    task_history.finish(db, {"id": "resume1"}, "running")
    db._conn.close()
    recovered = DB(tmp_path / "grocer.db")
    monkeypatch.setattr(jobs, "db", recovered)
    pending = task_history.recovery(recovered)
    assert pending[0]["id"] == "resume1" and pending[0]["state"] == "interrupted"
    job = task_history.resume("resume1")
    jobs.wait_for(job)
    assert (
        recovered.one("SELECT state FROM job_records WHERE id=?", job["id"])["state"]
        == "done"
    )
    assert task_history.recovery(recovered) == []


def test_failed_task_is_persisted(household):
    c, b, db = household

    def fail():
        raise RuntimeError("simulated")

    job = jobs.start("build", "Fail once", fail)
    jobs.wait_for(job)
    row = db.one("SELECT * FROM job_records WHERE id=?", job["id"])
    assert row["state"] == "failed" and row["error"] == "simulated"


def test_cleanup_requires_review_and_can_be_undone(household, monkeypatch):
    c, b, db = household
    monkeypatch.setattr(jobs, "rebuild_soon", lambda: {})
    for key in ("black pepper", "cracked black pepper"):
        db.ensure_food(key, "spice")
        db.set_stock(key, 0.5, 0.9)
    before = cleanup.snapshot(db)
    p = cleanup.scan()
    assert cleanup.snapshot(db) == before
    cleanup.apply(p["id"], [0])
    assert db.food("cracked black pepper") is None
    cleanup.undo(p["id"])
    assert cleanup.snapshot(db) == before


def test_cleanup_rejects_stale_proposals_and_unsafe_undo(household, monkeypatch):
    c, b, db = household
    monkeypatch.setattr(jobs, "rebuild_soon", lambda: {})
    for key in ("black pepper", "cracked black pepper"):
        db.ensure_food(key, "spice")
    p = cleanup.scan()
    db.set_stock("black pepper", 1, 0.9)
    with pytest.raises(RuntimeError, match="changed"):
        cleanup.apply(p["id"], [0])
    p = cleanup.scan()
    cleanup.apply(p["id"], [0])
    db.set_stock("black pepper", 2, 0.9)
    with pytest.raises(RuntimeError, match="changed"):
        cleanup.undo(p["id"])
    assert db.stock("black pepper")["packages"] == 2


def test_database_upgrade_preserves_legacy_rows_and_cart(tmp_path):
    path = tmp_path / "legacy.db"
    db = DB(path)
    db.ensure_food("milk", "dairy")
    db.save_pick("milk", {"item_id": "7", "size_qty": 1000, "size_dim": "volume"}, True)
    db.run(
        "INSERT INTO stock(key,packages,confidence) VALUES (?,?,?)", "milk", 0.5, 0.9
    )
    db.run(
        "INSERT INTO reviews(week_start,created,status,data) VALUES (?,?,?,?)",
        "2026-10-11",
        now_iso(),
        "carted",
        "{}",
    )
    rid = db.latest_review()["id"]
    db.run(
        "INSERT INTO cart_runs VALUES (?,?,?,?,?)",
        "2026-10-11",
        rid,
        "carted",
        "{}",
        now_iso(),
    )
    db._conn.close()
    db = DB(path)
    assert db.stock("milk")["quantity"] == 500
    assert db.latest_review()["trip_id"] == "legacy:" + str(rid)
    assert db.cart_run("2026-10-11")["state"] == "carted"
    assert db.cart_run("2026-10-11")["review_id"] == rid
    db._conn.close()


def test_root_returns_to_trip_or_setup(household, monkeypatch):
    c, b, db = household
    monkeypatch.setattr(jobs.CONFIG, "mealie_token", "")
    assert c.get("/", follow_redirects=False).headers["location"] == "/settings#setup"
    monkeypatch.setattr(jobs.CONFIG, "mealie_token", "fake")
    assert c.get("/", follow_redirects=False).headers["location"] == "/dinners"
    build(c)
    assert c.get("/", follow_redirects=False).headers["location"] == "/review"


def test_connection_checks_are_explicit_and_do_not_expose_tokens(
    household, monkeypatch
):
    c, b, db = household
    monkeypatch.setattr(jobs.CONFIG, "mealie_token", "secret-mealie")
    monkeypatch.setattr(jobs.CONFIG, "ha_token", "secret-ha")
    calls = []

    class Reply:
        def raise_for_status(self):
            pass

        def json(self):
            return {"models": [{"name": jobs.CONFIG.ollama_model}]}

    def get(url, **kw):
        calls.append(url)
        return Reply()

    monkeypatch.setattr(readiness.httpx, "get", get)
    assert c.get("/api/setup").status_code == 200 and calls == []
    response = c.post("/api/setup/check")
    assert response.status_code == 200 and len(calls) == 3
    assert "secret-" not in response.text
    assert response.json()["checks"]["ollama"]["ok"]


def test_partial_checkout_requires_explicit_quantities(household):
    c, b, db = household
    build(c)
    original = b.add_to_cart
    b.add_to_cart = lambda lines: original(lines[:1])
    c.post("/api/send?wait=true")
    run = db.cart_run("2026-10-11")
    assert run["state"] == "partial"
    assert c.post("/api/checked-out").status_code == 409
    quantities = {
        line["key"]: (
            line["qty"]
            if str(line["pick"]["item_id"]) not in run["data"]["missing"]
            else 0
        )
        for line in run["data"]["lines"]
    }
    assert (
        c.post("/api/checked-out", json={"quantities": quantities}).status_code == 200
    )
    assert db.cart_run("2026-10-11")["state"] == "checked_out"
    assert any(not m["complete"] for m in db.cart_run("2026-10-11")["data"]["coverage"])


def test_purchase_rollback_does_not_partially_credit_stock(household, monkeypatch):
    c, b, db = household
    build(c)
    c.post("/api/send?wait=true")
    original = pantry.credit
    seen = []

    def fail_second(connection, line, ref):
        seen.append(line["key"])
        if len(seen) == 2:
            raise RuntimeError("simulated credit failure")
        return original(connection, line, ref)

    monkeypatch.setattr(pantry, "credit", fail_second)
    assert c.post("/api/checked-out", json={}).status_code == 409
    assert db.all("SELECT * FROM inventory_events WHERE kind='purchase'") == []
    assert db.stock(seen[0]) is None
    assert db.cart_run("2026-10-11")["state"] == "carted"


def test_unread_recipe_lines_require_manual_confirmation(household):
    c, b, db = household
    r = build(c)
    data = json.loads(db.latest_review()["data"])
    unread = data["meals"][0]["name"] + ": unread ingredient"
    data["unparsed"] = [unread]
    data["meals"][0]["unparsed"] = [unread]
    db.update_review(r["id"], data=data)
    c.post("/api/send?wait=true")
    run = db.cart_run("2026-10-11")
    assert unread in run["data"]["unresolved"]
    assert any(unread in m["missing"] for m in run["data"]["coverage"])
    assert c.post("/api/checked-out", json={"manual_keys": [unread]}).status_code == 200
    assert not any(
        unread in m["missing"] for m in db.cart_run("2026-10-11")["data"]["coverage"]
    )


def test_active_cart_rejects_ingredient_deletion(household):
    c, b, db = household
    build(c)
    c.post("/api/send?wait=true")
    assert c.post("/api/food/delete", json={"key": "corn tortilla"}).status_code == 409
    assert db.food("corn tortilla") is not None


def test_stock_compatibility_column_remains_in_packages(household):
    c, b, db = household
    purchase(c)
    assert (
        db.one("SELECT packages FROM stock WHERE key=?", "corn tortilla")["packages"]
        == 1
    )


def test_migration_takes_a_backup_before_adding_columns(tmp_path):
    import sqlite3

    from grocer.db import SCHEMA

    path = tmp_path / "before.db"
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.execute(
        "INSERT INTO picks(key,item_id,size_qty,size_dim) VALUES (?,?,?,?)",
        ("milk", "7", 1000, "volume"),
    )
    conn.execute(
        "INSERT INTO stock(key,packages,confidence) VALUES (?,?,?)", ("milk", 0.5, 0.9)
    )
    conn.commit()
    conn.close()
    upgraded = DB(path)
    assert upgraded.stock("milk")["quantity"] == 500
    backup = tmp_path / "before.before-0.2.0.db"
    assert backup.exists()
    with sqlite3.connect(backup) as old:
        assert (
            old.execute("SELECT packages FROM stock WHERE key=?", ("milk",)).fetchone()[
                0
            ]
            == 0.5
        )
        assert "quantity" not in {r[1] for r in old.execute("PRAGMA table_info(stock)")}
    upgraded._conn.close()


def test_failed_tasks_remain_visible_and_can_be_dismissed(household):
    c, b, db = household
    build(c)
    task_history.record(
        db,
        {"id": "failed-preview", "kind": "cart", "label": "Check cart"},
        jobs.run_cart,
        (),
        {},
    )
    task_history.finish(
        db, {"id": "failed-preview"}, "failed", "Try the cart check again"
    )
    response = c.get("/api/status").json()["recovery"]
    assert response[0]["can_resume"] and response[0]["state"] == "failed"
    assert c.post("/api/jobs/failed-preview/dismiss").status_code == 200
    assert c.get("/api/status").json()["recovery"] == []


def test_undo_reconciles_partial_cart_before_retry(household):
    c, b, db = household
    review = build(c)
    iid = next(
        i["pick"]["item_id"] for i in review["items"] if i["key"] == "corn tortilla"
    )
    b.carted = [(iid, 2)]
    c.post("/api/send?wait=true")

    def remove(ids):
        removed = [key for key, qty in b.carted if key in ids]
        b.carted = [(key, qty) for key, qty in b.carted if key not in ids]
        return {"removed": removed, "left": b.cart_contents()}

    b.remove_from_cart = remove
    jobs.clear_cart("ours")
    assert db.cart_run("2026-10-11")["state"] == "partial"
    assert b.cart_contents() == {iid: 2}
    c.post("/api/send?wait=true")
    run = db.cart_run("2026-10-11")
    assert run["state"] == "carted"
    assert b.cart_contents()[iid] == 2
    assert all(
        qty == 1 for key, qty in run["data"]["added_quantities"].items() if key != iid
    )


def test_cooking_uncounted_ingredient_marks_estimate_uncertain(household):
    c, b, db = household
    r = purchase(c)
    key = r["meals"][0]["key"]
    db.ensure_food("mystery sauce", "condiment")
    db.save_pick(
        "mystery sauce", {"item_id": "99", "size_qty": 100, "size_dim": "volume"}, True
    )
    db.set_stock("mystery sauce", 0.8, 0.9)
    db.run(
        "INSERT INTO reservations VALUES (?,?,?,?)", key, "mystery sauce", 0, "unknown"
    )
    assert (
        c.post("/api/meals/resolve", json={"key": key, "action": "cooked"}).status_code
        == 200
    )
    assert db.stock("mystery sauce")["quantity"] == 80
    assert db.stock("mystery sauce")["confidence"] < 0.5


def test_each_produce_reservations_survive_cooking(household):
    c, b, db = household
    r = purchase(c)
    garlic = db.stock("garlic")
    assert garlic["dimension"] == "count" and garlic["packages"] == 1
    assert pantry.reserved_packages("garlic") > 0
    meal = next(m for m in r["meals"] if m["recipe_id"] == "t1")
    c.post("/api/meals/resolve", json={"key": meal["key"], "action": "cooked"})
    assert db.stock("garlic")["packages"] is not None
    assert db.stock("garlic")["packages"] < 1


def test_merge_preserves_physical_stock_when_kept_package_is_larger(
    household, monkeypatch
):
    c, b, db = household
    monkeypatch.setattr(jobs, "rebuild_soon", lambda: {})
    for key in ("old flour", "flour"):
        db.ensure_food(key, "pantry")
    db.save_pick(
        "old flour", {"item_id": "old", "size_qty": 100, "size_dim": "weight"}, True
    )
    db.save_pick(
        "flour",
        {"item_id": "new", "size_qty": 200, "size_dim": "weight"},
        True,
        source="manual",
    )
    db.set_stock("old flour", 0.5, 0.9)
    jobs.merge_by_hand("old flour", "flour")
    assert db.stock("flour")["quantity"] == 50
    assert db.stock("flour")["packages"] == 0.25


def test_old_cart_task_cannot_act_on_a_new_trip(household):
    c, b, db = household
    build(c)
    task_history.record(
        db,
        {"id": "old-trip-cart", "kind": "cart", "label": "Fill previous cart"},
        jobs.run_cart,
        (),
        {},
    )
    task_history.finish(db, {"id": "old-trip-cart"}, "interrupted")
    # Finish this trip synchronously so the deliberately saved interruption remains.
    jobs.send_to_walmart()
    jobs.checked_out()
    jobs.build_review(notify=False)
    assert not task_history.recovery(db)[0]["can_resume"]
    before = list(b.carted)
    with pytest.raises(RuntimeError, match="earlier trip"):
        task_history.resume("old-trip-cart")
    assert b.carted == before


def test_whole_cart_removal_needs_a_fresh_confirmation(household):
    c, b, db = household
    build(c)
    task_history.record(
        db,
        {"id": "clear-all", "kind": "clear", "label": "Empty whole cart"},
        jobs.clear_cart,
        ("all",),
        {},
    )
    task_history.finish(db, {"id": "clear-all"}, "interrupted")
    assert not task_history.recovery(db)[0]["can_resume"]
    with pytest.raises(RuntimeError, match="confirmation"):
        task_history.resume("clear-all")


def test_rebuild_requests_are_durable_and_coalesced(household):
    import threading

    c, b, db = household
    gate = threading.Event()
    started = threading.Event()

    def slow():
        started.set()
        gate.wait(5)

    job = jobs.start("history", "Read history", slow)
    assert started.wait(2)
    first = jobs.rebuild_soon()
    second = jobs.rebuild_soon()
    assert first["queued"] and second["queued"]
    assert len(db.all("SELECT * FROM job_records WHERE state='queued'")) == 2
    gate.set()
    jobs.wait_for(job)
    jobs.WORKER.submit(lambda: None).result(timeout=10)
    assert len(db.all("SELECT * FROM reviews")) == 1
    assert not db.all(
        "SELECT * FROM job_records WHERE state IN ('queued','dispatching')"
    )


def test_repeated_recipe_on_same_day_keeps_distinct_meal_ids(household, monkeypatch):
    c, b, db = household
    original = jobs.mealie.mealplan

    def mealplan(start, end):
        entries = original(start, end)
        return entries + [dataclasses.replace(entries[0], id="second-same-dinner")]

    monkeypatch.setattr(jobs.mealie, "mealplan", mealplan)
    review = build(c)
    assert len(review["meal_keys"]) == 3
    assert len({m["key"] for m in review["meals"]}) == 3
    beef = next(i for i in review["items"] if i["key"] == "ground beef")
    assert len(beef["meal_keys"]) == 2 and len(beef["meal_usage"]) == 2

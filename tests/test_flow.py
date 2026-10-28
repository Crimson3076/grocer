"""End-to-end weekly flow with fake Mealie, Walmart, Ollama and Home Assistant."""
import os
from pathlib import Path
import tempfile

os.environ.setdefault("DATA_DIR", tempfile.mkdtemp())

from fastapi.testclient import TestClient  # noqa: E402

from grocer import app as appmod, jobs  # noqa: E402
from grocer.mealie import Ingredient, PlanEntry, Recipe  # noqa: E402

TACOS = Recipe("t1", "tacos", "Classic crispy tacos", 12.0, "", [Ingredient(x) for x in [
    "1 tablespoon vegetable oil", "1/2 medium yellow onion, diced", "1 jalapeño, seeded and diced",
    "4 cloves garlic, minced", "1 pound ground beef", "1 teaspoon kosher salt", "Oil for frying",
    "12 corn tortillas", "2 cups (8 ounces) shredded yellow cheddar or Colby-Jack cheese", "Salsa, for serving",
]], None, None)
SALMON = Recipe("s1", "salmon", "Honey Garlic Glazed Salmon", 4.0, "", [Ingredient(x) for x in [
    "4 (6 oz each) salmon filets", "3 Tbsp butter", "6 cloves garlic (minced)", "1/2 cup honey", "3 Tbsp water",
]], None, None)


class FakeMealie:
    def mealplan(self, s, e):
        return [PlanEntry("2026-10-11", "dinner", "t1", "tacos", "Tacos"),
                PlanEntry("2026-10-12", "dinner", "s1", "salmon", "Salmon")]

    def recipe(self, slug):
        return {"tacos": TACOS, "salmon": SALMON}[slug]

    def on_hand_foods(self):
        return {"kosher salt"}

    def recipe_url(self, public_url, slug):
        return f"{public_url}/g/home/r/{slug}"


class FakeLLM:
    MAP = {"vegetable oil": ("vegetable oil", "oil"), "oil": ("vegetable oil", "oil"),
           "jalapeno": ("jalapeno", "produce"), "yellow onion": ("yellow onion", "produce"),
           "garlic": ("garlic", "produce"), "ground beef": ("ground beef", "meat"),
           "kosher salt": ("kosher salt", "spice"), "corn tortilla": ("corn tortilla", "bakery"),
           "salsa": ("salsa", "condiment"), "salmon filet": ("salmon", "seafood"),
           "butter": ("butter", "dairy"), "honey": ("honey", "condiment"), "water": ("water", "none"),
           "shredded yellow cheddar cheese": ("shredded cheddar cheese", "dairy")}

    def find_duplicates(self, names):
        groups = []
        if "black pepper" in names:
            same = [n for n in names if n != "black pepper" and n.endswith("pepper") and "bell" not in n and "red" not in n]
            if same:
                groups.append({"keep": "black pepper", "same": same})
        if "dried parsley" in names and "dried parsley leaves" in names:
            groups.append({"keep": "dried parsley", "same": ["dried parsley leaves"]})
        return groups

    def confirm_same(self, pairs):
        return [{"index": i, "reason": "different kind" if "red" in b.lower() or "cheddar" in b.lower() else "same thing",
                 "same": not ("red" in b.lower() or "cheddar" in b.lower())} for i, (a, b) in enumerate(pairs)]

    def canonicalize(self, foods, existing=None):
        return [{"input": f, "item": self.MAP.get(f, (f, "other"))[0], "category": self.MAP.get(f, (f, "other"))[1]}
                for f in foods]

    def judge_history(self, food, lines, cands):
        return [{"index": i, "match": food.split()[-1] in c["title"].lower() and "packet" not in c["title"].lower(),
                 "reason": "same product" if food.split()[-1] in c["title"].lower() else "different product"}
                for i, c in enumerate(cands)]

    def choose_product(self, food, need, cands, avoid=None, usual_brands=None, history=False, recipe_lines=None):
        self.last = {"food": food, "history": history, "usual_brands": usual_brands, "recipe_lines": recipe_lines}
        return {"index": 0, "reason": "first"}


TITLES = {"ground beef": "Great Value 80/20 Ground Beef, 1 lb", "corn tortilla": "Mission Corn Tortillas, 30 Count",
          "salmon": "Atlantic Salmon Fillets, 12 oz", "butter": "Great Value Salted Butter Sticks, 16 oz",
          "honey": "Great Value Clover Honey, 12 oz", "vegetable oil": "Great Value Vegetable Oil, 48 fl oz",
          "shredded cheddar cheese": "Great Value Shredded Mild Cheddar, 8 oz", "salsa": "Pace Chunky Salsa, 16 oz"}


class FakeBrowser:
    def __init__(self):
        self.carted = []
        self.n = 1000
        self.catalog = {}

    def ensure_logged_in(self):
        pass

    def ensure_store(self):
        pass

    def _listing(self, title, iid):
        from grocer.walmart import listing_from_node
        result = listing_from_node({"usItemId": iid, "name": title, "priceInfo": {"currentPrice": {"price": 3.0}},
                                  "availabilityStatusV2": {"value": "IN_STOCK"},
                                  "fulfillmentOptions": [{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}]},
                                  store_id="1234")
        self.catalog[iid] = result
        return result

    def search(self, q, limit=12):
        self.n += 10
        title = TITLES.get(q, f"Fresh {q.title()}, Each")
        return [self._listing(title, str(self.n)), self._listing(title + " Family Pack", str(self.n + 1))]

    def item(self, iid):
        if iid in self.catalog:
            return dict(self.catalog[iid])
        row = jobs.db.one("SELECT title FROM picks WHERE item_id=?", iid) or jobs.db.one("SELECT title FROM purchases WHERE item_id=?", iid)
        return self._listing(row["title"] if row else "Some Item, 16 oz", iid)

    history = []

    def purchase_history(self):
        self.history_calls = getattr(self, "history_calls", 0) + 1
        return [self._listing(t, i) for t, i in self.history]

    def add_to_cart(self, lines):
        self.carted.extend(lines)

    def cart_item_ids(self):
        return {i for i, _ in self.carted}

    def cart_contents(self):
        result = {}
        for iid, qty in self.carted:
            result[iid] = result.get(iid, 0) + qty
        self.last_cart_checks = {}
        for iid in result:
            product = self.catalog.get(iid)
            if product is None:
                row = jobs.db.one("SELECT * FROM picks WHERE item_id=?", iid)
                product = dict(row) if row else {}
            self.last_cart_checks[iid] = {"available": jobs.locally_available(product),
                                          "reason": "Unavailable" if not jobs.locally_available(product) else "Pickup"}
        return result

    def close(self):
        pass


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_services(monkeypatch):
    """Background rebuilds outlive a test; make sure they never reach the real Mealie or Chrome."""
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs.CONFIG, "walmart_store_id", "1234")
    monkeypatch.setattr(jobs.CONFIG, "walmart_fulfillment", "PICKUP")
    fake = FakeBrowser()
    for k, v in vars(jobs.browser).items():   # keep the alert callbacks wired up
        if k.startswith("on_"):
            setattr(fake, k, v)
    monkeypatch.setattr(jobs, "browser", fake)
    # every test starts with nothing ordered, on Saturday Oct 10 (the fake plan is Oct 11 and 12)
    jobs.db.run("DELETE FROM meal_orders")
    jobs.db.run("DELETE FROM settings WHERE key = 'meal_choice'")
    monkeypatch.setattr(jobs, "today", lambda: __import__("datetime").date(2026, 10, 10))
    yield
    import time
    for _ in range(50):
        if not jobs.busy():
            break
        time.sleep(0.1)
    jobs.db.run("DELETE FROM cart_runs")


class FakeHA:
    def __init__(self):
        self.sent = []

    def notify(self, title, message, **kw):
        self.sent.append((title, message))

    def clear(self, tag):
        pass


def test_weekly_flow(monkeypatch):
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", FakeBrowser())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 10, 11),
                                                                   __import__("datetime").date(2026, 10, 17)))
    c = TestClient(appmod.app)

    assert c.get("/review").status_code == 200
    r = c.post("/api/review/build?wait=true").json()["review"]
    items = {i["key"]: i for i in r["items"]}

    assert "water" not in items
    assert items["kosher salt"]["status"] == "staple"           # on hand in Mealie
    assert items["vegetable oil"]["status"] == "check"          # ask how much is left
    assert items["garlic"]["qty"] == 1                          # 10 cloves, one bulb
    assert items["corn tortilla"]["qty"] == 1
    assert items["salmon"]["qty"] == 1                          # 2 of 4 filets = 12 oz
    tacos = next(m for m in r["meals"] if m["recipe_id"] == "t1")
    assert tacos["yield_question"]                              # "12" isn't people
    print("\nREVIEW:", r["summary"])
    for k, i in sorted(items.items()):
        print(f"  {k:26s} {i['status']:8s} qty={i['qty']} need={i['need_text']}")

    # answer questions the way the page does
    r = c.post("/api/yield?wait=true", json={"recipe_id": "t1", "servings": 4}).json()["review"]
    assert not next(m for m in r["meals"] if m["recipe_id"] == "t1")["yield_question"]
    c.post("/api/stock", json={"food": "vegetable oil", "level": "low"})
    r = c.get("/api/review").json()
    assert {i["key"]: i for i in r["items"]}["vegetable oil"]["status"] == "buy"

    # guests on salmon night -> 4 people -> more salmon
    r = c.post("/api/servings?wait=true", json={"date": "2026-10-12", "entry_type": "dinner", "recipe_id": "s1", "servings": 4}).json()["review"]
    assert {i["key"]: i for i in r["items"]}["salmon"]["qty"] == 2

    # manual qty survives a rebuild; skip works
    c.post("/api/qty", json={"key": "butter", "qty": 3})
    c.post("/api/skip", json={"key": "salsa"})
    r = c.post("/api/review/build?wait=true").json()["review"]
    items = {i["key"]: i for i in r["items"]}
    assert items["butter"]["qty"] == 3 and items["salsa"]["status"] == "skip"

    # swap to an alternative, approve, fill the cart
    c.post("/api/alternative", json={"key": "ground beef", "index": 0})
    assert c.post("/api/approve").json()["ok"]
    res = c.post("/api/cart?wait=true").json()
    print("CART:", res)
    assert res["added"] == len([i for i in items.values() if i["status"] == "buy"])
    assert c.get("/api/review").json()["status"] == "carted"
    assert jobs.db.stock("corn tortilla") is None              # cart filling is not a purchase
    assert c.post("/api/cart?wait=true").json() == {"skipped": "already carted"}
    assert c.post("/api/checked-out").status_code == 200
    assert jobs.db.stock("corn tortilla")["packages"] > 0      # leftovers credited only after checkout


def test_deleted_and_out_of_stock(monkeypatch):
    monkeypatch.setattr(jobs.CONFIG, "cart_verify_after_add", False)
    b = FakeBrowser()
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", b)
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 10, 18),
                                                                   __import__("datetime").date(2026, 10, 24)))
    c = TestClient(appmod.app)
    items = {i["key"]: i for i in c.post("/api/review/build?wait=true").json()["review"]["items"]}
    gone = items["butter"]["pick"]["item_id"]
    oos = items["salmon"]["pick"]["item_id"]
    c.post("/api/food", json={"key": "salmon", "brand_lock": True})
    for food in ("butter", "salmon"):
        c.post("/api/stock", json={"food": food, "level": "out"})
    items = {i["key"]: i for i in c.get("/api/review").json()["items"]}
    assert items["butter"]["status"] == items["salmon"]["status"] == "buy"

    real_item = b.item

    def item(iid):
        if iid == gone:
            return None                      # listing deleted
        live = real_item(iid)
        if iid == oos:
            live["in_stock"] = False         # temporarily out
            live["local_available"] = False
        return live
    b.item = item
    # Built earlier: availability observations have expired.
    jobs.db.run("UPDATE picks SET updated = '2020-01-01T00:00:00+00:00', availability_checked_at = '2020-01-01T00:00:00+00:00'")

    res = c.post("/api/cart?force=true&wait=true").json()
    assert any(s.startswith("butter ->") for s in res["swaps"])
    assert "salmon" in res["carried"]
    assert jobs.db.pick("butter")["item_id"] != gone           # replacement saved as the new pick
    assert jobs.db.pick("butter")["approved"] == 0             # and flagged for a look next review
    assert jobs.db.pick("salmon")["item_id"] == oos            # brand-locked pick kept for next order


def test_restock_by_voice(monkeypatch):
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", FakeBrowser())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 10, 25),
                                                                   __import__("datetime").date(2026, 10, 31)))
    FakeLLM.MAP["milk"] = ("milk", "dairy")
    c = TestClient(appmod.app)
    c.post("/api/review/build?wait=true")
    c.post("/api/stock", json={"food": "milk", "level": "out"})       # "we're out of milk", no recipe uses it
    jobs.submit(lambda: None).result(timeout=10)                      # the product lookup runs in the background
    items = {i["key"]: i for i in c.get("/api/review").json()["items"]}
    assert items["milk"]["status"] == "buy" and items["milk"]["sources"] == ["Restock"]
    c.post("/api/cart?force=true&wait=true")
    assert jobs.db.stock("milk")["restock"]                              # filling the cart isn't checkout
    assert c.post("/api/checked-out").status_code == 200
    assert not jobs.db.stock("milk")["restock"]                          # cleared after confirmed checkout


def test_robot_check_mid_build_still_finishes(monkeypatch):
    from grocer.walmart import BrowserChallenge
    b = FakeBrowser()
    calls = {"n": 0}
    real = b.search

    def search(q, limit=12):
        calls["n"] += 1
        if calls["n"] > 2:
            raise BrowserChallenge("https://www.walmart.com/blocked")
        return real(q, limit)
    b.search = search
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", b)
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 11, 1),
                                                                   __import__("datetime").date(2026, 11, 7)))
    jobs.db.run("DELETE FROM picks")
    r = TestClient(appmod.app).post("/api/review/build?wait=true").json()["review"]
    assert r["blocked"] is True
    assert calls["n"] == 3                                   # stopped searching after the block
    assert sum(1 for i in r["items"] if i["pick"]) == 2      # kept what it found


def test_purchase_history_first(monkeypatch):
    b = FakeBrowser()
    b.history = [("Land O Lakes Salted Butter Sticks, 16 oz", "555001"),
                 ("Heinz Tomato Ketchup, 32 oz", "555002"),
                 ("Nature Nate's Raw Honey, 16 oz", "555003")]
    searched = []
    real = b.search
    b.search = lambda q, limit=12: (searched.append(q), real(q, limit))[1]
    llm = FakeLLM()
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", llm)
    monkeypatch.setattr(jobs, "browser", b)
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 11, 8),
                                                                   __import__("datetime").date(2026, 11, 14)))
    jobs.db.run("DELETE FROM picks")
    jobs.db.run("DELETE FROM events WHERE kind = 'history_synced'")
    c = TestClient(appmod.app)
    items = {i["key"]: i for i in c.post("/api/review/build?wait=true").json()["review"]["items"]}
    assert b.history_calls == 1                                  # synced before picking
    assert items["butter"]["pick"]["item_id"] == "555001"        # the brand you already buy
    assert items["butter"]["pick"]["source"] == "history" and not items["butter"]["new_pick"]
    assert "butter" not in searched and "honey" not in searched  # no search needed
    assert "ground beef" in searched                             # never bought: searched as before
    assert jobs.db.top_brands()[:1]                              # brands learned
    c.post("/api/review/build?wait=true")
    assert b.history_calls == 1                                  # not re-synced within a week


def test_history_sync_rechecks_unapproved(monkeypatch):
    b = FakeBrowser()
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", b)
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 11, 15),
                                                                   __import__("datetime").date(2026, 11, 21)))
    jobs.db.run("DELETE FROM picks"); jobs.db.run("DELETE FROM purchases")
    c = TestClient(appmod.app)
    items = {i["key"]: i for i in c.post("/api/review/build?wait=true").json()["review"]["items"]}
    assert items["salmon"]["new_pick"]                           # searched, needs a look
    c.post("/api/swap", json={"key": "ground beef", "url": "https://www.walmart.com/ip/x/777777"})
    b.history = [("Atlantic Salmon Fillet Portions, 12 oz", "888001"),
                 ("Laura's Lean 92% Ground Beef, 1 lb", "888002")]
    res = c.post("/api/history/sync?wait=true").json()
    assert "salmon" in res["switched"]
    items = {i["key"]: i for i in res["review"]["items"]}
    assert items["salmon"]["pick"]["item_id"] == "888001" and not items["salmon"]["new_pick"]
    assert items["ground beef"]["pick"]["item_id"] == "777777"   # your manual swap is never overridden


def test_bbq_sauce_never_becomes_sauce(monkeypatch):
    from grocer import planner
    assert planner.keep_specific("bbq sauce", "sauce") == "bbq sauce"
    assert planner.keep_specific("chicken thigh", "chicken") == "chicken thigh"
    assert planner.keep_specific("shredded yellow cheddar or colby jack cheese", "shredded cheddar cheese") \
        == "shredded cheddar cheese"                              # fine: still specific
    assert planner.keep_specific("evoo", "olive oil") == "olive oil"  # fine: a rename, not a shortening
    assert planner.keep_specific("salmon filet", "salmon") == "salmon"  # fine: "filet" is just the cut

    # a bad name saved by an earlier version gets repaired at startup, and its wrong pick dropped
    jobs.db.run("INSERT OR REPLACE INTO parse_cache(text, food_key, category, updated) VALUES ('bbq sauce','sauce','condiment','x')")
    jobs.db.save_pick("sauce", {"item_id": "ragu1", "title": "RAGU Old World Style Six Cheese Sauce, 24 oz"}, approved=True,
                      source="history")
    jobs._repair_generic_names()
    assert jobs.db.one("SELECT food_key FROM parse_cache WHERE text='bbq sauce'")["food_key"] == "bbq sauce"
    assert jobs.db.pick("sauce") is None


def test_salt_and_pepper_split():
    from grocer import planner
    from grocer.parse import parse_line
    parts = planner.split_pairs(parse_line("salt and pepper (to taste)"))
    assert [p.food for p in parts] == ["salt", "pepper"]
    assert len(planner.split_pairs(parse_line("1 cup mac and cheese"))) == 1   # has a quantity: one product


def test_picker_sees_recipe_lines(monkeypatch):
    b = FakeBrowser()
    b.history = [("RAGU Old World Style Six Cheese Sauce Pasta Sauce, 24 oz", "ragu1")]
    llm = FakeLLM()
    calls = []
    def choose(food, need, cands, **kw):
        calls.append((food, kw.get("history"), kw.get("recipe_lines")))
        return {"index": -1 if kw.get("history") else 0, "reason": ""}
    llm.choose_product = choose
    monkeypatch.setattr(jobs, "llm", llm)
    monkeypatch.setattr(jobs, "browser", b)
    jobs.db.run("DELETE FROM purchases"); jobs.db.save_purchases(b.purchase_history())
    from grocer import planner
    need = planner.Need(key="bbq sauce", category="condiment")
    need.add(None, "Slow Cooker BBQ Chicken", "18 oz. BBQ sauce ($1.50)", False)
    jobs.db.run("DELETE FROM picks WHERE key = 'bbq sauce'")
    jobs._pick_for("bbq sauce", need)
    assert all(lines == ["18 oz. BBQ sauce ($1.50)"] for _, _, lines in calls)


def test_pantry_search_and_remove(monkeypatch):
    b = FakeBrowser()
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", b)
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 11, 22),
                                                                   __import__("datetime").date(2026, 11, 28)))
    c = TestClient(appmod.app)
    c.post("/api/review/build?wait=true")
    p = {i["key"]: i for i in c.get("/api/pantry").json()["items"]}
    assert p["butter"]["this_week"] and p["butter"]["pick"] and "Honey Garlic Glazed Salmon" in p["butter"]["sources"]
    assert p["butter"]["lines"] == ["3 Tbsp butter"]
    res = c.get("/api/search", params={"q": "kerrygold butter"}).json()["results"]
    assert res and "bought_before" in res[0]
    c.post("/api/swap", json={"key": "butter", "url": res[0]["item_id"]})
    assert c.get("/api/pantry").json()["items"] and jobs.db.pick("butter")["source"] == "manual"
    jobs.db.ensure_food("lawry's seasoned salt", "spice")
    c.post("/api/food/delete", json={"key": "lawry's seasoned salt"})
    assert jobs.db.food("lawry's seasoned salt") is None


def test_each_names_cleaned_at_startup():
    jobs.db.run("INSERT OR REPLACE INTO parse_cache(text, food_key, category, updated) VALUES ('each salt and pepper','each salt and pepper','spice','x')")
    jobs.db.ensure_food("each salt and pepper", "spice")
    jobs.db.save_pick("each salt and pepper", {"item_id": "1", "title": "x"}, approved=False)
    jobs._repair_generic_names()
    assert jobs.db.food("each salt and pepper") is None and jobs.db.pick("each salt and pepper") is None


def test_quick_rebuild_skips_walmart(monkeypatch):
    b = FakeBrowser()
    calls = {"login": 0, "search": 0}
    b.ensure_logged_in = lambda: calls.__setitem__("login", calls["login"] + 1)
    real = b.search
    b.search = lambda q, limit=12: (calls.__setitem__("search", calls["search"] + 1), real(q, limit))[1]
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", b)
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 11, 29),
                                                                   __import__("datetime").date(2026, 12, 5)))
    jobs.db.log("history_synced", count=1)
    c = TestClient(appmod.app)
    c.post("/api/review/build?wait=true")                       # everything already linked from earlier tests
    calls.update(login=0, search=0)
    c.post("/api/servings?wait=true", json={"date": "2026-10-12", "entry_type": "dinner", "recipe_id": "s1", "servings": 3})
    assert calls == {"login": 0, "search": 0}          # an edit never touches Walmart
    jobs.build_review(notify=True)
    assert calls["login"] == 0                         # even scheduled cached builds skip account browsing


def test_product_images_cached(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "IMAGE_DIR", tmp_path)
    downloads = []

    class Resp:
        content = b"\xff\xd8\xff fake jpeg"
        headers = {"content-type": "image/jpeg"}
        def raise_for_status(self): pass

    monkeypatch.setattr(jobs.httpx, "get", lambda url, **kw: (downloads.append(url), Resp())[1])
    c = TestClient(appmod.app)
    jobs.db.save_pick("test olive oil", {"item_id": "424242", "title": "Olive Oil",
                                         "image": "https://i5.walmartimages.com/x.jpeg"}, approved=True)
    assert c.get("/img/424242").status_code == 200
    assert c.get("/img/424242").content.startswith(b"\xff\xd8")
    assert len(downloads) == 1                                   # second view came from the local copy

    # picked before images were stored: one background lookup, then remembered
    b = FakeBrowser()
    lookups = []
    b.item = lambda iid: (lookups.append(iid), {"item_id": iid, "image": None})[1]
    monkeypatch.setattr(jobs, "browser", b)
    jobs.db.run("INSERT OR REPLACE INTO picks(key, item_id, title, approved, source) VALUES ('old pick','515151','Old',1,'search')")
    jobs._image_lookups.discard("515151")
    assert c.get("/img/515151").status_code == 404
    assert jobs._image_queue[-1] == "515151"                     # queued, not fetched right away
    jobs._lookup_image(jobs._image_queue.pop())                  # what the spaced-out background loop does
    assert lookups == ["515151"]
    assert c.get("/img/515151").status_code == 404 and lookups == ["515151"]   # no picture: not looked up again
    assert c.get("/img/..%2Fetc").status_code == 404


def test_jobs_report_progress_and_refuse_overlap(monkeypatch):
    import threading
    gate = threading.Event()
    seen = []
    def slow():
        jobs.progress(step="Finding salsa at Walmart", done=3, total=10)
        seen.append(jobs.status()["job"]["step"])
        gate.wait(5)
        return {"products": 5, "switched": ["salsa"]}
    c = TestClient(appmod.app)
    v0 = c.get("/api/status").json()["version"]
    job = jobs.start("history", "Checking your past Walmart orders", slow)
    for _ in range(50):
        if seen:
            break
        __import__("time").sleep(0.05)
    st = c.get("/api/status").json()
    assert st["job"]["step"] == "Finding salsa at Walmart" and st["job"]["total"] == 10
    assert c.get("/api/search", params={"q": "salsa"}).status_code == 409          # busy: refused, not hung
    assert c.post("/api/qty", json={"key": "butter", "qty": 2}).status_code == 200  # quick edits still work
    with pytest.raises(RuntimeError, match='not queued'):
        jobs.start('build', 'x', lambda: None)
    gate.set()
    jobs.wait_for(job)
    st = c.get("/api/status").json()
    assert st["job"] is None and st["last"]["state"] == "done"
    assert "switched 1" in st["last"]["message"] and st["version"] > v0


def test_alerts_clear_on_every_phone(monkeypatch):
    class RecHA(FakeHA):
        def __init__(self):
            super().__init__(); self.cleared = []
        def clear(self, tag):
            self.cleared.append(tag)
    ha = RecHA()
    monkeypatch.setattr(jobs, "ha", ha)
    c = TestClient(appmod.app)
    # a failed cart fill raised the alert; someone solves the robot check -> gone from every phone
    jobs.raise_alert()
    jobs._on_challenge_cleared()
    assert jobs.CHALLENGE_TAG in ha.cleared and jobs.STATUS_TAG in ha.cleared
    # sign-in expired alert clears the next time sign-in is confirmed working
    ha.cleared.clear(); jobs.raise_alert()
    jobs.browser.on_login_ok()
    assert ha.cleared == [jobs.STATUS_TAG]
    ha.cleared.clear(); jobs.browser.on_login_ok()
    assert ha.cleared == []                                    # nothing open: nothing to clear
    # "Checked out" on any phone clears the cart notification everywhere
    assert c.post('/api/checked-out').status_code == 409
    assert ha.cleared == []  # no purchase may be recorded without an active cart


def test_home_assistant_buttons_and_voice():
    import json as _json, threading as _th, time as _t
    from websockets.sync.server import serve
    from grocer.ha_events import dispatch, listen_forever

    got = []
    handlers = {"GROCER_APPROVE": lambda: got.append("approve"), "GROCER_DONE": lambda: got.append("done"),
                "stock": lambda food, level: got.append((food, level))}
    assert dispatch("mobile_app_notification_action", {"action": "GROCER_APPROVE"}, handlers)
    assert not dispatch("mobile_app_notification_action", {"action": "SOMETHING_ELSE"}, handlers)
    assert dispatch("grocer_stock", {"food": " Butter ", "level": "LOW"}, handlers)
    assert got == ["approve", ("Butter", "low")]

    # a stand-in Home Assistant: auth handshake, subscriptions, then events
    got.clear()
    subs = []
    def ha(ws):
        ws.send(_json.dumps({"type": "auth_required"}))
        assert _json.loads(ws.recv())["access_token"] == "tok"
        ws.send(_json.dumps({"type": "auth_ok"}))
        for _ in range(2):
            subs.append(_json.loads(ws.recv())["event_type"])
        ws.send(_json.dumps({"type": "event", "event": {"event_type": "mobile_app_notification_action",
                                                        "data": {"action": "GROCER_DONE"}}}))
        ws.send(_json.dumps({"type": "event", "event": {"event_type": "grocer_stock",
                                                        "data": {"food": "milk", "level": "out"}}}))
        _t.sleep(1)
    server = serve(ha, "127.0.0.1", 0)
    port = server.socket.getsockname()[1]
    _th.Thread(target=server.serve_forever, daemon=True).start()
    listen_forever(f"http://127.0.0.1:{port}", "tok", handlers)
    for _ in range(50):
        if len(got) == 2:
            break
        _t.sleep(0.05)
    server.shutdown()
    assert set(subs) == {"mobile_app_notification_action", "grocer_stock"}
    assert got == ["done", ("milk", "out")]


def test_image_found_anywhere_in_product_data():
    from grocer.walmart import listing_from_node
    node = {"usItemId": "1", "name": "x", "imageInfo": {"allImages": []},
            "media": {"images": [{"src": "https://i5.walmartimages.com/seo/Great-Value-Butter_abc.jpeg?odnHeight=100"}]}}
    assert listing_from_node(node)["image"] == "https://i5.walmartimages.com/seo/Great-Value-Butter_abc.jpeg"


def test_background_activity_shows_in_bar(monkeypatch):
    monkeypatch.setattr(jobs, "ha", FakeHA())
    c = TestClient(appmod.app)
    with jobs.activity("Getting product pictures", "Looking up butter on Walmart"):
        st = c.get("/api/status").json()
        assert st["job"] is None and st["bg"]["label"] == "Getting product pictures"
        jobs._on_challenge()                                    # robot check during background work
        assert c.get("/api/status").json()["bg"]["waiting"] is True
        jobs._on_challenge_cleared()
        assert c.get("/api/status").json()["bg"]["waiting"] is False
    assert c.get("/api/status").json()["bg"] is None


def test_check_page(monkeypatch):
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    FakeLLM.MAP["ketchup"] = ("ketchup", "condiment")
    jobs.db.run("DELETE FROM purchases")
    jobs.db.save_purchases([
        {"item_id": "70001", "title": "Heinz Tomato Ketchup, 32 oz", "brand": "Heinz", "price": 4.12},
        {"item_id": "70002", "title": "Great Value Tomato Ketchup, 38 oz", "brand": "Great Value", "price": 2.48},
        {"item_id": "70003", "title": "Hunt's Tomato Sauce, 15 oz", "brand": "Hunt's", "price": 1.12},
    ])
    c = TestClient(appmod.app)
    r = c.get("/api/check", params={"q": "Ketchup"}).json()
    assert r["key"] == "ketchup" and r["history_count"] == 3
    assert {x["title"] for x in r["candidates"]} == {"Heinz Tomato Ketchup, 32 oz", "Great Value Tomato Ketchup, 38 oz"}
    assert all(x["match"] is True and x["reason"] for x in r["candidates"])
    assert r["near"] == []                                     # tomato sauce shares no word with "ketchup"
    FakeLLM.MAP["bbq sauce"] = ("bbq sauce", "condiment")
    near = c.get("/api/check", params={"q": "bbq sauce"}).json()["near"]
    assert [n["title"] for n in near] == ["Hunt's Tomato Sauce, 15 oz"] and near[0]["shared"] == ["sauce"]
    assert c.get("/check").status_code == 200
    # "Use this one" links it straight from history, no Walmart visit
    assert c.post("/api/use-purchase", json={"key": "ketchup", "item_id": "70001"}).status_code == 200
    assert jobs.db.pick("ketchup")["item_id"] == "70001" and jobs.db.pick("ketchup")["source"] == "manual"
    assert c.get("/api/check", params={"q": "ketchup"}).json()["pick"]["item_id"] == "70001"
    assert c.post("/api/use-purchase", json={"key": "ketchup", "item_id": "nope"}).status_code == 400


def test_spelling_duplicates_merge():
    from grocer.parse import norm_key, parse_line
    assert norm_key("All-Purpose Flour") == norm_key("all purpose flour") == "all purpose flour"
    assert norm_key("Half & Half") == "half and half" and norm_key("Barbecue Sauce") == "bbq sauce"
    assert parse_line("1/3 cup all-purpose flour").food == "all purpose flour"
    # existing duplicates fold together, keeping the product someone chose by hand
    jobs.db.ensure_food("all-purpose flour", "pantry"); jobs.db.ensure_food("all purpose flour", "pantry")
    jobs.db.save_pick("all-purpose flour", {"item_id": "10lb", "title": "Great Value All-Purpose Flour, 10 lb"}, approved=False)
    jobs.db.save_pick("all purpose flour", {"item_id": "5lb", "title": "Great Value All-Purpose Flour, 5 lb"}, approved=True,
                      source="manual")
    jobs.db.run("INSERT OR REPLACE INTO parse_cache(text, food_key, category, updated) VALUES ('all-purpose flour','all-purpose flour','pantry','x')")
    jobs._merge_spelling_duplicates()
    assert jobs.db.food("all-purpose flour") is None
    assert jobs.db.pick("all purpose flour")["item_id"] == "5lb"
    row = jobs.db.one("SELECT * FROM parse_cache WHERE text = 'all purpose flour'")
    assert row["food_key"] == "all purpose flour" and row["category"] == "pantry"


def test_recipe_links(monkeypatch):
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "browser", FakeBrowser())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "shopping_week", lambda ref=None: (__import__("datetime").date(2026, 12, 6),
                                                                   __import__("datetime").date(2026, 12, 12)))
    c = TestClient(appmod.app)
    r = c.post("/api/review/build?wait=true").json()["review"]
    assert r["recipes"]["Classic crispy tacos"] == "https://mealie.example.com/g/home/r/tacos"
    assert c.get("/api/pantry").json()["recipes"]["Honey Garlic Glazed Salmon"].endswith("/g/home/r/salmon")


def test_mealie_recipe_url_uses_group_slug():
    from grocer.mealie import Mealie
    m = Mealie("http://x", "t")
    m._group_slug = "home"
    assert m.recipe_url("https://mealie.example.com", "classic-meatloaf") == \
        "https://mealie.example.com/g/home/r/classic-meatloaf"


def test_duplicate_ingredients_merge(monkeypatch):
    monkeypatch.setattr(jobs, "mealie", FakeMealie())
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    for k in ("black pepper", "cracked black pepper", "cracked pepper", "crushed red pepper",
              "dried parsley", "dried parsley leaves"):
        jobs.db.ensure_food(k, "spice")
    jobs.db.save_pick("black pepper", {"item_id": "gv3oz", "title": "Great Value Ground Black Pepper, 3 oz"}, approved=False)
    jobs.db.save_pick("cracked pepper", {"item_id": "bag10", "title": "Half Cracked Black Pepper - 10 oz Bag"}, approved=False)
    jobs.db.save_pick("dried parsley leaves", {"item_id": "mine", "title": "McCormick Parsley Flakes"}, approved=True,
                      source="manual")
    jobs.db.save_pick("dried parsley", {"item_id": "gv", "title": "Great Value Parsley Flakes, 0.4 oz"}, approved=False)
    c = TestClient(appmod.app)
    last = c.post("/api/dedupe?wait=true").json()
    assert last['state'] == 'done' and 'suggested' in last['message']
    assert jobs.db.food('cracked pepper') is not None  # scanning never mutates ingredients
    proposal = c.get('/api/cleanup').json()['proposal']
    monkeypatch.setattr(jobs, 'rebuild_soon', lambda: {})
    assert c.post('/api/cleanup/apply',json={'id':proposal['id'],'groups':list(range(len(proposal['groups'])))}).status_code == 200
    keys = {r["key"] for r in jobs.db.all("SELECT key FROM foods WHERE category = 'spice'")}
    assert {"cracked black pepper", "cracked pepper"}.isdisjoint(keys) and "black pepper" in keys
    assert "crushed red pepper" in keys                                  # a different product stays
    # parsley: you picked a product for "dried parsley leaves", so that name and product win
    assert "dried parsley" not in keys and jobs.db.pick("dried parsley leaves")["item_id"] == "mine"
    # recipe text that used an old name now lands on the merged one
    assert jobs.db.resolve("cracked pepper") == "black pepper"
    # by hand
    jobs.db.ensure_food("parsley flakes", "spice")
    assert c.post("/api/merge", json={"old": "parsley flakes", "new": "dried parsley leaves"}).status_code == 200
    assert jobs.db.food("parsley flakes") is None


class CleanupMealie(FakeMealie):
    def __init__(self):
        names = ["All-Purpose Flour", "all purpose flour", "Black Pepper", "Cracked Black Pepper",
                 "Crushed Red Pepper", "Dried Parsley", "Dried Parsley Leaves", "Butter", "Onion", "Onions"]
        self.foods = {f"id{i}": {"id": f"id{i}", "name": n, "aliases": []} for i, n in enumerate(names)}
        self.merged, self.aliased, self.backups, self.on_list = [], {}, 0, set()

    def all_foods(self):
        return list(self.foods.values())

    def merge_foods(self, from_id, to_id):
        import httpx
        if from_id in self.on_list:
            req = httpx.Request("PUT", "http://m/api/foods/merge")
            raise httpx.HTTPStatusError("boom", request=req, response=httpx.Response(500, request=req))
        self.merged.append((from_id, to_id))
        del self.foods[from_id]

    def add_aliases(self, food_id, names):
        self.aliased.setdefault(food_id, []).extend(names)

    def backup(self):
        self.backups += 1
        return True


def _by_name(m, name):
    return next(f["id"] for f in m.foods.values() if f["name"] == name)


def test_mealie_cleanup_review_then_apply(monkeypatch):
    m = CleanupMealie()

    class LowerLLM(FakeLLM):
        def find_duplicates(self, names):
            low = {n.lower(): n for n in names}
            out = [{"keep": low[g["keep"]], "same": [low[s] for s in g["same"]]}
                   for g in super().find_duplicates(list(low))]
            for g in out:   # too eager, like the real model was
                if g["keep"] == "Black Pepper" and "Crushed Red Pepper" in names:
                    g["same"].append("Crushed Red Pepper")
            return out

    monkeypatch.setattr(jobs, "mealie", m)
    monkeypatch.setattr(jobs, "llm", LowerLLM())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    jobs.db.ensure_food("cracked black pepper", "spice")
    import time
    for _ in range(100):
        if not jobs.busy():
            break
        time.sleep(0.1)
    c = TestClient(appmod.app)

    assert c.get("/mealie-cleanup").status_code == 200
    c.post("/api/mealie/cleanup/scan?wait=true")
    prop = c.get("/api/mealie/cleanup").json()["proposal"]
    assert prop["scanned"] == 10
    groups = {g["keep"]["name"]: sorted(f["name"] for f in g["same"]) for g in prop["groups"]}
    assert groups["All-Purpose Flour"] == ["all purpose flour"]        # spelling
    assert groups["Onion"] == ["Onions"]                                # plural
    assert groups["Black Pepper"] == ["Cracked Black Pepper"]           # model, by meaning
    assert groups["Dried Parsley"] == ["Dried Parsley Leaves"]
    assert not any("Crushed Red Pepper" in s for s in groups.values())
    assert m.merged == []                                               # nothing changes until you apply

    # tick only pepper and flour; flour is stuck on a shopping list
    m.on_list.add(_by_name(m, "all purpose flour"))
    pick = [{"keep": g["keep"]["id"], "keep_name": g["keep"]["name"], "same": [f["id"] for f in g["same"]]}
            for g in prop["groups"] if g["keep"]["name"] in ("Black Pepper", "All-Purpose Flour")]
    last = c.post("/api/mealie/cleanup/apply?wait=true", json={"groups": pick}).json()
    assert last["state"] == "done" and m.backups == 1
    assert [m_[1] for m_ in m.merged] == [_by_name(m, "Black Pepper")]
    assert m.aliased[_by_name(m, "Black Pepper")] == ["Cracked Black Pepper"]
    assert "Onions" in {f["name"] for f in m.foods.values()}            # unticked, left alone
    failed = last["result"]["failed"]
    assert failed and "shopping list" in failed[0]["why"]
    # this app follows Mealie
    assert jobs.db.food("cracked black pepper") is None and jobs.db.food("black pepper")
    # merged groups drop off the page; the rest stay for next time
    left = {g["keep"]["name"] for g in c.get("/api/mealie/cleanup").json()["proposal"]["groups"]}
    assert "Black Pepper" not in left and {"Onion", "All-Purpose Flour"} <= left
    # clear all
    assert c.delete("/api/mealie/cleanup").status_code == 200
    assert c.get("/api/mealie/cleanup").json() == {"proposal": None, "last_applied": None}


class PlanMealie(FakeMealie):
    """A Mealie with a real-ish meal plan: entry ids, add/delete, a tagged rotation and history."""
    POOL = [{"id": f"r{i}", "slug": f"r{i}", "name": f"Recipe {i}", "tags": ["dinner-rotation"]} for i in range(1, 9)]

    def __init__(self):
        from datetime import date as d
        self.plan = [  # (id, date, recipe)
            ("e1", "2026-10-05", "r1"), ("e2", "2026-10-06", "r2"), ("e3", "2026-10-07", "r3"),
            ("e4", "2026-10-09", "r4"),
            ("e5", "2026-10-12", "r5"),
            ("old1", "2026-09-20", "r6"), ("old2", "2026-10-01", "r7"),
        ]
        self.n = 100

    def mealplan(self, s, e):
        names = {r["id"]: r["name"] for r in self.POOL}
        return [PlanEntry(dt, "dinner", rid, rid, names[rid], id=eid)
                for eid, dt, rid in self.plan if s.isoformat() <= dt <= e.isoformat()]

    def add_mealplan(self, day, entry_type, recipe_id):
        self.n += 1
        self.plan.append((f"e{self.n}", day.isoformat(), recipe_id))

    def delete_mealplan(self, entry_id):
        self.plan = [p for p in self.plan if p[0] != entry_id]

    def tag(self, name, create=False):
        return {"id": "t1", "name": name, "slug": "dinner-rotation"}

    def recipes(self, search="", tag_id=None):
        return [r for r in self.POOL if search.lower() in r["name"].lower()]

    def recipe(self, slug):
        import dataclasses
        base = TACOS if slug in ("r1", "r3", "r5", "r7") else SALMON
        return dataclasses.replace(base, id=slug, slug=slug, name=f"Recipe {slug[1:]}")


def test_dinners_pick_swap_and_order(monkeypatch, tmp_path):
    from datetime import date
    from grocer import dinners
    from grocer.db import DB
    m = PlanMealie()
    monkeypatch.setattr(jobs, "db", DB(tmp_path / "g.db"))
    monkeypatch.setattr(jobs, "mealie", m)
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    monkeypatch.setattr(jobs, "today", lambda: date(2026, 10, 5))     # Monday, nothing ordered yet
    c = TestClient(appmod.app)
    assert c.get("/dinners").status_code == 200

    ov = c.get("/api/dinners").json()
    sel = {x["key"]: x["selected"] for x in ov["meals"]}
    # this week's dinners are ticked; next week's (Oct 12) is shown but not ticked
    assert sel == {"e1": True, "e2": True, "e3": True, "e4": True, "e5": False}
    assert "2026-10-08" in ov["empty"] and "2026-10-10" in ov["empty"]

    # untick tonight, tick next Monday
    c.post("/api/dinners/choose", json={"keys": ["e1"], "selected": False})
    c.post("/api/dinners/choose", json={"keys": ["e5"], "selected": True})
    assert dinners.selected_keys() == ["e2", "e3", "e4", "e5"]
    assert c.get("/api/settings").json()["selected_meals"] == ["e2", "e3", "e4", "e5"]

    # swap Tuesday: a different recipe, not one already planned, and it stays ticked
    planned = {rid for _, dt, rid in m.plan if dt >= "2026-09-29"}   # recent and upcoming; older ones may come back
    r = c.post("/api/dinners/swap", json={"key": "e2"}).json()
    assert r["recipe_id"] not in planned
    tue = [p for p in m.plan if p[1] == "2026-10-06"]
    assert len(tue) == 1 and tue[0][2] == r["recipe_id"]
    assert tue[0][0] in dinners.selected_keys()

    # pick a specific recipe for an empty day
    c.post("/api/dinners/set", json={"day": "2026-10-08", "recipe_id": "r8"})
    assert ("2026-10-08", "r8") in {(p[1], p[2]) for p in m.plan}

    # plan the rest: every day tomorrow..+7 gets a dinner, no repeats
    c.post("/api/dinners/fill")
    days = [p[1] for p in m.plan if "2026-10-06" <= p[1] <= "2026-10-12"]
    assert sorted(set(days)) == [f"2026-10-{d:02d}" for d in range(6, 13)]

    # the picker favors what you haven't had in a while: r7 (eaten Oct 1) loses to never-eaten ones
    import random
    picks = [dinners.pick({"r1", "r2", "r3", "r4", "r5", "r8"}, random.Random(i))["id"] for i in range(300)]
    assert picks.count("r6") > picks.count("r7")   # r6 last eaten Sep 20, r7 Oct 1

    # Supply recipe yields and pantry answers so this trip fully covers the dinners.
    for r in m.POOL:
        jobs.db.set_recipe_yield(r['id'],4)
    # ordering: build with the ticked dinners, checkout marks them ordered
    review = c.post("/api/review/build?wait=true").json()["review"]
    assert set(review["meal_keys"]) == set(dinners.selected_keys())
    for item in review['items']:
        if item['status']=='check':
            c.post('/api/stock',json={'food':item['key'],'level':'high'})
    c.post("/api/cart?force=true&wait=true")
    assert c.post("/api/checked-out").status_code == 200
    ov = c.get("/api/dinners").json()
    for x in ov["meals"]:
        if x["key"] in review["meal_keys"]:
            assert x["ordered"] and not x["selected"]
    with pytest.raises(ValueError):
        dinners.swap(review["meal_keys"][0])     # can't swap a dinner you already bought for


def test_browser_paces_page_loads():
    """Walmart's bot check reacts to pace: gaps between pages, a break after a burst, slower after a check."""
    import random
    from grocer.walmart import WalmartBrowser
    w = WalmartBrowser(Path("/tmp/none"))
    waits = []
    clock = [1000.0]
    w.sleep = lambda s: (waits.append(s), clock.__setitem__(0, clock[0] + s))
    import grocer.walmart as wm
    real_time = wm.time.time
    wm.time.time = lambda: clock[0]
    try:
        random.seed(1)
        for _ in range(9):
            w._throttle()
        assert all(10 <= x <= 12 * 1.8 + 0.01 for x in waits[:7])      # every page waits at least ~10s
        assert max(waits) >= 60                                       # the 9th page comes after a break
        waits.clear()
        w._cool_until = clock[0] + 1800                               # just had a robot check
        w._throttle()
        assert waits[0] >= 12 * 0.8 * 2.0 - 0.01
    finally:
        wm.time.time = real_time


def test_same_search_is_not_repeated():
    from grocer.walmart import WalmartBrowser
    w = WalmartBrowser(Path("/tmp/none"))
    calls = []
    w._search = lambda q, limit=12: (calls.append(q), [{"item_id": "1", "title": q}])[1]
    assert w.search("Yellow Mustard")[0]["title"] == "Yellow Mustard"
    w.search("yellow mustard ")
    assert calls == ["Yellow Mustard"]


def test_only_water_and_ice_are_never_bought(monkeypatch, tmp_path):
    from grocer.llm import LLM
    from grocer.db import DB
    llm = LLM("http://x", "m")
    monkeypatch.setattr(llm, "_chat", lambda *a, **k: {"items": [
        {"input": "chicken breast", "item": "chicken breast", "category": "none"},
        {"input": "water", "item": "water", "category": "none"},
        {"input": "garlic", "item": "garlic", "category": "produce"}]})
    cats = {o["input"]: o["category"] for o in llm.canonicalize(["chicken breast", "water", "garlic"])}
    assert cats == {"chicken breast": "other", "water": "none", "garlic": "produce"}

    # saved data from the old prompt gets repaired at startup
    db = DB(tmp_path / "g.db")
    monkeypatch.setattr(jobs, "db", db)
    for text, cat in (("chicken breast", "none"), ("water", "none"), ("soy sauce", "condiment")):
        db.run("INSERT INTO parse_cache(text, food_key, category, updated) VALUES (?,?,?,?)", text, text, cat, "x")
        db.ensure_food(text, cat)
    jobs._repair_never_bought()
    assert {r["text"] for r in db.all("SELECT text FROM parse_cache")} == {"water", "soy sauce"}
    assert db.food("chicken breast")["category"] == "other" and db.food("water")["category"] == "none"


def test_browser_learns_its_pace():
    """A robot check slows it down; a clean run speeds it up; it remembers; it stays within limits."""
    from grocer.walmart import WalmartBrowser
    w = WalmartBrowser(Path("/tmp/none"))
    saved = []
    w.on_pace = saved.append
    w._learn(hit_check=True)
    assert abs(w.min_gap - 12 * 1.5) < 0.01 and saved[-1]["checks_seen"] == 1
    for _ in range(14):
        w._learn(hit_check=False)
    assert abs(w.min_gap - 18) < 0.01                     # not yet
    w._learn(hit_check=False)
    assert abs(w.min_gap - 18 * 0.85) < 0.01              # 15 clean pages: a bit faster
    for _ in range(30):
        w._learn(hit_check=True)
    assert w.min_gap == 60                                # never slower than a minute
    for _ in range(15 * 60):
        w._learn(hit_check=False)
    assert w.min_gap == 5                                 # never faster than 5 seconds
    assert 12 <= w.per_hour() <= 120 and saved[-1]["min_gap"] == 5


def test_check_right_after_a_fresh_start_does_not_slow_down():
    """Walmart often checks the first page after a long idle; that's the session, not the speed."""
    from grocer.walmart import WalmartBrowser

    class Page:
        url = "https://www.walmart.com/blocked?x"
        def title(self): return "Robot or human?" if "blocked" in self.url else "Walmart.com"

    w = WalmartBrowser(Path("/tmp/none"))
    w._page = Page()
    w.sleep = lambda s: setattr(Page, "url", "https://www.walmart.com/search?q=x")   # someone solves it
    w._since_warm = 1
    w._wait_out_challenge()
    assert w.min_gap == 12 and w.checks_seen == 0
    Page.url = "https://www.walmart.com/blocked?x"
    w._since_warm = 5                                     # well into a session: that one counts
    w._wait_out_challenge()
    assert w.min_gap == 18 and w.checks_seen == 1


def test_use_from_search_does_not_reopen_the_page(monkeypatch):
    b = FakeBrowser()
    monkeypatch.setattr(jobs, "browser", b)
    monkeypatch.setattr(jobs, "llm", FakeLLM())
    monkeypatch.setattr(jobs, "ha", FakeHA())
    c = TestClient(appmod.app)
    c.post("/api/review/build?wait=true")
    found = b._listing("Land O Lakes Salted Butter, 16 oz", "777001")
    b.cached_listing = lambda iid: dict(found) if iid == "777001" else None
    b.item = lambda iid: (_ for _ in ()).throw(AssertionError("opened the product page"))
    r = c.post("/api/swap", json={"key": "butter", "url": "https://www.walmart.com/ip/x/777001"})
    assert r.status_code == 200 and jobs.db.pick("butter")["item_id"] == "777001"


def test_pictures_for_unsaved_listings_only_from_walmart(monkeypatch, tmp_path):
    monkeypatch.setattr(jobs, "IMAGE_DIR", tmp_path)
    fetched = []

    class R:
        headers = {"content-type": "image/jpeg"}
        content = b"jpg"
        def raise_for_status(self): pass

    monkeypatch.setattr(jobs.httpx, "get", lambda url, **kw: (fetched.append(url), R())[1])
    assert jobs.image_file("987654", src="https://evil.example.com/x.jpg") is None
    assert jobs.image_file("987654", src="https://i5.walmartimages.com/asr/abc.jpeg?odnHeight=180") is not None
    assert fetched == ["https://i5.walmartimages.com/asr/abc.jpeg"]


def test_search_falls_back_when_typing_fails():
    """Typing into Walmart's search box is preferred, but any trouble there must not break searching."""
    from grocer.walmart import WalmartBrowser
    w = WalmartBrowser(Path("/tmp/none"))
    w.store_id = "1234"
    went = []
    w._ready = lambda: None
    w._typed_search = lambda q: (_ for _ in ()).throw(RuntimeError("no search box"))
    w._goto = lambda url, ready=True: went.append((url, ready))
    w._next_data = lambda: {"props": {"pageProps": {"initialData": {"searchResult": {"itemStacks": [{"items": [
        {"usItemId": "42", "name": "Great Value Butter, 16 oz", "availabilityStatusV2": {"value": "IN_STOCK"},
         "fulfillmentOptions": [{"type": "PICKUP", "storeId": "1234", "availabilityStatus": "IN_STOCK"}],
         "fulfillmentBadgeGroups": [{"text": "Pickup", "slaText": "today"}]}]}]}}}}}
    res = w._search("butter")
    assert went == [("https://www.walmart.com/search?q=butter", False)]
    assert [r["item_id"] for r in res] == ["42"] and res[0]["local_reason"] == "Pickup today"


def test_scheduled_job_retries_then_notifies(monkeypatch):
    import threading
    monkeypatch.setattr(jobs, "SCHEDULE_RETRY_SECONDS", 0.01)
    sent = []
    class RecHA(FakeHA):
        def notify(self, title, message, **kw):
            sent.append(title)
    monkeypatch.setattr(jobs, "ha", RecHA())
    gate = threading.Event()
    blocker = jobs.start("history", "Checking", lambda: gate.wait(5))
    ran = []
    jobs.start_scheduled("build", "Building", lambda: ran.append(1))
    for _ in range(100):
        if sent:
            break
        __import__("time").sleep(0.05)
    assert sent == ["Grocer could not start a job"] and not ran
    gate.set()
    jobs.wait_for(blocker)
    jobs.start_scheduled("build", "Building", lambda: ran.append(1))
    for _ in range(100):
        if ran:
            break
        __import__("time").sleep(0.05)
    assert ran == [1]


def test_finished_futures_are_pruned():
    first = jobs.start("history", "a", lambda: None)
    jobs.wait_for(first)
    second = jobs.start("history", "b", lambda: None)
    jobs.wait_for(second)
    assert first["id"] not in jobs.FUTURES


def test_missing_tokens_warn(monkeypatch, caplog):
    monkeypatch.setattr(appmod.CONFIG, "mealie_token", "")
    monkeypatch.setattr(appmod.CONFIG, "ha_token", "")
    with caplog.at_level("WARNING", logger="grocer"):
        appmod.warn_missing_tokens()
    assert "MEALIE_TOKEN is empty" in caplog.text and "HA_TOKEN is empty" in caplog.text

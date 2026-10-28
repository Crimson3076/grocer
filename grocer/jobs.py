"""The weekly jobs: build the Saturday review, fill the Sunday cart."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from urllib.parse import urlparse
from functools import wraps
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from . import history, planner
from .config import CONFIG
from .db import DB, now_iso
from .ha import HA
from .llm import LLM
from .mealie import Mealie
from .parse import clean_food, singular
from .walmart import BrowserChallenge, LoginRequired, WalmartBrowser, item_id_from_url

log = logging.getLogger(__name__)

# One thread owns the browser; every job runs on it so nothing overlaps.
WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="grocer")
REVIEW_LOCK = threading.RLock()


def review_edit(fn):
    @wraps(fn)
    def locked(*args, **kwargs):
        with REVIEW_LOCK:
            return fn(*args, **kwargs)
    return locked

db = DB(CONFIG.db_path)
mealie = Mealie(CONFIG.mealie_url, CONFIG.mealie_token)
llm = LLM(CONFIG.ollama_url, CONFIG.ollama_model)
ha = HA(CONFIG.ha_url, CONFIG.ha_token, CONFIG.notify_targets)
browser = WalmartBrowser(CONFIG.profile_dir, CONFIG.browser_channel, CONFIG.browser_delay)


def walmart_settings() -> dict:
    return db.settings("walmart", {"store_id": CONFIG.walmart_store_id,
                                   "fulfillment": CONFIG.walmart_fulfillment})


def configure_browser() -> dict:
    settings = walmart_settings()
    browser.store_id = settings["store_id"]
    browser.fulfillment = settings["fulfillment"]
    return settings


def locally_available(pick: dict | None) -> bool:
    """Only explicit availability for the configured store and method counts."""
    if not pick or not pick.get("local_available"):
        return False
    settings = walmart_settings()
    return bool(settings["store_id"] and str(pick.get("store_id") or "") == settings["store_id"]
                and pick.get("fulfillment") == settings["fulfillment"])


@review_edit
def set_walmart_settings(store_id: str, fulfillment: str) -> None:
    if busy():
        raise RuntimeError("Wait for the current job to finish before changing stores.")
    row = db.one("SELECT 1 FROM cart_runs WHERE state != 'checked_out' LIMIT 1")
    if row:
        raise RuntimeError("Finish the current cart before changing stores.")
    db.save_settings("walmart", {"store_id": store_id, "fulfillment": fulfillment})
    row = db.latest_review()
    if row and row["status"] in {"pending", "approved", "failed"}:
        data = json.loads(row["data"])
        data['stale'] = True
        for item in data["items"]:
            if item.get("pick"):
                item["pick"]["local_available"] = False
            item["alternatives"] = []
        db.update_review(row["id"], data=data, status="pending")


def editable_review() -> None:
    with _plock:
        if STATE.get("job") and STATE["job"]["kind"] == "cart":
            raise RuntimeError("Wait for the cart check to finish before editing the list.")
    row = db.latest_review()
    if row and (db.cart_run(row["week_start"], row["id"]) or row["status"] in {"carted", "checked_out"}):
        raise RuntimeError("This order is already in progress. Finish it in Walmart before editing a new list.")

def delete_food(key: str, keep_manual: bool = False) -> None:
    """Forget an ingredient only when no active trip or dinner depends on it."""
    ingredient_edit_allowed()
    if db.one('SELECT 1 FROM reservations WHERE food_key=?', key):
        raise RuntimeError('Mark the dinners using this ingredient cooked or skipped before deleting it.')
    pick = db.pick(key)
    if keep_manual and pick and pick["source"] == "manual":
        return
    with db.tx() as c:
        for table, col in (("picks", "key"), ("stock", "key"), ("foods", "key"), ("aliases", "key"),
                           ("parse_cache", "food_key")):
            c.execute(f"DELETE FROM {table} WHERE {col} = ?", (key,))


def _repair_generic_names() -> None:
    """Undo earlier over-shortened names ("bbq sauce" saved as "sauce") so they get picked again properly."""
    fixed = []
    for r in db.all("SELECT text, food_key FROM parse_cache"):
        good = planner.keep_specific(r["text"], r["food_key"])
        if good != r["food_key"]:
            db.run("UPDATE parse_cache SET food_key = ? WHERE text = ?", good, r["text"])
            fixed.append((r["text"], r["food_key"]))
    for _, generic in fixed:
        # the generic pick was made for the wrong product; drop it unless someone chose it by hand
        db.run("DELETE FROM picks WHERE key = ? AND COALESCE(source,'search') != 'manual'", generic)
    if fixed:
        log.info("repaired over-generic names: %s", fixed)
    # an earlier parser kept "each" in names ("each salt and pepper"); forget those so lines get re-read
    junk = [r["food_key"] for r in db.all("SELECT food_key FROM parse_cache WHERE text LIKE 'each %'")]
    if junk:
        db.run("DELETE FROM parse_cache WHERE text LIKE 'each %'")
        for key in junk:
            if not db.one("SELECT 1 FROM parse_cache WHERE food_key = ?", key):
                delete_food(key, keep_manual=True)
        log.info("removed misread names: %s", junk)


# Legacy repairs are available for diagnosis, never run automatically.


PICK_RANK = {"manual": 3, "history": 2, "search": 1}


def merge_into(old: str, new: str) -> None:
    """Make 'old' the same ingredient as 'new', keeping the better product choice, stock and settings."""
    if old == new or not db.food(old):
        return
    db.ensure_food(new, db.food(old)["category"])
    po, pn = db.pick(old), db.pick(new)
    if po and (not pn or (PICK_RANK.get(po["source"] or "search", 1), po["approved"])
               > (PICK_RANK.get(pn["source"] or "search", 1), pn["approved"])):
        db.save_pick(new, dict(po, image=po["image_url"]), approved=bool(po["approved"]), source=po["source"] or "search")
    so, sn = db.stock(old), db.stock(new)
    if so and not sn:
        db.run('''INSERT INTO stock(key,packages,confidence,updated,last_bought,restock,quantity,dimension,basis_item_id)
                  SELECT ?,packages,confidence,updated,last_bought,restock,quantity,dimension,basis_item_id
                  FROM stock WHERE key=?''', new, old)
        normalized = db.stock(new)
        if normalized['packages'] is None:
            db.run('UPDATE stock SET confidence=MIN(confidence,0.3) WHERE key=?', new)
        else:
            db.run('UPDATE stock SET packages=? WHERE key=?', normalized['packages'], new)
    if so and so["restock"]:
        db.set_restock(new, True)
    fo, fn = db.food(old), db.food(new)
    if fo["staple"] or fo["brand_lock"]:
        db.set_food(new, staple=max(fo["staple"], fn["staple"]), brand_lock=max(fo["brand_lock"], fn["brand_lock"]))
    from .pantry import _convert
    for reservation in db.all('SELECT * FROM reservations WHERE food_key=?', old):
        target = db.one('SELECT * FROM reservations WHERE meal_key=? AND food_key=?', reservation['meal_key'], new)
        dim = target['dimension'] if target else reservation['dimension']
        quantity = _convert(reservation['quantity'], reservation['dimension'], dim, new)
        if quantity is None:
            raise RuntimeError('The reserved ingredient amounts cannot be combined safely. Keep these ingredients separate.')
        db.run('INSERT INTO reservations VALUES (?,?,?,?) ON CONFLICT(meal_key,food_key) DO UPDATE SET quantity=excluded.quantity',
               reservation['meal_key'], new, quantity + (target['quantity'] if target else 0), dim)
    db.run('DELETE FROM reservations WHERE food_key=?', old)
    db.merge_food(old, new)


def _merge_spelling_duplicates() -> None:
    """Fold 'all-purpose flour' into 'all purpose flour' (and the like), keeping the better product choice."""
    from .parse import norm_key
    merged = []
    for row in db.all("SELECT key FROM foods"):
        old = row["key"]
        new = norm_key(old)
        if not new or new == old:
            continue
        merge_into(old, new)
        merged.append(f"{old} -> {new}")
    for r in db.all("SELECT text, food_key, category FROM parse_cache"):
        t, k = norm_key(r["text"]), norm_key(r["food_key"])
        if (t, k) != (r["text"], r["food_key"]):
            db.run("DELETE FROM parse_cache WHERE text = ?", r["text"])
            db.run("INSERT OR REPLACE INTO parse_cache(text, food_key, category, updated) VALUES (?,?,?,?)",
                   t, k, r["category"], now_iso())
    if merged:
        log.info("merged spelling duplicates: %s", merged)


# Legacy repairs are available for diagnosis, never run automatically.


def _repair_pairs_and_choices() -> None:
    """Older parsing kept "salt & pepper" as one thing ("salt pepper") and "butter or margarine" as one.
    Drop the first (recipes get re-read as salt + pepper) and fold the second into its first choice."""
    import re as _re
    from .parse import clean_food
    fixed = []
    for row in db.all("SELECT key FROM foods"):
        key = row["key"]
        if _re.fullmatch(r"(?:\w+ )?salt ?(?:\w+ )?pepper", key) and " and " not in f" {key} ":
            delete_food(key, keep_manual=True)
            fixed.append(f"{key} (removed)")
        elif " or " in f" {key} ":
            new = clean_food(key)
            if new and new != key:
                merge_into(key, new)
                db.run("DELETE FROM parse_cache WHERE food_key = ?", key)
                fixed.append(f"{key} -> {new}")
    if fixed:
        log.info("fixed combined ingredient names: %s", fixed)


# Legacy repairs are available for diagnosis, never run automatically.


def _repair_never_bought() -> None:
    """An older prompt let the model mark meat, produce and dairy as 'none' (never bought), so they were
    silently left off every list. Only water and ice are 'none'; everything else gets re-read next build."""
    import re as _re
    bad = [r["text"] for r in db.all("SELECT text, food_key FROM parse_cache WHERE category = 'none'")
           if not _re.search(r"\b(?:water|ice)\b", r["text"] + " " + r["food_key"])]
    for t in bad:
        db.run("DELETE FROM parse_cache WHERE text = ?", t)
    foods = [r["key"] for r in db.all("SELECT key FROM foods WHERE category = 'none'")
             if not _re.search(r"\b(?:water|ice)\b", r["key"])]
    for k in foods:
        db.run("UPDATE foods SET category = 'other' WHERE key = ?", k)
    if bad or foods:
        log.info("re-reading %d ingredients that were wrongly marked 'never bought': %s", len(set(bad) | set(foods)),
                 sorted(set(bad) | set(foods)))


# Legacy repairs are available for diagnosis, never run automatically.

REVIEW_TAG = "grocer_review"
STATUS_TAG = "grocer_status"
CHALLENGE_TAG = "grocer_challenge"

def _on_challenge() -> None:
    progress(waiting=True, step="Walmart wants a quick robot check")
    ha.notify(
    "Walmart wants a robot check",
    "The grocery helper is paused. Open the browser and press and hold the button, it picks up where it left off.",
    tag=CHALLENGE_TAG, url=CONFIG.vnc_url,
    actions=[{"action": "URI", "title": "Open browser", "uri": CONFIG.vnc_url}],
    )


def _on_challenge_cleared() -> None:
    progress(waiting=False, step="Robot check cleared, carrying on")
    ha.clear(CHALLENGE_TAG)          # clears it on every phone, whoever solved it
    resolve_alert()


def resolve_alert() -> None:
    """A "cart needs you" alert is fixed (signed back in, check solved): clear it from every phone."""
    with _plock:
        open_ = STATE.pop("alert_open", False)
    if open_:
        ha.clear(STATUS_TAG)


def raise_alert() -> None:
    with _plock:
        STATE["alert_open"] = True


browser.on_challenge = _on_challenge
browser.on_challenge_cleared = _on_challenge_cleared
browser.on_progress = lambda msg: progress(step=msg)
def _login_ok():
    db.save_settings('walmart_session', {'signed_in': True, 'checked': __import__('grocer.db', fromlist=['now_iso']).now_iso()})
    resolve_alert()

browser.on_login_ok = _login_ok
# the browsing pace is learned and remembered across restarts (BROWSER_MIN_GAP is only the starting point)
_pace = db.settings("browser_pace") or {}
# an earlier version blamed every robot check on speed and crawled; start from a sane pace again
browser.min_gap = min(float(_pace.get("min_gap") or CONFIG.browser_min_gap), 20.0)
browser.checks_seen = int(_pace.get("checks_seen") or 0)
browser.on_pace = lambda p: db.save_settings("browser_pace", p)


def _on_wait(seconds: float, why: str) -> None:
    msg = f"Pausing so Walmart doesn't flag us as a bot ({why})"
    if STATE.get("job") or STATE.get("bg"):
        progress(step=msg, until=time.time() + seconds)   # the page counts this down
    log.info("%s: %ds", msg, round(seconds))


browser.on_wait = _on_wait


def submit(fn, *args, **kwargs) -> Future:
    return WORKER.submit(_guard, fn, *args, **kwargs)


# ---------------------------------------------------------------- background jobs with progress

_plock = threading.Lock()
STATE: dict = {"job": None, "last": None, "version": 1}
FUTURES: dict[str, Future] = {}


def changed() -> None:
    """Something on the pages changed; open pages reload when they see a new version."""
    with _plock:
        STATE["version"] += 1


def progress(**kw) -> None:
    with _plock:
        target = STATE["job"] or STATE.get("bg")
        if target:
            target.update(kw)


class activity:
    """Smaller background work that still uses the browser; shown in the same top bar."""

    def __init__(self, label: str, step: str = ""):
        self.info = {"label": label, "step": step, "waiting": False}

    def __enter__(self):
        with _plock:
            STATE["bg"] = self.info
        return self

    def __exit__(self, *exc):
        with _plock:
            if STATE.get("bg") is self.info:
                STATE["bg"] = None
        return False


def busy() -> bool:
    with _plock:
        return STATE["job"] is not None


def start(kind: str, label: str, fn, *args, **kwargs) -> dict:
    """Run a long task on the browser thread and report progress. One at a time."""
    with _plock:
        if STATE["job"]:
            raise RuntimeError('Another task is running. This action was not queued. Try again when it finishes.')
        job = {"id": uuid.uuid4().hex[:8], "kind": kind, "label": label, "step": "Starting",
               "done": 0, "total": 0, "waiting": False, "started": time.time()}
        STATE["job"] = job

    from . import task_history
    try:
        task_history.record(db, job, fn, args, kwargs)
    except Exception:
        with _plock:
            STATE['job'] = None
        raise

    def run():
        task_history.finish(db, job, 'running')
        try:
            result = fn(*args, **kwargs)
            failed = kind == "cart" and isinstance(result, dict) and result.get("failed")
            _finish("failed" if failed else "done", str(failed) if failed else None, _message(kind, result), result)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", kind)
            _finish("failed", _friendly(e), None, None)
            raise

    FUTURES[job["id"]] = WORKER.submit(run)
    return dict(job, state='accepted')


def _finish(state: str, error: str | None, message: str | None, result) -> None:
    with _plock:
        job = STATE["job"] or {}
        job.update(state=state, error=error, message=message, finished=time.time(), waiting=False)
        job["result"] = result if isinstance(result, dict) and job.get("kind") != "build" else None
        STATE["last"], STATE["job"] = job, None
        STATE["version"] += 1
    from . import task_history
    task_history.finish(db, job, state, error)
    task_history.dispatch_rebuild()


def rebuild_soon() -> dict:
    """Persist and coalesce rebuild requests without taking database locks under the progress lock."""
    from .task_history import queue_rebuild
    return queue_rebuild()


def _message(kind: str, result) -> str:
    r = result if isinstance(result, dict) else {}
    if kind == "build":
        s = summary(r) if r.get("items") is not None else {}
        return f"List updated: {s.get('buy', 0)} item{'s' if s.get('buy', 0) != 1 else ''}, about ${s.get('total', 0):.0f}" if s else "List updated"
    if kind == "clear":
        if r.get('protected'):
            return r['message']
        n = r.get("removed", 0)
        msg = f"Took {n} item{'s' if n != 1 else ''} out of your Walmart cart"
        return msg + (f", {r['left']} still there" if r.get("left") else "")
    if kind == "plan":
        n = len(r.get("added", []))
        return f"Planned {n} dinner{'s' if n != 1 else ''}" if n else "Every day already has a dinner"
    if kind == "history":
        n = len(r.get("switched", []))
        return f"Found {r.get('products', 0)} things you've bought before" + (f", switched {n} picks to them" if n else "")
    if kind == "mealie-scan":
        return f"Found {len(r.get('groups', []))} groups to review" if r.get("groups") else "Mealie's ingredients look tidy"
    if kind == "mealie-apply":
        n = sum(len(d["merged"]) for d in r.get("done", []))
        msg = f"Merged {n} ingredient{'s' if n != 1 else ''} in Mealie"
        if r.get("failed"):
            msg += f", {len(r['failed'])} couldn't be merged"
        return msg
    if kind == "dedupe":
        n = len(r.get('groups', []))
        return f'{n} suggested ingredient groups ready to review' if n else 'No duplicates found'
    if kind == "cart":
        if r.get("failed"):
            return r["failed"]
        if r.get("skipped"):
            return "Nothing to do: " + r["skipped"]
        return f"{r.get('added', 0)} grocery lines verified in your Walmart cart"
    return "Done"


def status() -> dict:
    with _plock:
        bg = dict(STATE['bg']) if STATE.get('bg') else None
        if bg and _image_queue:
            bg['left'] = len(_image_queue)
        result = {'job':dict(STATE['job']) if STATE['job'] else None,
                  'bg':bg, 'last':dict(STATE['last']) if STATE['last'] else None,
                  'version':STATE['version']}
    from .task_history import recovery
    result['recovery'] = recovery(db)
    return result


def _friendly(e: Exception) -> str:
    if isinstance(e, LoginRequired):
        from .db import now_iso
        db.save_settings('walmart_session', {'signed_in': False, 'checked': now_iso()})
        return "Walmart sign-in expired. Open the browser and sign in, then try again."
    if isinstance(e, BrowserChallenge):
        return "Walmart's robot check wasn't solved in time. Open the browser, press and hold, then try again."
    if isinstance(e, httpx.HTTPError):
        return "Couldn't reach Mealie or Ollama on the server. Try again in a minute."
    return str(e) or e.__class__.__name__


def wait_for(job: dict, timeout: float = 900) -> None:
    fut = FUTURES.get(job["id"])
    if fut:
        try:
            fut.result(timeout=timeout)
        except Exception:  # noqa: BLE001  the failure is reported through STATE
            pass


def _guard(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except Exception:  # noqa: BLE001
        log.exception("job %s failed", getattr(fn, "__name__", fn))
        raise


def today() -> date:
    return datetime.now(ZoneInfo(CONFIG.tz)).date()


def shopping_week(ref: date | None = None) -> tuple[date, date]:
    """First and last date of the dinners ticked for the next order (today, if none are)."""
    from . import dinners
    days = [date.fromisoformat(e.date) for e in dinners.selected()]
    t = ref or today()
    return (min(days), max(days)) if days else (t, t)


# ---------------------------------------------------------------- review

def _from_history(key: str, need: planner.Need) -> dict | None:
    """The exact product the household bought before, if one matches this ingredient."""
    prev = history.candidates(key, db.purchases())
    if not prev:
        return None
    try:
        idx = llm.choose_product(key, need.text(), prev, history=True, recipe_lines=need.lines)["index"]
    except Exception as e:  # noqa: BLE001
        log.warning("history choose failed for %s: %s", key, e)
        return None
    if not 0 <= idx < len(prev):
        return None
    live = browser.item(prev[idx]["item_id"])
    return live if locally_available(live) else None


RECHECK_AFTER = 7 * 24 * 3600   # a saved pick checked within a week isn't reopened on Walmart


def recently_checked(key: str, hours: float = 24) -> bool:
    row = db.pick(key)
    if not row or not row["updated"] or not locally_available(dict(row)):
        return False
    return (datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(row["updated"])).total_seconds() < hours * 3600


def _pick_for(key: str, need: planner.Need) -> tuple[dict | None, list[dict], bool]:
    """Saved pick, else something bought before, else search + let the model choose.
    Returns (pick, alternatives, needs_a_look)."""
    configure_browser()
    row = db.pick(key)
    if row:
        if locally_available(dict(row)) and (CONFIG.cart_verify_after_add or (row["updated"] and (datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(row["updated"])).total_seconds() < RECHECK_AFTER)):
            return dict(row), [], not row["approved"]
        db.run("UPDATE picks SET local_available=0 WHERE key=?", key)
        live = browser.item(row["item_id"])
        if locally_available(live):
            db.save_pick(key, live, approved=bool(row["approved"]), source=row["source"])
            return dict(db.pick(key)), [], not row["approved"]
        db.run("UPDATE picks SET local_available=0, local_reason=? WHERE key=?",
               (live or {}).get("local_reason") or "Local stock could not be verified", key)
        food = db.food(key)
        if food and food["brand_lock"]:
            return None, [], False
    prev = _from_history(key, need)
    if prev:
        db.save_pick(key, prev, approved=True, source="history")   # you already buy this: no review needed
        return dict(db.pick(key)), [], False
    try:
        cands = [p for p in browser.search(key, limit=8) if locally_available(p)]
        # from your store (pickup or delivery) beats shipped, when there's a choice
        at_store = [p for p in cands if p.get("method") in ("pickup", "delivery")]
        cands = at_store or cands
    except BrowserChallenge:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("search failed for %s: %s", key, e)
        return None, [], False
    if not cands:
        return None, [], False
    bought = db.purchased_ids()
    for c in cands:
        c["bought_before"] = c["item_id"] in bought
    try:
        choice = llm.choose_product(key, need.text(), cands, usual_brands=db.top_brands(), recipe_lines=need.lines)
        idx = choice["index"]
    except Exception as e:  # noqa: BLE001
        log.warning("choose failed for %s: %s", key, e)
        idx = next((i for i, c in enumerate(cands) if c["in_stock"]), 0)
    if not 0 <= idx < len(cands):
        return None, cands[:4], False
    pick = cands[idx]
    db.save_pick(key, pick, approved=False)
    alts = [c for i, c in enumerate(cands) if i != idx][:3]
    return dict(db.pick(key)), alts, True


HISTORY_MAX_AGE_DAYS = 7


def sync_history(recheck: bool = True) -> dict:
    """Pull purchase history from Walmart, then switch searched picks to things you've bought before."""
    configure_browser()
    progress(step="Reading your past Walmart orders")
    items = browser.purchase_history()
    if items:   # a full re-read: replace, so anything wrongly saved before doesn't linger
        db.run("DELETE FROM purchases")
    db.save_purchases(items)
    db.log("history_synced", count=len(items))
    changed = []
    if recheck:
        # anything the model searched for gets re-checked; picks you chose with Swap are left alone
        rows = db.all("SELECT key FROM picks WHERE COALESCE(source,'search') = 'search'")
        for n, row in enumerate(rows, 1):
            key = row["key"]
            progress(done=n - 1, total=len(rows), step=f"Matching {key} to past orders")
            need = _need_from_pending(key) or planner.Need(key=key, category=(db.food(key) or {"category": "other"})["category"])
            prev = _from_history(key, need)
            if prev:
                db.save_pick(key, prev, approved=True, source="history")
                changed.append(key)
        if changed:
            _refresh_pending(lambda it: it["key"] in changed)
    log.info("history sync: %d products, %d picks switched to past purchases", len(items), len(changed))
    return {"products": len(items), "brands": db.top_brands(10), "switched": changed}


def _need_from_pending(key: str) -> planner.Need | None:
    row = db.latest_review()
    if not row:
        return None
    for i in json.loads(row["data"])["items"]:
        if i["key"] == key:
            return planner.Need(key=key, category=i["category"], amounts=i["amounts"],
                                unquantified=i["unquantified"], optional_only=i["optional_only"],
                                sources=i["sources"], lines=i["lines"])
    return None


def _history_stale() -> bool:
    last = db.last_event("history_synced")
    if not last:
        return True
    age = datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(last)
    return age.days >= HISTORY_MAX_AGE_DAYS


def _item_entry(need: planner.Need, pick: dict | None, alts: list[dict], is_new: bool, on_hand: set[str]) -> dict:
    if not locally_available(pick):
        pick = None
    alts = [alt for alt in alts if locally_available(alt)]
    food = db.food(need.key)
    stock = db.stock(need.key)
    staple = bool(food and food["staple"]) or need.key in on_hand
    from .pantry import reserved_packages, reservation_uncertain
    available = stock['packages'] if stock and stock['confidence'] >= 0.5 and not reservation_uncertain(need.key, need.meal_keys) else None
    if available is not None:
        available = max(available - reserved_packages(need.key, need.meal_keys), 0)
    status, qty = planner.decide(need, pick, available, staple)
    if need.optional_only and not stock and status == "buy" and need.category in planner.ASK_CATEGORIES:
        status, qty = "check", 0
    if stock and stock['restock'] and pick:
        status, qty = 'buy', max(1, qty)
    if qty > 0 and not pick:
        status, qty = "nopick", 0
    use_pk = None
    if pick:
        use_pk = planner.packages_needed(need, pick.get("size_qty"), pick.get("size_dim"), pick.get("sales_unit"))
    return {
        "meal_keys": need.meal_keys,
        "meal_usage": need.meal_usage,
        "use_pk": use_pk,
        "key": need.key,
        "category": need.category,
        "need_text": need.text(),
        "amounts": need.amounts,
        "unquantified": need.unquantified,
        "optional_only": need.optional_only,
        "sources": need.sources,
        "lines": need.lines,
        "status": status,
        "qty": qty,
        "staple": staple,
        "brand_lock": bool(food and food["brand_lock"]),
        "stock": available,
        "reserved": reserved_packages(need.key, need.meal_keys),
        "pick": pick,
        "new_pick": is_new,
        "alternatives": alts,
    }


def recompute_item(item: dict, on_hand: set[str] | None = None) -> dict:
    need = planner.Need(key=item["key"], category=item["category"], amounts=item["amounts"],
                        unquantified=item["unquantified"], optional_only=item["optional_only"],
                        sources=item["sources"], lines=item["lines"],
                        meal_keys=item.get("meal_keys", []), meal_usage=item.get("meal_usage", {}))
    if item.get("status") == "skip":
        return item
    row = db.pick(item["key"])
    pick = dict(row) if row else None
    is_new = bool(row and not row["approved"])
    fresh = _item_entry(need, pick, item.get("alternatives", []), is_new, on_hand or set())
    if item.get("manual_qty"):
        fresh.update(qty=item["qty"], manual_qty=True,
                     status="buy" if item["qty"] > 0 and fresh.get("pick") else "covered" if item["qty"] == 0 else "nopick")
    return fresh


def build_review(notify: bool = True) -> dict:
    began = time.time()
    start, end = shopping_week()
    prev = db.latest_review()
    if prev and prev["status"] != "checked_out" and db.cart_run(prev["week_start"], prev["id"]):
        raise RuntimeError('Finish or reopen this shopping trip before rebuilding the list.')
    if db.one("SELECT 1 FROM cart_runs WHERE state != 'checked_out' LIMIT 1"):
        raise RuntimeError("Confirm checkout or reset the current cart plan before building another week.")
    progress(step="Reading the dinners you picked from Mealie")
    from . import dinners
    entries = dinners.selected()
    from .trips import input_version
    keys = {dinners.key_of(e) for e in entries}
    recipes = {}
    for e in entries:
        if e.recipe_id and e.recipe_id not in recipes:
            recipes[e.recipe_id] = mealie.recipe(e.recipe_slug or e.recipe_id)
    version = input_version(entries, recipes)
    meals = planner.build_meals(entries, recipes, db, CONFIG.default_servings, CONFIG.max_trusted_yield)
    needs, unparsed = planner.gather_needs(meals, recipes, db, llm)
    from .parse import norm_key
    on_hand = {db.resolve(norm_key(name)) for name in mealie.on_hand_foods()}
    if on_hand:
        log.info("Mealie marks these foods as on hand (treated like staples): %s", sorted(on_hand))
    # things someone said we're out of or low on, even if no planned meal uses them
    for key in db.restock_keys():
        if key not in needs:
            food = db.food(key)
            needs[key] = planner.Need(key=key, category=food["category"] if food else "other",
                                      unquantified=True, optional_only=False, sources=["Restock"])

    # Keep full history browsing off the normal shopping path. Import on first setup
    # before any picks exist; later imports are explicit through Settings.
    login_ok = True
    def covered_staple(key):
        food, stock = db.food(key), db.stock(key)
        return ((food and food["staple"]) or key in on_hand) and not (
            stock and (stock["packages"] is None or stock["packages"] <= planner.STOCK_LEVELS["low"]))

    cached_only = all(db.pick(k) and locally_available(dict(db.pick(k))) for k in needs
                      if needs[k].category != "none" and not covered_staple(k))
    refresh_history = not cached_only and _history_stale() and not db.one("SELECT 1 FROM picks LIMIT 1")
    if (notify and not cached_only) or refresh_history:
        try:
            browser.ensure_logged_in()
        except LoginRequired:
            login_ok = False
        except BrowserChallenge:
            pass  # handled per search below
    if login_ok and refresh_history:
        try:
            sync_history(recheck=False)
        except Exception as e:  # noqa: BLE001  never let history block the weekly list
            log.warning("history sync failed: %s", e)

    items = []
    blocked = False
    ordered = sorted(needs, key=lambda k: (needs[k].category, k))
    for n, key in enumerate(ordered, 1):
        need = needs[key]
        progress(done=n - 1, total=len(ordered),
                 step=f"Checking {key}" if db.pick(key) else f"Finding {key} at Walmart")
        if need.category == "none":
            continue
        if covered_staple(key):
            items.append(_item_entry(need, dict(db.pick(key)) if db.pick(key) else None, [], False, on_hand))
            continue
        if blocked:
            pick, alts, is_new = (dict(db.pick(key)), [], False) if db.pick(key) else (None, [], False)
        else:
            try:
                pick, alts, is_new = _pick_for(key, need)
            except BrowserChallenge:
                blocked = True    # nobody cleared the robot check in time: finish without searching
                pick, alts, is_new = None, [], False
        items.append(_item_entry(need, pick, alts, is_new, on_hand))

    with REVIEW_LOCK:
        # rebuilding mid-review (servings changed, etc.) keeps the edits already made
        status = "pending"
        prev = db.latest_review()
        if prev and prev["status"] in {"pending", "approved"} and not db.cart_run(prev["week_start"], prev["id"]):
            status = "pending"  # a changed/rebuilt list must be approved again
            old = {i["key"]: i for i in json.loads(prev["data"])["items"]}
            existing_keys = {i["key"] for i in items}
            for o in old.values():
                if o["key"] not in existing_keys and "Restock" in o.get("sources", []) and o["key"] in db.restock_keys():
                    items.append(recompute_item(o, on_hand))
            for it in items:
                o = old.get(it["key"])
                if not o:
                    continue
                if not it["alternatives"] and o.get("alternatives"):
                    it["alternatives"] = o["alternatives"]
                if o["status"] == "skip":
                    it["status"], it["qty"] = "skip", 0
                elif o.get("manual_qty"):
                    it["qty"], it["manual_qty"] = o["qty"], True
                    it["status"] = "buy" if o["qty"] > 0 else "covered"

        items = [recompute_item(it, on_hand) if it["status"] != "skip" else it for it in items]
        by_status: dict[str, list[str]] = {}
        for it in items:
            by_status.setdefault(it["status"], []).append(f'{it["key"]} ({it.get("category")})')
        log.info("list for %d dinners: %s", len(meals), {k: sorted(v) for k, v in by_status.items()})
        data = {
            "input_version": version,
            "week_start": start.isoformat(),
            "week_end": end.isoformat(),
            "built": datetime.now(ZoneInfo(CONFIG.tz)).isoformat(timespec="minutes"),
            "meals": [m.__dict__ for m in meals],
            "meal_keys": sorted(keys),
            "items": items,
            "unparsed": unparsed,
            "login_ok": login_ok,
            "blocked": blocked,
            "recipes": _recipe_links(recipes),
            "walmart": walmart_settings(),
        }
        review_id = db.save_review(start.isoformat(), data, status=status)
    changed()
    db.log("review_built", id=review_id, meals=len(meals), items=len(items))
    if notify:
        notify_review(data)
    elif time.time() - began > 90:
        s = summary(data)
        needs = len(s["checks"]) + len(s["yield_questions"]) + len(s["no_pick"]) + len(s["new_picks"])
        ha.notify("Your grocery list is ready",
                  f"{needs} thing{'s' if needs != 1 else ''} need{'' if needs != 1 else 's'} you." if needs
                  else f"{s['buy']} items, about ${s['total']:.0f}. Ready to send to Walmart.",
                  tag=REVIEW_TAG, url=f"{CONFIG.public_url}/review")
    return data


def summary(data: dict) -> dict:
    buy = [i for i in data["items"] if i["status"] == "buy"]
    total = sum(((i["pick"] or {}).get("price") or 0) * i["qty"] for i in buy)
    return {
        "buy": len(buy),
        "total": round(total, 2),
        "unknown_prices": [i["key"] for i in buy if (i["pick"] or {}).get("price") is None],
        "checks": [i["key"] for i in data["items"] if i["status"] == "check"],
        "new_picks": [i["key"] for i in data["items"] if i.get("new_pick")],
        "no_pick": [i["key"] for i in data["items"] if i["status"] == "nopick"],
        "yield_questions": [m["name"] for m in data["meals"] if m["yield_question"]],
    }


def notify_review(data: dict) -> None:
    s = summary(data)
    if not data["meals"] and not data['items']:
        ha.notify("Groceries", "Nothing to buy yet. Add groceries or choose dinners.", tag=REVIEW_TAG,
                  url=f"{CONFIG.public_url}/review")
        return
    trip_label = f"{len(data['meals'])} dinners" if data['meals'] else 'Groceries-only trip'
    parts = [f"{trip_label}, {s['buy']} items, about ${s['total']:.0f}."]
    asks = len(s["checks"]) + len(s["yield_questions"])
    if asks:
        parts.append(f"{asks} quick question{'s' if asks != 1 else ''}.")
    if s["new_picks"]:
        parts.append(f"{len(s['new_picks'])} new product pick{'s' if len(s['new_picks']) != 1 else ''} to check.")
    if s["total"] < CONFIG.order_minimum:
        parts.append(f"Under the ${CONFIG.order_minimum:.0f} delivery minimum.")
    if not data["login_ok"]:
        parts.append("Walmart sign-in expired, open the browser link to sign in.")
    ha.notify(
        "Grocery order ready to review", " ".join(parts), tag=REVIEW_TAG,
        url=f"{CONFIG.public_url}/review",
        actions=[
            {"action": "GROCER_APPROVE", "title": "Approve"},
            {"action": "URI", "title": "Review", "uri": f"{CONFIG.public_url}/review"},
        ],
    )


# ---------------------------------------------------------------- cart

def _substitute(item: dict, avoid_id: str) -> dict | None:
    need = planner.Need(key=item["key"], category=item["category"], amounts=item["amounts"])
    cands = [c for c in browser.search(item["key"]) if c["item_id"] != avoid_id and locally_available(c)]
    if not cands:
        return None
    choice = llm.choose_product(item["key"], need.text(), cands, recipe_lines=item.get("lines"))
    return cands[choice["index"]] if 0 <= choice["index"] < len(cands) else None


def run_cart(force: bool = False) -> dict:
    from .ordering import fill
    return fill(force=force)


def send_to_walmart() -> dict:
    """The one button: approve the list (when it can be) and put it in the Walmart cart now."""
    row = db.latest_review()
    if not row:
        raise RuntimeError('Build a shopping list first')
    if not db.cart_run(row['week_start'], row['id']):
        from .trips import require_current
        require_current(json.loads(row['data']))
    return run_cart(force=True)


@review_edit
def confirm_pick(key: str) -> None:
    """"Looks right" on a product the app found by itself."""
    editable_review()
    db.run("UPDATE picks SET approved = 1 WHERE key = ?", key)
    data, review_id = _pending()
    for i in data["items"]:
        if i["key"] == key:
            i["new_pick"] = False
    db.update_review(review_id, data=data, status="pending")


def product_choices(key: str) -> dict:
    """For the Change sheet: things you've bought before that match, then the other options found."""
    data, _ = _pending()
    item = next((i for i in data["items"] if i["key"] == key), None)
    if not item:
        raise ValueError("Unknown ingredient in this list")
    current = str((item.get("pick") or {}).get("item_id") or "")
    bought = [dict(p) for p in history.candidates(key, db.purchases())][:8]
    return {"key": key, "qty": item.get("qty"), "need": item.get("need_text"),
            "bought_before": [p for p in bought if str(p.get("item_id")) != current],
            "alternatives": [dict(a, index=n) for n, a in enumerate(item.get("alternatives") or [])
                             if str(a.get("item_id")) != current and a.get("local_available")
                             and not str(a.get("local_reason") or "").startswith(("In stock at Walmart", "Couldn't confirm"))],
            "brand_lock": bool(item.get("brand_lock"))}


# ---------------------------------------------------------------- edits from the review page / voice

def resolve_food(text: str) -> str:
    cleaned = clean_food(text)
    canon = planner.canonicalize([planner.ParsedLine(text=text, qty=None, unit=None, food=cleaned)], db, llm)
    return canon.get(cleaned, (cleaned, "other"))[0]


@review_edit
def set_stock_level(food_text: str, level: str) -> str:
    key = resolve_food(food_text) if not db.food(food_text) else food_text
    packages = planner.STOCK_LEVELS[level]
    db.set_stock(key, packages, confidence=0.9)
    db.set_restock(key, level in {"out", "low"})
    _refresh_pending(lambda it: it["key"] == key)
    _add_restock_to_pending(key)
    return key


@review_edit
def _add_restock_to_pending(key: str) -> None:
    """A new restock enters the draft, or waits for the next trip if a cart is active."""
    if busy() and STATE['job']['kind'] == 'cart':
        return
    row = db.latest_review()
    if not row or row["status"] not in {"pending", "approved"} or not db.stock(key)["restock"]:
        return
    data, review_id = _pending()
    existing = next((i for i in data["items"] if i["key"] == key), None)
    if existing:
        if not existing.get("pick"):
            submit(_find_restock_pick, key)
        return
    food = db.food(key)
    need = planner.Need(key=key, category=food["category"] if food else "other",
                        unquantified=True, optional_only=False, sources=["Restock"])
    on_browser_thread = threading.current_thread().name.startswith("grocer")
    lookup_later = False
    if db.pick(key) and locally_available(dict(db.pick(key))):
        pick, alts, is_new = (dict(db.pick(key)) if db.pick(key) else None), [], False
    elif on_browser_thread:
        pick, alts, is_new = _pick_for(key, need)
    else:
        # finding a product needs the browser: add it now without one, and look it up in the background
        pick, alts, is_new = None, [], False
        lookup_later = True
    data["items"].append(_item_entry(need, pick, alts, is_new, set()))
    db.update_review(review_id, data=data)
    if lookup_later:
        submit(_find_restock_pick, key)


def swap_pick(key: str, url_or_id: str) -> dict:
    editable_review()
    configure_browser()
    item_id = item_id_from_url(url_or_id) or url_or_id.strip()
    # just found in a search? use that instead of opening the product page again
    live = browser.cached_listing(item_id) if hasattr(browser, "cached_listing") else None
    if not live:
        with activity("Changing a product", f"Opening that product on Walmart for {key}"):
            live = browser.item(item_id)
    if not live:
        raise ValueError("Couldn't find that Walmart item")
    if not locally_available(live):
        raise ValueError((live or {}).get("local_reason") or "This product is not available locally")
    db.ensure_food(key, None)
    db.save_pick(key, live, approved=True, source="manual")
    _refresh_pending(lambda it: it["key"] == key)
    changed()
    return live


def choose_alternative(key: str, index: int) -> dict:
    editable_review()
    configure_browser()
    data, review_id = _pending()
    item = next(i for i in data["items"] if i["key"] == key)
    if not 0 <= index < len(item["alternatives"]):
        raise ValueError("Choose a valid alternative")
    alt = browser.item(item["alternatives"][index]["item_id"])
    if not locally_available(alt):
        raise ValueError((alt or {}).get("local_reason") or "This alternative is no longer locally available")
    db.save_pick(key, alt, approved=True, source="manual")
    _refresh_pending(lambda it: it["key"] == key)
    changed()
    return alt


def _pending() -> tuple[dict, int]:
    row = db.latest_review()
    if not row:
        raise RuntimeError("No review yet")
    return json.loads(row["data"]), row["id"]


@review_edit
def _refresh_pending(match) -> None:
    if busy() and STATE['job']['kind'] == 'cart':
        return  # current cart inputs are frozen; the stock change applies to the next trip
    row = db.latest_review()
    if not row or row["status"] not in {"pending", "approved"}:
        return
    data, review_id = _pending()
    from .parse import norm_key
    on_hand = {db.resolve(norm_key(name)) for name in mealie.on_hand_foods()}
    data["items"] = [recompute_item(i, on_hand) if match(i) else i for i in data["items"]]
    db.update_review(review_id, data=data)
    if row["status"] == "approved":
        db.update_review(review_id, status="pending")


@review_edit
def skip_item(key: str, skip: bool) -> None:
    editable_review()
    data, review_id = _pending()
    for i in data["items"]:
        if i["key"] == key:
            if skip:
                i["status"], i["qty"] = "skip", 0
            else:
                i["status"] = "pending"
                i.update(recompute_item(i, mealie.on_hand_foods()))
    db.update_review(review_id, data=data, status="pending")


@review_edit
def set_item_qty(key: str, qty: int) -> None:
    editable_review()
    data, review_id = _pending()
    if not any(i["key"] == key for i in data["items"]):
        raise ValueError("Unknown ingredient in this review")
    for i in data["items"]:
        if i["key"] == key:
            if qty > 0 and not locally_available(i.get("pick")):
                raise ValueError("Choose a locally available product first")
            i["qty"] = max(int(qty), 0)
            i["manual_qty"] = True
            i["status"] = "buy" if i["qty"] > 0 else "covered"
    db.update_review(review_id, data=data, status="pending")


@review_edit
def set_food_flags(key: str, **flags) -> None:
    allowed = {k: int(v) if k in {"staple", "brand_lock"} else v for k, v in flags.items()
               if k in {"staple", "brand_lock", "buy_as"}}
    db.set_food(key, **allowed)
    _refresh_pending(lambda it: it["key"] == key)


@review_edit
def approve() -> None:
    editable_review()
    configure_browser()
    data, review_id = _pending()
    from .trips import require_current
    require_current(data)
    for i in data["items"]:
        if i["status"] == "buy" and not locally_available(i.get("pick")):
            raise RuntimeError("Rebuild the list to verify local availability before approving it.")
        if i.get("pick"):
            db.run("UPDATE picks SET approved = 1 WHERE key = ?", i["key"])
            i["new_pick"] = False
    db.update_review(review_id, data=data, status="approved")
    ha.clear(REVIEW_TAG)
    db.log("approved", id=review_id)


@review_edit
def set_servings(date_: str, entry_type: str, recipe_id: str, servings: float) -> dict:
    row = db.latest_review()
    if not row or row['status'] != 'checked_out':
        editable_review()
    db.set_meal_servings(date_, entry_type, recipe_id, float(servings))
    return rebuild_soon()


@review_edit
def set_recipe_yield(recipe_id: str, servings: float) -> dict:
    row = db.latest_review()
    if not row or row['status'] != 'checked_out':
        editable_review()
    db.set_recipe_yield(recipe_id, float(servings))
    return rebuild_soon()


def foods_overview() -> list[dict]:
    rows = db.all(
        """SELECT f.key, f.category, f.staple, f.brand_lock, p.title, p.url, p.price, s.packages, s.last_bought
           FROM foods f LEFT JOIN picks p ON p.key = f.key LEFT JOIN stock s ON s.key = f.key
           WHERE f.category != 'none' ORDER BY f.category, f.key"""
    )
    return [dict(r) for r in rows]


def pantry() -> list[dict]:
    """Every ingredient, what it's linked to and why, and where it's used this week."""
    row = db.latest_review()
    week = {i["key"]: i for i in json.loads(row["data"])["items"]} if row else {}
    out = []
    for f in db.all("SELECT * FROM foods WHERE category != 'none' ORDER BY category, key"):
        key = f["key"]
        p = db.pick(key)
        st = db.stock(key)
        w = week.get(key)
        out.append({
            "key": key, "category": f["category"], "staple": bool(f["staple"]), "brand_lock": bool(f["brand_lock"]),
            "pick": ({"item_id": p["item_id"], "title": p["title"], "url": p["url"], "price": p["price"],
                      "image": f"/img/{p['item_id']}",
                      "source": p["source"] or "search", "approved": bool(p["approved"])} if p else None),
            "stock": st["packages"] if st else None,
            "restock": bool(st and st["restock"]),
            'reserved': __import__('grocer.pantry', fromlist=['reserved_packages']).reserved_packages(key),
            'quantity': st['quantity'] if st else None, 'dimension': st['dimension'] if st else None,
            'confidence': st['confidence'] if st else None,
            'reserved_unknown': __import__('grocer.pantry', fromlist=['reservation_uncertain']).reservation_uncertain(key),
            "this_week": w is not None,
            "status": w["status"] if w else None,
            "sources": w["sources"] if w else [],
            "lines": w["lines"] if w else [],
        })
    return out


def search(query: str) -> list[dict]:
    configure_browser()
    bought = db.purchased_ids()
    with activity("Searching Walmart", f"Looking for {query}"):
        res = [p for p in browser.search(query, limit=10) if locally_available(p)]
    for r in res:
        r["bought_before"] = r["item_id"] in bought
    return res


# ---------------------------------------------------------------- product images

IMAGE_DIR = CONFIG.data_dir / "images"
_image_lookups: set[str] = set()
_image_queue: list[str] = []
IMAGE_LOOKUP_GAP = 180   # seconds between background picture lookups (each is a Walmart page load)
SIZES = {"small": 200, "large": 900}


def image_file(item_id: str, size: str = "small", src: str | None = None) -> Path | None:
    """Local copy of a product's picture, downloading it once. None if there's no picture (yet).
    src: the listing's own picture link, for products not saved yet (search results, other options)."""
    if not item_id.isdigit() or size not in SIZES:
        return None
    path = IMAGE_DIR / (f"{item_id}.jpg" if size == "small" else f"{item_id}_{size}.jpg")
    if path.exists():
        return path
    url = db.image_for(item_id)
    if not url and src:
        from urllib.parse import urlparse
        host = urlparse(src).hostname or ""
        if host == "walmartimages.com" or host.endswith(".walmartimages.com"):
            url = src.split("?")[0]
    if url is None:
        # picked before images were saved: look it up later, one product at a time
        if item_id not in _image_lookups and db.one("SELECT 1 FROM picks WHERE item_id = ?", item_id):
            _image_lookups.add(item_id)
            with _plock:
                _image_queue.append(item_id)
        return None
    if not url:
        return None   # looked up before, Walmart had no picture
    px = SIZES[size]
    try:
        r = httpx.get(url, params={"odnHeight": px, "odnWidth": px, "odnBg": "FFFFFF"}, timeout=20,
                      headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=False)
        for _ in range(3):   # follow redirects only while they stay on walmartimages.com
            nxt = r.headers.get("location") if r.is_redirect else None
            if not nxt:
                break
            nxt = str(r.url.join(nxt))
            host = urlparse(nxt).hostname or ""
            if not (host == "walmartimages.com" or host.endswith(".walmartimages.com")):
                return None
            r = httpx.get(nxt, timeout=20, headers={"User-Agent": "Mozilla/5.0"}, follow_redirects=False)
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image/"):
            return None
        IMAGE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(r.content)
        return path
    except httpx.HTTPError as e:
        log.warning("image download failed for %s: %s", item_id, e)
        return None


def _lookup_image(item_id: str) -> None:
    row = db.one("SELECT key FROM picks WHERE item_id = ? LIMIT 1", item_id)
    with activity("Getting product pictures", f"Looking up {row['key'] if row else 'a product'} on Walmart"):
        live = browser.item(item_id)
    db.set_image(item_id, (live or {}).get("image"))
    changed()


def _image_lookup_loop() -> None:
    """Background: one product-page lookup at a time, spaced out, never during a job."""
    while True:
        time.sleep(IMAGE_LOOKUP_GAP)
        if busy():
            continue
        with _plock:
            item_id = _image_queue.pop(0) if _image_queue else None
        if item_id:
            try:
                submit(_lookup_image, item_id).result(timeout=120)
            except Exception as e:  # noqa: BLE001
                log.warning("image lookup failed for %s: %s", item_id, e)


def start_image_lookups() -> None:
    """Pictures now come from search results and your order history. Opening product pages in the
    background just to fetch a picture looked like a bot to Walmart, so that's no longer done."""
    return


def _find_restock_pick(key: str) -> None:
    food = db.food(key)
    need = planner.Need(key=key, category=food["category"] if food else "other",
                        unquantified=True, optional_only=False, sources=["Restock"])
    with activity("Adding to the next order", f"Finding {key} at Walmart"):
        _pick_for(key, need)
    _refresh_pending(lambda it: it["key"] == key)
    changed()


CHECKOUT_LOOK_EVERY = 3 * 3600     # while an order sits in the cart, glance at the cart this often
CHECKOUT_ASK_AFTER = 20 * 3600     # then ask on the phone if it still hasn't happened


def check_checkout() -> str | None:
    """An empty cart can mean removal, so always ask a person to confirm checkout."""
    row = db.latest_review()
    if not row or row["status"] != "carted":
        return None
    run = db.cart_run(row["week_start"], row["id"])
    if not run:
        return None
    targets = [str(i) for i in (run["data"].get("targets") or {})]
    contents = browser.cart_contents()
    empty = contents is not None and targets and not any(contents.get(i) for i in targets)
    since = (datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(run["updated"])).total_seconds()
    event = f"checkout_asked:{row['id']}"
    asked = db.last_event(event)
    asked_age = (datetime.now(ZoneInfo("UTC")) - datetime.fromisoformat(asked)).total_seconds() if asked else None
    if (empty or since > CHECKOUT_ASK_AFTER) and (asked_age is None or asked_age > CHECKOUT_ASK_AFTER):
        message = ("The groceries are no longer in the cart. Confirm checkout only if you bought them."
                   if empty else "Confirm checkout if you bought the groceries. Pantry estimates stay unchanged until you confirm.")
        ha.notify("Did you check out the groceries?", message, tag=STATUS_TAG,
                  url=f"{CONFIG.public_url}/review",
                  actions=[{"action": "GROCER_DONE", "title": "Yes, checked out"},
                           {"action": "GROCER_NOTYET", "title": "Not yet"}])
        db.log(event)
        return "asked"
    return "waiting"


def _checkout_watch_loop() -> None:
    while True:
        time.sleep(CHECKOUT_LOOK_EVERY)
        row = db.latest_review()
        if busy() or not row or row["status"] != "carted":
            continue
        try:
            submit(check_checkout).result(timeout=600)
        except Exception as e:  # noqa: BLE001
            log.warning("checkout check failed: %s", e)


def start_checkout_watch() -> None:
    threading.Thread(target=_checkout_watch_loop, name="checkout-watch", daemon=True).start()


@review_edit
def checked_out(quantities=None, manual_keys=None) -> None:
    if busy():
        raise RuntimeError("Wait for the cart job to finish before confirming checkout.")
    from .ordering import confirm_checkout
    confirm_checkout(quantities, manual_keys)


def clear_cart(scope: str = "ours") -> dict:
    """Take things back out of the Walmart cart. scope "ours": only what this app put there for the
    current order; "all": everything in the cart. Never touches checkout."""
    row = db.latest_review()
    ours: set[str] = set()
    run = db.cart_run(row["week_start"], row["id"]) if row else None
    if run:
        ours = {str(i) for i in (run["data"].get("targets") or {})} | \
               {str(l["pick"]["item_id"]) for l in run["data"].get("lines") or [] if l.get("pick")}
    if scope == "ours" and not ours:
        raise ValueError("The app hasn't put anything in the Walmart cart for this order")
    progress(step="Opening your Walmart cart")
    protected = []
    if scope == 'ours':
        current = browser.cart_contents()
        if current is None:
            raise RuntimeError('The cart could not be read. Nothing was removed. Check it in Walmart.')
        from .trips import owned_removals
        safe, protected = owned_removals(run['data'], current)
        result = browser.remove_from_cart(safe) if safe else {'removed': [], 'left': current}
    else:
        result = browser.remove_from_cart(None)
    removed = result["removed"]
    left = result["left"]
    # the order is no longer in the cart: let the list be edited and sent again
    if row and run and row["status"] != "checked_out" and not protected and left is not None and not (ours & set(left)):
        data = json.loads(row["data"])
        data.pop("cart_result", None)
        with db.tx() as c:
            c.execute("DELETE FROM cart_runs WHERE week_start=?", (run["week_start"],))
            db.update_review(row['id'], data=data, status='pending')
        ha.clear(STATUS_TAG)
    elif row and run and row['status'] != 'checked_out':
        # Removing some owned lines must invalidate the old "cart ready" result.
        # Keep the durable plan so a retry can add deficits without duplicating shared lines.
        from .cart import deficits
        plan = run['data']
        if left is None:
            plan['error'] = 'The cart changed but could not be checked. Open Walmart before retrying.'
            plan['cart_before'] = None
            state, status = 'needs_check', 'failed'
        else:
            plan['missing'] = [iid for iid, _ in deficits(plan['targets'], left)]
            for iid in removed:
                if not left.get(iid):
                    plan.setdefault('added_quantities', {})[iid] = 0
            plan['added'] = sum(str(line['pick']['item_id']) not in plan['missing'] for line in plan['lines'])
            state = status = 'partial' if plan['missing'] or plan.get('unverified') else 'carted'
        data = json.loads(row['data'])
        data['cart_result'] = plan
        db.save_cart_run(row['week_start'], row['id'], state, plan)
        db.update_review(row['id'], data=data, status=status)
    db.log("cart_cleared", scope=scope, removed=removed)
    changed()
    return {"removed": len(removed), "left": None if left is None else sum(left.values()), "scope": scope,
            'protected': protected,
            'message': 'Shared or changed items were left in Walmart for you to adjust.' if protected else 'Cart checked.'}


@review_edit
def reset_cart_plan() -> None:
    if busy():
        raise RuntimeError("Wait for the current job to finish before resetting the plan.")
    row = db.latest_review()
    if not row:
        raise RuntimeError("No review yet")
    if row["status"] == "checked_out":
        raise RuntimeError("A completed order cannot be reset. Build the next shopping week instead.")
    data = json.loads(row["data"])
    data.pop("cart_result", None)
    run = db.cart_run(row['week_start'], row['id'])
    with db.tx() as c:
        if run:
            db.log('cart_plan_archived', trip=row['trip_id'], plan=run['data'])
            c.execute("DELETE FROM cart_runs WHERE week_start=?", (run["week_start"],))
        db.update_review(row['id'], data=data, status='pending')
    ha.clear(STATUS_TAG)
    db.log("cart_plan_reset", week=row["week_start"])
    changed()


# ---------------------------------------------------------------- buttons and voice, from Home Assistant

def ha_handlers() -> dict:
    return {
        "GROCER_APPROVE": lambda: (approve(), changed()),
        "GROCER_RETRY": lambda: start("cart", "Filling the Walmart cart", run_cart, force=True),
        "GROCER_DONE": checked_out,
        "GROCER_NOTYET": lambda: ha.clear(STATUS_TAG),
        "stock": lambda food, level: (set_stock_level(food, level if level in planner.STOCK_LEVELS else "out"),
                                      changed()),
    }


# ---------------------------------------------------------------- the check page

def check(text: str) -> dict:
    """Explain how one ingredient gets its product: name, current link, history matches and verdicts."""
    cleaned = clean_food(text) or text.strip().lower()
    known = db.food(text.strip().lower())
    key = known["key"] if known else resolve_food(text)
    pick = db.pick(key)
    need = _need_from_pending(key)
    lines = need.lines if need else []
    purchases = db.purchases()
    cands = history.candidates(key, purchases)
    verdicts = []
    if cands:
        try:
            verdicts = llm.judge_history(key, lines, cands)
        except Exception as e:  # noqa: BLE001
            verdicts = [{"index": i, "match": None, "reason": f"model unavailable: {e}"} for i in range(len(cands))]
    rejected = {r["item_id"] for r in db.all("SELECT item_id FROM rejected WHERE key = ?", key)} \
        if db.one("SELECT name FROM sqlite_master WHERE name = 'rejected'") else set()
    last_sync = db.last_event("history_synced")
    result = {
        "typed": text, "cleaned": cleaned, "key": key,
        "category": (db.food(key) or {"category": None})["category"],
        "lines": lines,
        "pick": dict(pick) if pick else None,
        "history_count": len(purchases), "history_synced": last_sync,
        "candidates": [dict(c, image=f"/img/{c['item_id']}" if c.get("image_url") else None,
                            match=v.get("match"), reason=v.get("reason"), rejected=c["item_id"] in rejected)
                       for c, v in zip(cands, verdicts or [{}] * len(cands))],
        "near": [dict(n, image=f"/img/{n['item_id']}" if n.get("image_url") else None)
                 for n in history.near_misses(key, purchases, {c["item_id"] for c in cands})],
    }
    configure_browser()
    with activity("Checking local stock", "Verifying past purchases at your Walmart store"):
        for product in result["candidates"] + result["near"]:
            live = browser.item(product["item_id"])
            product["local_available"] = locally_available(live)
            product["local_reason"] = (live or {}).get("local_reason") or "Local stock could not be verified"
    return result


def use_purchase(key: str, item_id: str) -> dict:
    """Validate current local inventory before linking a past purchase."""
    editable_review()
    configure_browser()
    row = db.one("SELECT * FROM purchases WHERE item_id = ?", item_id)
    if not row:
        raise ValueError("That product isn't in your saved Walmart history")
    p = browser.item(item_id)
    if not locally_available(p):
        raise ValueError((p or {}).get("local_reason") or "That past purchase is not available locally")
    p["image"] = p.get("image") or p.get("image_url")
    db.ensure_food(key, None)
    db.save_pick(key, p, approved=True, source="manual")
    _refresh_pending(lambda it: it["key"] == key)
    changed()
    return p


def _recipe_links(recipes: dict) -> dict:
    """Recipe name -> link to it in Mealie, for the "Used in" lines."""
    out = {}
    for r in recipes.values():
        try:
            out[r.name] = mealie.recipe_url(CONFIG.mealie_public_url, r.slug)
        except Exception:  # noqa: BLE001
            continue
    return out


def recipe_links() -> dict:
    row = db.latest_review()
    return json.loads(row["data"]).get("recipes", {}) if row else {}


# ---------------------------------------------------------------- duplicate ingredients

DEDUPE_EVERY_DAYS = 7


def _confirmed(groups: list[dict], step: str = "Double-checking") -> list[dict]:
    """The model proposes groups loosely; ask it again about every pair on its own and keep only the yeses.
    groups: [{"keep": name, "same": [names]}] -> same shape, plus "reasons": {name: why}."""
    pairs = [(g["keep"], s) for g in groups for s in g["same"]]
    verdict: dict[tuple[str, str], dict] = {}
    for i in range(0, len(pairs), 15):
        chunk = pairs[i:i + 15]
        progress(done=i, total=len(pairs), step=f"{step}: {chunk[0][1]} vs {chunk[0][0]}")
        try:
            for v, pair in zip(llm.confirm_same(chunk), chunk):
                verdict[pair] = v
        except Exception as e:  # noqa: BLE001
            log.warning("double-check failed: %s", e)
    out = []
    for g in groups:
        ok = [s for s in g["same"] if verdict.get((g["keep"], s), {}).get("same") is True]
        if ok:
            out.append({"keep": g["keep"], "same": ok,
                        "reasons": {s: verdict[(g["keep"], s)].get("reason", "") for s in ok}})
        for s in g["same"]:
            if s not in ok:
                log.info("not merging %s into %s: %s", s, g["keep"], verdict.get((g["keep"], s), {}).get("reason", "no answer"))
    return out


def dedupe_foods() -> dict:
    """Propose local ingredient merges. A person reviews before anything changes."""
    from .cleanup import scan
    return scan()


def ingredient_edit_allowed():
    if busy():
        raise RuntimeError('Wait for the current task to finish before changing ingredient records.')
    row = db.latest_review()
    if row and row['status'] != 'checked_out' and db.cart_run(row['week_start'],row['id']):
        raise RuntimeError('Finish or reopen the active cart before changing ingredient records.')


def merge_by_hand(old: str, new: str) -> None:
    ingredient_edit_allowed()
    if not db.food(old) or not db.food(new):
        raise ValueError("Unknown ingredient")
    from .cleanup import apply_manual
    apply_manual(old,new)
    db.log("merged_by_hand", old=old, new=new)


# ---------------------------------------------------------------- Mealie food cleanup (review first)

def _head(name: str) -> str:
    from .parse import norm_key
    words = [singular(w) for w in norm_key(name).split()]
    # "dried parsley leaves" belongs with "dried parsley", "salmon filet" with "salmon"
    while len(words) > 1 and words[-1] in planner.FORM_WORDS:
        words.pop()
    return words[-1] if words else ""


def mealie_cleanup_scan() -> dict:
    """Propose merges for Mealie's food list. Nothing changes in Mealie until you apply."""
    from .parse import norm_key
    progress(step="Reading Mealie's ingredient list")
    foods = mealie.all_foods()
    by_name = {f["name"]: f for f in foods}
    groups: list[dict] = []
    used: set[str] = set()

    # 1. same thing spelled differently (all-purpose / all purpose, Half & Half / half and half)
    spell: dict[str, list[dict]] = {}
    for f in foods:
        words = norm_key(f["name"]).split()
        if words:
            words[-1] = singular(words[-1])
        spell.setdefault(" ".join(words), []).append(f)
    for same in spell.values():
        if len(same) > 1:
            keep = min(same, key=lambda f: (len(f["name"]), f["name"]))
            groups.append({"keep": keep, "same": [f for f in same if f is not keep], "why": "Same name, spelled differently"})
            used |= {f["id"] for f in same}

    # 2. the model looks at names sharing a main word (all the peppers, all the parsleys)
    buckets: dict[str, list[str]] = {}
    for f in foods:
        if f["id"] not in used:
            buckets.setdefault(_head(f["name"]), []).append(f["name"])
    batch: list[str] = []
    proposed: list[dict] = []
    todo = [names for names in buckets.values() if len(names) > 1]
    for n, names in enumerate(todo, 1):
        batch += names
        if len(batch) < 30 and n < len(todo):
            continue
        progress(done=n, total=len(todo), step=f"Checking {', '.join(names[:3])}")
        try:
            proposed += llm.find_duplicates(batch)
        except Exception as e:  # noqa: BLE001
            log.warning("mealie cleanup check failed: %s", e)
        batch = []
    for g in _confirmed(proposed):
        keep = by_name.get(g["keep"])
        same = [by_name[x] for x in g["same"] if x in by_name and by_name[x]["id"] not in used]
        if keep and same and keep["id"] not in used:
            why = "; ".join(r for r in (g["reasons"].get(f["name"]) for f in same) if r) or "Same ingredient"
            groups.append({"keep": keep, "same": same, "why": why})
            used |= {keep["id"]} | {f["id"] for f in same}
    proposal = {"scanned": len(foods), "groups": groups, "created": now_iso()}
    db.log("mealie_cleanup_proposal", **proposal)
    return proposal


def mealie_cleanup_proposal() -> dict | None:
    row = db.one("SELECT id, data FROM events WHERE kind = 'mealie_cleanup_proposal' ORDER BY id DESC LIMIT 1")
    if not row:
        return None
    prop = json.loads(row["data"])
    gone: set[str] = set()   # merged away since this scan
    for a in db.all("SELECT data FROM events WHERE kind = 'mealie_cleanup_applied' AND id > ?", row["id"]):
        gone |= set(json.loads(a["data"]).get("merged_ids", []))
    groups = []
    for g in prop["groups"]:
        same = [f for f in g["same"] if f["id"] not in gone]
        if same and g["keep"]["id"] not in gone:
            groups.append({**g, "same": same})
    return {**prop, "groups": groups}


def mealie_cleanup_apply(choices: list[dict]) -> dict:
    """choices: [{"keep": food_id, "same": [food_id, ...]}], exactly what was ticked on the page."""
    row = db.latest_review()
    if row and row['status'] != 'checked_out' and db.cart_run(row['week_start'],row['id']):
        raise RuntimeError('Finish or reopen the active cart before cleaning up ingredients.')
    progress(step="Making a Mealie backup first")
    backed_up = mealie.backup()
    if not backed_up:
        raise RuntimeError("Mealie backup failed. No ingredients were merged. Check the token's admin permissions and try again.")
    names = {f["id"]: f["name"] for f in mealie.all_foods()}
    done, failed, merged_ids = [], [], []
    for n, c in enumerate(choices, 1):
        keep = c["keep"]
        if keep not in names:
            failed.append({"name": c.get("keep_name", keep), "why": "no longer in Mealie"})
            continue
        merged_names = []
        for fid in c.get("same", []):
            if fid not in names or fid == keep:
                continue
            progress(done=n - 1, total=len(choices), step=f"Merging {names[fid]} into {names[keep]}")
            try:
                mealie.merge_foods(fid, keep)
                merged_names.append(names[fid])
                merged_ids.append(fid)
            except httpx.HTTPStatusError as e:
                why = ("it's on a Mealie shopping list; remove it there and try again"
                       if e.response.status_code >= 500 else f"Mealie said {e.response.status_code}")
                failed.append({"name": names[fid], "why": why})
        if merged_names:
            try:
                mealie.add_aliases(keep, merged_names)
            except httpx.HTTPError as e:
                log.warning("aliases for %s failed: %s", names[keep], e)
            done.append({"keep": names[keep], "merged": merged_names})
            # this app's own ingredient list follows along
            from .parse import norm_key
            for old in merged_names:
                ok, nk = norm_key(old), norm_key(names[keep])
                if db.food(ok) and ok != nk:
                    db.ensure_food(nk, db.food(ok)["category"])
                    merge_into(ok, nk)
    db.log("mealie_cleanup_applied", done=done, failed=failed, backed_up=backed_up, merged_ids=merged_ids)
    changed()
    return {"done": done, "failed": failed, "backed_up": backed_up}


def add_grocery(text: str) -> str:
    if not text.strip() or len(text) > 120:
        raise ValueError('Enter a grocery item up to 120 characters')
    key = resolve_food(text.strip().lower())
    if not db.stock(key):
        db.set_stock(key, 0, 0.5)
    db.set_restock(key, True)
    _add_restock_to_pending(key)
    changed()
    return key

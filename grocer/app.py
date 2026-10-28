"""HTTP API + review page. Runs the weekly schedule too."""
from __future__ import annotations

import json
import logging
from datetime import date
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from urllib.parse import urlparse

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, conint

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

from . import dinners, jobs, planner  # noqa: E402  (after logging, so startup repairs are logged)
from .config import CONFIG  # noqa: E402
log = logging.getLogger("grocer")

STATIC = Path(__file__).parent / "static"
scheduler = BackgroundScheduler(timezone=CONFIG.tz)

BROWSER_TIMEOUT = 900  # seconds; a full review build searches Walmart for new items


def _on_worker(fn, *args, timeout: int = BROWSER_TIMEOUT, **kwargs):
    """Quick things that need the browser. Refused while a long job has it, instead of hanging."""
    if jobs.busy():
        raise HTTPException(409, "Busy updating the list. Try again when the progress bar finishes.")
    try:
        return jobs.submit(fn, *args, **kwargs).result(timeout=timeout)
    except TimeoutError as e:
        # it keeps going in the background and the page refreshes when it's done
        raise HTTPException(504, "Walmart is slow right now. It'll finish on its own and the page will update.") from e
    except (ValueError, KeyError, IndexError, StopIteration) as e:
        raise HTTPException(400, str(e) or "bad request") from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e


def _direct(fn, *args, **kwargs):
    """Quick edits that don't need the browser: run right away, even during a long job."""
    try:
        res = fn(*args, **kwargs)
    except (ValueError, KeyError, IndexError, StopIteration) as e:
        raise HTTPException(400, str(e) or "bad request") from e
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    jobs.changed()
    return res


def _job_response(job: dict, wait: bool):
    if wait and job.get("id"):
        jobs.wait_for(job)
        return {"job": jobs.status()["last"], "review": get_review()}
    return {"job": job}


@asynccontextmanager
async def lifespan(_app):
    scheduler.add_job(lambda: jobs.start("build", "Building next week's list", jobs.build_review), CronTrigger.from_crontab(CONFIG.review_cron, timezone=CONFIG.tz),
                      id="review", replace_existing=True)
    scheduler.add_job(lambda: jobs.start("cart", "Filling the Walmart cart", jobs.run_cart), CronTrigger.from_crontab(CONFIG.cart_cron, timezone=CONFIG.tz),
                      id="cart", replace_existing=True)
    scheduler.add_job(lambda: jobs.start("plan", "Planning dinners", dinners.fill_empty),
                      CronTrigger.from_crontab(CONFIG.plan_cron, timezone=CONFIG.tz), id="plan", replace_existing=True)
    scheduler.start()
    log.info("schedule: review %s, cart %s (%s)", CONFIG.review_cron, CONFIG.cart_cron, CONFIG.tz)
    jobs.start_image_lookups()
    jobs.start_checkout_watch()
    if CONFIG.ha_token:
        from .ha_events import listen_forever
        listen_forever(CONFIG.ha_url, CONFIG.ha_token, jobs.ha_handlers())
    yield
    scheduler.shutdown(wait=False)
    jobs.submit(jobs.browser.close)


app = FastAPI(title="Grocer", docs_url="/docs", lifespan=lifespan)
@app.middleware("http")
async def same_origin_only(request: Request, call_next):
    """Refuse changes sent from another web page. Browsers always say where a cross-site POST came from;
    requests with no Origin (scripts, Home Assistant, curl) are not browser pages and pass."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        if origin is not None and urlparse(origin).netloc != request.headers.get("host", ""):
            return JSONResponse({"detail": "Cross-site request refused."}, status_code=403)
    return await call_next(request)


app.mount("/static", StaticFiles(directory=STATIC), name="static")


# ---------------------------------------------------------------- pages

@app.get("/")
def root():
    from . import readiness
    if not readiness.status()['configured']:
        return RedirectResponse('/settings#setup')
    row = jobs.db.latest_review()
    return RedirectResponse('/review' if row else '/dinners')


@app.get("/dinners")
def dinners_page():
    return FileResponse(STATIC / "dinners.html")


@app.get("/meal-img/{recipe_id}")
def meal_image(recipe_id: str):
    path = dinners.image_file(recipe_id)
    if not path:
        raise HTTPException(404, "no image")
    return FileResponse(path, media_type="image/webp", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/review")
def review_page():
    return FileResponse(STATIC / "review.html")


@app.get("/pantry")
def pantry_page():
    return FileResponse(STATIC / "pantry.html")


@app.get("/img/{item_id}")
def product_image(item_id: str, size: str = "small", src: str | None = None):
    path = jobs.image_file(item_id, size, src)
    if not path:
        raise HTTPException(404, "no image")
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=604800"})


@app.get("/settings")
def settings_page():
    return FileResponse(STATIC / "settings.html")


@app.get("/check")
def check_page():
    return FileResponse(STATIC / "check.html")


@app.get("/mealie-cleanup")
def mealie_cleanup_page():
    return FileResponse(STATIC / "mealie-cleanup.html")


@app.post("/signin")
def signin():
    """Open Walmart's sign-in page in the service's browser, then jump to the VNC view of it."""
    if not jobs.busy():
        jobs.submit(_open_signin)
    return RedirectResponse(CONFIG.vnc_url, status_code=303)


def _open_signin() -> None:
    jobs.browser.open()
    jobs.browser._page.goto("https://www.walmart.com/account/login", wait_until="domcontentloaded")


@app.get("/health")
def health():
    nxt = {j.id: str(j.next_run_time) for j in scheduler.get_jobs()}
    return {"ok": True, "next": nxt}


# ---------------------------------------------------------------- review data

@app.get("/api/review")
def get_review():
    row = jobs.db.latest_review()
    if not row:
        return {"status": "none"}
    data = json.loads(row["data"])
    from .costs import meal_costs
    data["meal_costs"] = meal_costs(data)
    next_cart = scheduler.get_job("cart")
    return {**data, "id": row["id"], 'trip_id': row['trip_id'], "status": row["status"], "summary": jobs.summary(data),
            "levels": list(planner.STOCK_LEVELS), "vnc_url": CONFIG.vnc_url,
            "order_minimum": CONFIG.order_minimum, "auto_cart": CONFIG.auto_cart_if_unreviewed,
            "next_cart": str(next_cart.next_run_time) if next_cart else None,
            "walmart": jobs.walmart_settings()}


@app.get("/api/settings")
def settings():
    try:
        picked = dinners.selected_keys()
    except Exception:  # noqa: BLE001  Mealie down: the page still loads
        picked = None
    return {"walmart": jobs.walmart_settings(), "vnc_url": CONFIG.vnc_url, "selected_meals": picked,
            "pace": getattr(jobs.browser, "pace", dict)()}


class WalmartSettingsIn(BaseModel):
    store_id: str = Field(pattern=r"^[0-9]{1,6}$")
    fulfillment: str = Field(pattern=r"^(PICKUP|DELIVERY)$")


@app.post("/api/settings/walmart")
def walmart_settings(body: WalmartSettingsIn):
    _direct(jobs.set_walmart_settings, body.store_id, body.fulfillment)
    return {"ok": True}


@app.get("/api/status")
def status():
    return {**jobs.status(), "vnc_url": CONFIG.vnc_url}


@app.post("/api/review/build")
def build(notify: bool = False, wait: bool = False):
    return _job_response(jobs.start("build", "Updating the list", jobs.build_review, notify=notify), wait)


class CheckoutIn(BaseModel):
    quantities: dict[str, conint(strict=True, ge=0, le=999)] | None = None
    manual_keys: list[str] = Field(default_factory=list)


@app.post("/api/checked-out")
def checked_out(body: CheckoutIn | None = None):
    _direct(jobs.checked_out, body.quantities if body else None, body.manual_keys if body else [])
    return {"ok": True}


@app.post("/api/cart/reset")
def cart_reset():
    _direct(jobs.reset_cart_plan)
    return {"ok": True}


@app.post("/api/approve")
def approve():
    _direct(jobs.approve)
    return {"ok": True}


@app.post("/api/cart")
def cart(force: bool = False, wait: bool = False):
    job = jobs.start("cart", "Filling the Walmart cart", jobs.run_cart, force=force)
    if wait:
        jobs.wait_for(job)
        return (jobs.status()["last"] or {}).get("result") or {}
    return {"job": job}


# ---------------------------------------------------------------- edits

class StockIn(BaseModel):
    food: str
    level: str


@app.post("/api/stock")
def stock(body: StockIn):
    level = body.level.strip().lower()
    if level not in planner.STOCK_LEVELS:
        raise HTTPException(400, f"level must be one of {list(planner.STOCK_LEVELS)}")
    key = _direct(jobs.set_stock_level, body.food.strip().lower(), level)
    return {"ok": True, "key": key, "level": level}


class SwapIn(BaseModel):
    key: str
    url: str


@app.post("/api/swap")
def swap(body: SwapIn):
    item = _on_worker(jobs.swap_pick, body.key, body.url, timeout=120)
    return {"ok": True, "pick": item}


class AltIn(BaseModel):
    key: str
    index: int = Field(ge=0)


@app.post("/api/alternative")
def alternative(body: AltIn):
    return {"ok": True, "pick": _on_worker(jobs.choose_alternative, body.key, body.index)}


class SkipIn(BaseModel):
    key: str
    skip: bool = True


@app.post("/api/skip")
def skip(body: SkipIn):
    _direct(jobs.skip_item, body.key, body.skip)
    return {"ok": True}


class QtyIn(BaseModel):
    key: str
    qty: int = Field(ge=0, le=999, strict=True)


@app.post("/api/qty")
def qty(body: QtyIn):
    _direct(jobs.set_item_qty, body.key, body.qty)
    return {"ok": True}


class FoodIn(BaseModel):
    key: str
    staple: bool | None = None
    brand_lock: bool | None = None


@app.post("/api/food")
def food(body: FoodIn):
    flags = {k: v for k, v in body.model_dump().items() if k != "key" and v is not None}
    _direct(jobs.set_food_flags, body.key, **flags)
    return {"ok": True}


class MergeIn(BaseModel):
    old: str
    new: str


@app.post("/api/merge")
def merge(body: MergeIn):
    _direct(jobs.merge_by_hand, body.old, body.new)
    return {"ok": True}


@app.post("/api/dedupe")
def dedupe(wait: bool = False):
    job = jobs.start("dedupe", "Looking for duplicate ingredients", jobs.dedupe_foods)
    if wait:
        jobs.wait_for(job)
        return jobs.status()["last"]
    return {"job": job}


class ServingsIn(BaseModel):
    date: str
    entry_type: str = "dinner"
    recipe_id: str
    servings: float = Field(gt=0, le=1000, allow_inf_nan=False)


@app.post("/api/servings")
def servings(body: ServingsIn, wait: bool = False):
    job = _direct(jobs.set_servings, body.date, body.entry_type, body.recipe_id, body.servings)
    return _job_response(job, wait)


class YieldIn(BaseModel):
    recipe_id: str
    servings: float = Field(gt=0, le=1000, allow_inf_nan=False)


@app.post("/api/yield")
def recipe_yield(body: YieldIn, wait: bool = False):
    job = _direct(jobs.set_recipe_yield, body.recipe_id, body.servings)
    return _job_response(job, wait)


@app.post("/api/history/sync")
def history_sync(wait: bool = False):
    """Read Walmart purchase history and switch unapproved picks to products you've bought before."""
    job = jobs.start("history", "Checking your past Walmart orders", jobs.sync_history)
    if wait:
        jobs.wait_for(job)
        last = jobs.status()["last"] or {}
        if last.get("state") == "failed":
            raise HTTPException(502, last.get("error") or "failed")
        return {**(last.get("result") or {}), "review": get_review()}
    return {"job": job}


@app.get("/api/pantry")
def pantry():
    return {"items": jobs.pantry(), "levels": list(planner.STOCK_LEVELS), "recipes": jobs.recipe_links()}


@app.get("/api/search")
def search(q: str):
    q = q.strip()
    if not q:
        raise HTTPException(400, "empty search")
    return {"results": _on_worker(jobs.search, q, timeout=180)}


class KeyIn(BaseModel):
    key: str


@app.post("/api/food/delete")
def food_delete(body: KeyIn):
    _direct(jobs.delete_food, body.key)
    return {"ok": True}


@app.get("/api/check")
def check(q: str):
    if not q.strip():
        raise HTTPException(400, "type an ingredient")
    return _on_worker(jobs.check, q)


class UsePurchaseIn(BaseModel):
    key: str
    item_id: str


@app.post("/api/use-purchase")
def use_purchase(body: UsePurchaseIn):
    return {"ok": True, "pick": _on_worker(jobs.use_purchase, body.key, body.item_id)}


@app.post("/api/mealie/cleanup/scan")
def mealie_cleanup_scan(wait: bool = False):
    job = jobs.start("mealie-scan", "Looking for duplicate ingredients in Mealie", jobs.mealie_cleanup_scan)
    if wait:
        jobs.wait_for(job)
    return {"job": job}


@app.get("/api/mealie/cleanup")
def mealie_cleanup_get():
    last_apply = jobs.db.one("SELECT data FROM events WHERE kind = 'mealie_cleanup_applied' ORDER BY id DESC LIMIT 1")
    return {"proposal": jobs.mealie_cleanup_proposal(),
            "last_applied": json.loads(last_apply["data"]) if last_apply else None}


@app.delete("/api/mealie/cleanup")
def mealie_cleanup_clear():
    """Forget the suggestions and the last cleanup summary. Nothing in Mealie changes."""
    jobs.db.run("DELETE FROM events WHERE kind IN ('mealie_cleanup_proposal', 'mealie_cleanup_applied')")
    jobs.changed()
    return {"ok": True}


class CleanupIn(BaseModel):
    groups: list[dict]


@app.post("/api/mealie/cleanup/apply")
def mealie_cleanup_apply(body: CleanupIn, wait: bool = False):
    if not body.groups:
        raise HTTPException(400, "Nothing selected")
    job = jobs.start("mealie-apply", "Cleaning up ingredients in Mealie", jobs.mealie_cleanup_apply, body.groups)
    if wait:
        jobs.wait_for(job)
        return jobs.status()["last"]
    return {"job": job}


@app.get("/api/foods")
def foods():
    return jobs.foods_overview()


# ---------------------------------------------------------------- dinners

def _mealie(fn, *args, **kwargs):
    """Dinner edits talk to Mealie; say so plainly when it can't be reached."""
    try:
        return _direct(fn, *args, **kwargs)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Mealie didn't answer: {e}") from e


@app.get("/api/dinners")
def dinners_list():
    try:
        return dinners.overview()
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Mealie didn't answer: {e}") from e


class ChooseIn(BaseModel):
    keys: list[str]
    selected: bool


@app.post("/api/dinners/choose")
def dinners_choose(body: ChooseIn):
    _direct(dinners.choose, body.keys, body.selected)
    return {"ok": True}


class KeyOnlyIn(BaseModel):
    key: str


@app.post("/api/dinners/swap")
def dinners_swap(body: KeyOnlyIn):
    return {"ok": True, **_mealie(dinners.swap, body.key)}


class SetDinnerIn(BaseModel):
    day: date
    recipe_id: str
    key: str | None = None


@app.post("/api/dinners/set")
def dinners_set(body: SetDinnerIn):
    return {"ok": True, **_mealie(dinners.set_dinner, body.day.isoformat(), body.recipe_id, body.key)}


class DayIn(BaseModel):
    day: date


@app.post("/api/dinners/fill_day")
def dinners_fill_day(body: DayIn):
    return {"ok": True, **_mealie(dinners.fill_day, body.day.isoformat())}


@app.post("/api/dinners/fill")
def dinners_fill():
    return {"ok": True, **_mealie(dinners.fill_empty)}


@app.get("/api/recipes/choices")
def recipe_choices(q: str = ""):
    try:
        return dinners.recipe_choices(q.strip())
    except httpx.HTTPError as e:
        raise HTTPException(502, f"Mealie didn't answer: {e}") from e


class RotationIn(BaseModel):
    slug: str
    on: bool


@app.post("/api/recipes/rotation")
def recipe_rotation(body: RotationIn):
    _mealie(dinners.set_rotation, body.slug, body.on)
    return {"ok": True}


# ---------------------------------------------------------------- the simple flow

@app.post("/api/send")
def send(wait: bool = False):
    """Approve and put it in the Walmart cart, in one go."""
    job = jobs.start("cart", "Sending to Walmart", jobs.send_to_walmart)
    if wait:
        jobs.wait_for(job)
        return (jobs.status()["last"] or {}).get("result") or {}
    return {"job": job}


@app.post("/api/confirm-pick")
def confirm_pick(body: KeyIn):
    _direct(jobs.confirm_pick, body.key)
    return {"ok": True}


@app.get("/api/choices")
def choices(key: str):
    return _direct(jobs.product_choices, key)


class ClearIn(BaseModel):
    scope: str = Field("ours", pattern=r"^(ours|all)$")


@app.post("/api/cart/clear")
def cart_clear(body: ClearIn, wait: bool = False):
    """Remove what the app added (scope ours) or everything (scope all) from the Walmart cart."""
    job = jobs.start("clear", "Emptying the Walmart cart" if body.scope == "all" else "Taking the order out of the Walmart cart",
                     jobs.clear_cart, body.scope)
    if wait:
        jobs.wait_for(job)
        return jobs.status()["last"]
    return {"job": job}


@app.exception_handler(RuntimeError)
async def task_conflict(_request, error):
    return JSONResponse(status_code=409, content={'detail': str(error)})


@app.get('/api/setup')
def setup_status():
    from .readiness import status
    return status()


@app.post('/api/setup/check')
def setup_check():
    from .readiness import check
    return check()


class GroceryIn(BaseModel):
    text: str = Field(min_length=1,max_length=120)


@app.post('/api/groceries')
def add_grocery(body: GroceryIn):
    return {'key':_direct(jobs.add_grocery,body.text)}


@app.get('/api/meals/pending')
def pending_meals():
    from .pantry import pending_meals
    return {'meals': pending_meals()}


class MealResolutionIn(BaseModel):
    key: str
    action: str = Field(pattern='^(cooked|skipped)$')


@app.post('/api/meals/resolve')
def resolve_meal(body: MealResolutionIn):
    from .pantry import resolve_meal
    _direct(resolve_meal,body.key,body.action)
    return {'ok':True}


@app.get('/api/jobs/recovery')
def recovery_jobs():
    from .task_history import recovery
    return {'tasks':recovery(jobs.db)}


@app.post('/api/jobs/{job_id}/resume')
def resume_task(job_id: str):
    from .task_history import resume
    return {'job':_direct(resume,job_id)}


@app.post('/api/jobs/{job_id}/dismiss')
def dismiss_task(job_id: str):
    jobs.db.run("UPDATE job_records SET state='dismissed' WHERE id=? AND state IN ('interrupted','queued','failed')",job_id)
    jobs.changed()
    return {'ok':True}


@app.get('/api/cleanup')
def cleanup_proposal():
    from .cleanup import latest
    return {'proposal':latest()}


class MergeProposalIn(BaseModel):
    id: str
    groups: list[int]


@app.post('/api/cleanup/apply')
def apply_cleanup(body: MergeProposalIn):
    from .cleanup import apply
    _direct(apply,body.id,body.groups)
    return {'ok':True}


@app.post('/api/cleanup/{proposal_id}/undo')
def undo_cleanup(proposal_id: str):
    from .cleanup import undo
    _direct(undo,proposal_id)
    return {'ok':True}


class DecisionIn(BaseModel):
    key: str
    decision: str = Field(pattern='^(manual|later)$')


@app.post('/api/trip/decision')
def trip_decision(body: DecisionIn):
    with jobs.REVIEW_LOCK, jobs.db.tx():
        if jobs.busy():
            raise HTTPException(409, 'Wait for the current task to finish before updating this trip.')
        row = jobs.db.latest_review()
        run = jobs.db.cart_run(row['week_start'], row['id']) if row else None
        if not run or run['state'] == 'checked_out':
            raise HTTPException(409, 'No active cart to update')
        plan = run['data']
        if body.key not in plan.get('unresolved', []):
            raise HTTPException(400, 'That ingredient does not need a decision')
        plan.setdefault('decisions', {})[body.key] = body.decision
        jobs.db.save_cart_run(row['week_start'], row['id'], run['state'], plan)
        data = json.loads(row['data'])
        data['cart_result'] = plan
        jobs.db.update_review(row['id'], data=data)
    jobs.changed()
    return {'ok': True}


@app.middleware('http')
async def refresh_interface_assets(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith('/static/') or response.headers.get('content-type','').startswith('text/html'):
        response.headers['Cache-Control'] = 'no-cache'
    return response

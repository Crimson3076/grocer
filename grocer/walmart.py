"""Drives a real, signed-in Chrome profile to search Walmart and fill the cart.

Never touches checkout. All calls must happen on the single browser worker
thread (see jobs.WORKER), because Playwright's sync API is thread bound.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
from pathlib import Path
from urllib.parse import quote_plus

from . import units
from .availability import cart_quantities, describe_keys, local_stock, selected_store
from .availability import fulfillment as get_fulfillment
from .db import now_iso

log = logging.getLogger(__name__)

BASE = "https://www.walmart.com"
CART_LINK = "https://affil.walmart.com/cart/addToCart?items="


class BrowserChallenge(RuntimeError):
    """Walmart showed a bot check. A human has to solve it in the VNC window."""


class LoginRequired(RuntimeError):
    """The Walmart session expired. A human has to sign in through the VNC window."""


def _price(v) -> float | None:
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def _walk(node, found: dict) -> None:
    if isinstance(node, dict):
        if "usItemId" in node and "name" in node:
            have = found.get(node["usItemId"])
            if have is None or len(node) > len(have):
                found[node["usItemId"]] = node
        for v in node.values():
            _walk(v, found)
    elif isinstance(node, list):
        for v in node:
            _walk(v, found)


# sections of a page that are suggestions, not things you bought
_NOT_PURCHASED = re.compile(r"recommend|carousel|sponsor|similar|related|p13n|\bads?\b|adsv|upsell|cross.?sell|"
                            r"frequently|also.?(bought|viewed)|you.?may|trending|deals|rollback|banner", re.I)


def _walk_paths(node, found: dict, origins: dict, path: tuple = ()) -> None:
    """Like _walk, but skips suggestion sections and remembers where each product sat in the data."""
    if isinstance(node, dict):
        kind = node.get("__typename") or node.get("moduleType") or node.get("type")
        if isinstance(kind, str) and _NOT_PURCHASED.search(kind):
            return                                   # a whole suggestion module
        if "usItemId" in node and "name" in node and node.get("usItemId") not in found:
            found[node["usItemId"]] = node
            origins[node["usItemId"]] = "/".join(path[-4:])
        for k, v in node.items():
            if isinstance(k, str) and _NOT_PURCHASED.search(k):
                continue
            if isinstance(v, (dict, list)):
                _walk_paths(v, found, origins, path + (k,))
    elif isinstance(node, list):
        for v in node:
            _walk_paths(v, found, origins, path + ("[]",))


def listing_from_node(n: dict, store_id: str = "", fulfillment: str = "PICKUP", session_store: str | None = None) -> dict:
    pi = n.get("priceInfo") or {}
    cur = pi.get("currentPrice") or {}
    price = _price(cur.get("price")) or _price(pi.get("linePrice")) or _price(n.get("price"))
    unit_price = (pi.get("unitPrice") or {}).get("priceString") if isinstance(pi.get("unitPrice"), dict) else pi.get("unitPrice")
    avail = (n.get("availabilityStatusV2") or {}).get("value") or n.get("availabilityStatus") or ""
    url = n.get("canonicalUrl") or f"/ip/{n['usItemId']}"
    title = n.get("name", "")
    size = units.package_size(title)
    store_ok, store_reason = local_stock(n, store_id, fulfillment, session_store)
    in_stock = str(avail).upper() in {"IN_STOCK", "AVAILABLE"} or n.get("availabilityStatusDisplayValue") == "In stock"
    ff = get_fulfillment(n, fulfillment)
    # Both the chosen store/method and the delivery time must be verified.
    local = store_ok and ff["ok"] and (in_stock or not avail)
    reason = store_reason if not store_ok else ff["label"]
    confirmed = local
    return {
        "item_id": str(n["usItemId"]),
        "title": title,
        "price": price,
        "unit_price": unit_price,
        "in_stock": in_stock,
        "local_confirmed": confirmed,
        "url": url if url.startswith("http") else BASE + url,
        "sales_unit": n.get("salesUnitType") or n.get("salesUnit") or "EACH",
        "sponsored": bool(n.get("isSponsoredFlag")),
        "brand": _brand(n),
        "size_qty": size.qty if size else None,
        "size_dim": size.dim if size else None,
        "image": _image(n),
        "local_available": local,
        "availability_checked_at": now_iso(),
        "local_reason": reason,
        "when": ff["when"] if local else None,
        "method": ff["method"] if local else None,
        "seller": ff["seller"],
        "store_id": store_id,
        "fulfillment": fulfillment,
    }


def _image(n: dict) -> str | None:
    """Product thumbnail link; Walmart puts it in a few different places."""
    info = n.get("imageInfo") or {}
    for v in (info.get("thumbnailUrl"), (info.get("allImages") or [{}])[0].get("url") if info.get("allImages") else None,
              n.get("thumbnailUrl"), n.get("image"), n.get("imageUrl"),
              (n.get("productImage") or {}).get("url") if isinstance(n.get("productImage"), dict) else None):
        if isinstance(v, dict):
            v = v.get("url") or v.get("thumbnailUrl")
        if isinstance(v, str) and v.startswith("http"):
            return v.split("?")[0]
    # Walmart moves the picture around between page types: take the first product image link anywhere in the data
    m = _IMG_RE.search(json.dumps(n))
    return m.group(0) if m else None


_IMG_RE = re.compile(r"https://i5\.walmartimages\.com/(?:asr|seo)/[^\"'?\s]+?\.(?:jpe?g|png|webp)", re.I)


def _brand(n: dict) -> str | None:
    b = n.get("brand")
    if isinstance(b, dict):
        b = b.get("name")
    if isinstance(b, list):
        b = b[0] if b else None
    if isinstance(b, str) and b.strip():
        return b.strip()
    title = n.get("name") or ""
    # "Great Value ...", "McCormick ...": brand is usually the leading words before the product
    m = re.match(r"^((?:Great Value|Freshness Guaranteed|Marketside|Sam's Choice|bettergoods)|[A-Z][\w'&.-]+)", title)
    return m.group(1) if m else None


def item_id_from_url(url: str) -> str | None:
    m = re.search(r"/ip/(?:[^/]+/)?(\d{5,})", url) or re.search(r"(\d{6,})", url)
    return m.group(1) if m else None


class WalmartBrowser:
    def __init__(self, profile_dir: Path, channel: str = "chrome", delay: float = 4.0):
        self.profile_dir = profile_dir
        self.channel = channel
        self.delay = delay
        self._pw = None
        self._ctx = None
        self._page = None
        self._warmed = False
        self.challenge_wait = 15 * 60          # seconds to wait for a human to clear a robot check
        self.on_challenge = None               # callbacks set by jobs (HA notification)
        self.on_challenge_cleared = None
        self.on_progress = None
        self.on_login_ok = None
        self.store_id = ""
        self.fulfillment = "PICKUP"
        # pacing: Walmart's bot check reacts to how fast and how much we browse.
        # min_gap is learned: a robot check slows it down, a long clean run speeds it up again.
        self.min_gap = 12.0            # seconds between page loads (randomized up to ~1.8x)
        self.gap_floor, self.gap_ceiling = 5.0, 60.0
        self.slow_factor, self.fast_factor = 1.5, 0.85
        self.clean_run = 15            # page loads without a check before trying a little faster
        self.idle_after = 20 * 60      # after this long without a page, start again from the home page
        self._last_load = 0.0
        self._since_warm = 0           # page loads since the last home-page warm-up
        self._clean = 0
        self.checks_seen = 0
        self.on_pace = None            # callback(dict) to remember the learned pace
        self.burst = 8                 # pages in a row before a longer break
        self.cooldown = 30 * 60        # right after a robot check, go even slower for this long
        self._loads: list[float] = []
        self._cool_until = 0.0
        self.on_wait = None            # callback(seconds, why) so the progress bar can say we're pacing
        self.sleep = time.sleep        # tests swap this out

    # ---- lifecycle ----
    def open(self) -> None:
        if self._page is not None:
            return
        try:
            from patchright.sync_api import sync_playwright  # undetected Playwright fork
        except ImportError:  # pragma: no cover
            from playwright.sync_api import sync_playwright
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self._pw = sync_playwright().start()
        self._ctx = self._pw.chromium.launch_persistent_context(
            user_data_dir=str(self.profile_dir),
            channel=self.channel or None,
            headless=False,          # runs on the container's virtual display, viewable over VNC
            no_viewport=True,
            locale="en-US",
            timezone_id="America/New_York",
        )
        self._page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()

    def close(self) -> None:
        try:
            if self._ctx:
                self._ctx.close()
            if self._pw:
                self._pw.stop()
        finally:
            self._pw = self._ctx = self._page = None
            self._warmed = False

    def _pause(self, factor: float = 1.0) -> None:
        self.sleep(self.delay * factor * random.uniform(0.7, 1.4))

    def _throttle(self) -> None:
        """Wait before the next page load so the pace looks like a person reading, not a crawler."""
        now = time.time()
        self._loads = [t for t in self._loads if now - t < 3600]
        slow = 2.0 if now < self._cool_until else 1.0
        waits = []
        if self._loads:
            waits.append((self._loads[-1] + self.min_gap * slow * random.uniform(0.8, 1.8) - now, "between pages"))
        recent = [t for t in self._loads if now - t < 600]
        if len(recent) >= self.burst and len(recent) % self.burst == 0:
            waits.append((random.uniform(5, 10) * self.min_gap * slow, "taking a short break"))
        per_hour = self.per_hour()
        if len(self._loads) >= per_hour:
            waits.append((self._loads[-per_hour] + 3600 - now, "hourly limit reached"))
        wait, why = max(waits, default=(0, ""))
        if wait > 1:
            if self.on_wait:
                try:
                    self.on_wait(wait, why)
                except Exception:  # noqa: BLE001
                    pass
            self.sleep(wait)
        self._loads.append(time.time())

    def per_hour(self) -> int:
        """The hourly cap follows the learned gap (about 40 an hour at the default 12 seconds)."""
        return int(min(120, max(12, 3600 / (self.min_gap * 1.3) / 1.8)))

    def pace(self) -> dict:
        return {"min_gap": round(self.min_gap, 1), "per_hour": self.per_hour(), "checks_seen": self.checks_seen,
                "clean": self._clean}

    def _learn(self, hit_check: bool) -> None:
        """Slow down a lot after a robot check; speed up a little after a long clean run."""
        before = self.min_gap
        if hit_check:
            self.min_gap = min(self.gap_ceiling, self.min_gap * self.slow_factor)
            self.checks_seen += 1
            self._clean = 0
        else:
            self._clean += 1
            if self._clean < self.clean_run:
                return
            self._clean = 0
            self.min_gap = max(self.gap_floor, self.min_gap * self.fast_factor)
        if self.min_gap != before:
            log.info("browsing pace: %.1fs -> %.1fs between pages (%s)", before, self.min_gap,
                     "robot check" if hit_check else f"{self.clean_run} pages without a check")
        if self.on_pace:
            try:
                self.on_pace(self.pace())
            except Exception:  # noqa: BLE001
                pass

    def _browse_like_a_person(self) -> None:
        """Scroll a little and move the mouse, the way someone glancing at the page would."""
        try:
            for _ in range(random.randint(1, 3)):
                self._page.mouse.move(random.randint(200, 1100), random.randint(150, 700), steps=random.randint(5, 15))
                self._page.mouse.wheel(0, random.randint(250, 700))
                self.sleep(random.uniform(0.4, 1.2))
        except Exception:  # noqa: BLE001  never let this break a page load
            pass

    def _ready(self) -> None:
        """Pace the next page, and after a while away start from the home page like a person would."""
        self.open()
        self._throttle()
        idle = time.time() - self._last_load > self.idle_after
        if not self._warmed or idle or "walmart.com" not in (self._page.url or ""):
            self._page.goto(BASE, wait_until="domcontentloaded", timeout=60_000)
            self._since_warm = 0
            self.sleep(random.uniform(3, 7))
            self._browse_like_a_person()
            self._wait_out_challenge()
            self._warmed = True
            self._throttle()

    def _arrived(self) -> None:
        self._last_load = time.time()
        self._since_warm += 1
        self._pause(0.6)
        self._browse_like_a_person()
        if not self._is_challenge():
            self._learn(hit_check=False)

    def _goto(self, url: str, ready: bool = True) -> None:
        if ready:
            self._ready()
        self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
        self._arrived()
        if self._wait_out_challenge():
            # solved: load what we actually wanted
            self._page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            self._pause(0.6)
            if self._is_challenge():
                raise BrowserChallenge(self._page.url)

    def _is_challenge(self) -> bool:
        url = self._page.url
        try:
            title = (self._page.title() or "").lower()
        except Exception:  # noqa: BLE001  page mid-navigation
            title = ""
        return "/blocked" in url or "robot or human" in title or "verify your identity" in title

    def _wait_out_challenge(self) -> bool:
        """If Walmart shows its robot check, ask a human to solve it over VNC and wait.
        Returns True if there was a challenge (now solved). Raises if nobody solves it in time."""
        if not self._is_challenge():
            return False
        log.warning("Walmart robot check at %s, waiting for someone to solve it", self._page.url)
        if self._since_warm > 2:
            self._cool_until = time.time() + self.cooldown     # go slower for a while afterwards
            self._learn(hit_check=True)
        else:
            log.info("robot check right after starting fresh: not blaming the browsing speed")
        if self.on_challenge:
            try:
                self.on_challenge()
            except Exception as e:  # noqa: BLE001
                log.warning("challenge notify failed: %s", e)
        deadline = time.time() + self.challenge_wait
        while time.time() < deadline:
            self.sleep(5)
            if not self._is_challenge():
                log.info("robot check cleared")
                if self.on_challenge_cleared:
                    try:
                        self.on_challenge_cleared()
                    except Exception:  # noqa: BLE001
                        pass
                self._pause(1.0)
                return True
        raise BrowserChallenge(self._page.url)

    def _next_data(self) -> dict:
        try:
            raw = self._page.locator("script#__NEXT_DATA__").first.text_content(timeout=15_000)
            return json.loads(raw or "{}")
        except Exception:  # noqa: BLE001
            return {}

    # ---- session ----
    def ensure_logged_in(self) -> None:
        self._goto(f"{BASE}/account")
        if "/account/login" in self._page.url or "login" in self._page.url.split("?")[0].split("/")[-1]:
            raise LoginRequired(self._page.url)
        if self.on_login_ok:
            self.on_login_ok()

    def ensure_store(self) -> None:
        self._goto(BASE)
        current = selected_store(self._next_data(), self.fulfillment)
        if not current or not self.store_id:
            log.info("Walmart store not confirmed (browser shows %s, settings say %s); carrying on", current, self.store_id or "none")
            return
        if current != self.store_id:
            raise RuntimeError("The Walmart browser's local store could not be verified. Select the same store and fulfillment method in Walmart before filling the cart.")

    # ---- catalog ----
    SEARCH_TTL = 12 * 3600   # the same search within 12 hours reuses the last results

    def search(self, query: str, limit: int = 12) -> list[dict]:
        cache = self.__dict__.setdefault("_search_cache", {})
        ck = (query.strip().lower(), self.store_id, self.fulfillment)
        hit = cache.get(ck)
        if hit and time.time() - hit[0] < self.SEARCH_TTL:
            log.info("search %r: reusing results from %d min ago", query, (time.time() - hit[0]) // 60)
            return [dict(p) for p in hit[1][:limit]]
        results = self._search(query, max(limit, 12))
        cache[ck] = (time.time(), results)
        return [dict(p) for p in results[:limit]]

    _SEARCH_BOX = ("input[type='search'], input[name='q'], #global-search-input, "
                   "input[aria-label*='search' i]")

    def cached_listing(self, item_id: str) -> dict | None:
        """A product seen in a search in the last 12 hours, without opening its page again."""
        for at, results in (self.__dict__.get("_search_cache") or {}).values():
            if time.time() - at < self.SEARCH_TTL:
                for p in results:
                    if str(p.get("item_id")) == str(item_id):
                        return dict(p)
        return None

    def _typed_search(self, query: str) -> tuple[dict, dict] | None:
        """Search the way a person does: click the search box, type, press Enter.
        Returns (page data, products found) or None if it couldn't be done that way."""
        box = self._page.locator(self._SEARCH_BOX).first
        if not box.count():
            return None
        captured: list = []

        def handler(resp) -> None:
            try:
                ctype = resp.headers.get("content-type") or ""
                if "walmart.com" in resp.url and "json" in ctype and re.search(r"search|graphql", resp.url, re.I):
                    captured.append(resp)
            except Exception:  # noqa: BLE001
                pass

        self._page.on("response", handler)
        try:
            box.click(timeout=5_000)
            box.fill("")
            box.type(query, delay=random.randint(70, 160))
            self.sleep(random.uniform(0.4, 1.0))
            box.press("Enter")
            self._page.wait_for_url(re.compile(r"/search"), timeout=20_000)
            self._page.wait_for_load_state("domcontentloaded", timeout=20_000)
            self.sleep(random.uniform(1.0, 2.0))
        finally:
            self._page.remove_listener("response", handler)
        self._arrived()
        if self._is_challenge():
            self._wait_out_challenge()
            return None                     # solved: get the results the plain way
        found: dict = {}
        for r in captured:
            try:
                _walk(r.json(), found)
            except Exception:  # noqa: BLE001
                pass
        data = self._next_data()
        page_q = str((data.get("query") or {}).get("q") or "")
        if page_q.strip().lower() == query.strip().lower():   # the page itself is for this search
            stacks = (((data.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get("searchResult")
            _walk(stacks if stacks else data, found)
        if not found:
            return None
        log.info("search %r: typed into Walmart's search box", query)
        return data, found

    def _search(self, query: str, limit: int = 12) -> list[dict]:
        self._ready()
        typed = None
        try:
            typed = self._typed_search(query)
        except BrowserChallenge:
            raise
        except Exception as e:  # noqa: BLE001  any trouble with the search box: use the address instead
            log.info("typed search didn't work (%s); using the search address", e)
        if typed:
            data, found = typed
        else:
            self._goto(f"{BASE}/search?q={quote_plus(query)}", ready=False)
            data = self._next_data()
            found = {}
            stacks = (((data.get("props") or {}).get("pageProps") or {}).get("initialData") or {}).get("searchResult")
            _walk(stacks if stacks else data, found)
        session_store = selected_store(data, self.fulfillment)
        nodes = [n for n in found.values() if n.get("name")]
        everything = [listing_from_node(n, self.store_id, self.fulfillment, session_store) for n in nodes]
        listings = [p for p in everything if p["local_available"]]
        why = {}
        for p in everything:
            if not p["local_available"]:
                why[p["local_reason"]] = why.get(p["local_reason"], 0) + 1
        log.info("search %r: %d of %d can be had today/tomorrow; left out: %s", query, len(listings), len(everything), why)
        for label in ("No pickup", "Unavailable", "Shipping only"):
            sample = next((n for n, p in zip(nodes, everything) if p["local_reason"].startswith(label)), None)
            if sample is not None:
                log.info("search %r sample %r: %s", query, label, json.dumps(sample, default=str)[:3000])
        speed = {"today": 0, "tomorrow": 1}
        method = {"pickup": 0, "delivery": 1, "shipping": 2}
        listings.sort(key=lambda x: (x["sponsored"], speed.get(x["when"], 2), method.get(x["method"], 3)))
        return listings[:limit]

    def item(self, item_id: str) -> dict | None:
        """Fetch one product page. None means the listing is gone."""
        self._goto(f"{BASE}/ip/{item_id}")
        if "/ip/" not in self._page.url and "/search" in self._page.url:
            return None
        data = self._next_data()
        found: dict = {}
        _walk(data, found)
        node = found.get(item_id) or next((n for n in found.values() if str(n.get("usItemId")) == str(item_id)), None)
        if not node:
            return None
        listing = listing_from_node(node, self.store_id, self.fulfillment, selected_store(data, self.fulfillment))
        status = str(node.get("availabilityStatus") or "").upper()
        if status:
            listing["in_stock"] = status in {"IN_STOCK", "AVAILABLE"}
        if not listing["local_available"]:
            log.info("item %s not available today/tomorrow (%s): %s", item_id, listing["local_reason"],
                     json.dumps(describe_keys(node))[:1500])
        return listing

    # ---- purchase history ----
    _PAGER = (
        # only real pagination controls; carousels also have "Next" arrows
        "nav[aria-label*='pagination' i] [aria-label*='next' i]",
        "[data-testid*='NextPage' i], [data-testid*='next-page' i], [data-automation-id*='next-page' i]",
        "a[aria-label='Next Page' i], button[aria-label='Next Page' i]",
        "nav[aria-label*='pagination' i] a:has-text('Next'), nav[aria-label*='pagination' i] button:has-text('Next')",
    )

    def _click_next(self) -> bool:
        page = self._page
        for sel in self._PAGER:
            loc = page.locator(sel)
            try:
                for i in range(min(loc.count(), 3)):
                    el = loc.nth(i)
                    if not el.is_visible() or el.get_attribute("aria-disabled") == "true" or not el.is_enabled():
                        continue
                    el.scroll_into_view_if_needed(timeout=5000)
                    el.click(timeout=5000)
                    page.wait_for_load_state("domcontentloaded", timeout=30_000)
                    self._pause(1.2)
                    self._wait_out_challenge()
                    return True
            except Exception:  # noqa: BLE001
                continue
        return False

    def _goto_page_param(self, base_url: str, n: int) -> None:
        sep = "&" if "?" in base_url else "?"
        self._goto(f"{base_url}{sep}page={n}")

    def purchase_history(self, scrolls: int = 8, max_pages: int = 12) -> list[dict]:
        """Products from "My Items" (reorder list) and orders, every page. Reads the page data plus any
        JSON the pages load, skipping suggestion sections."""
        self.open()
        responses = []

        def handler(resp) -> None:   # Playwright needs a real function here, not a bound builtin
            responses.append(resp)

        found: dict = {}
        origins: dict = {}
        counts: dict = {}
        self._page.on("response", handler)
        try:
            for url in (f"{BASE}/my-items", f"{BASE}/orders"):
                self._goto(url)
                if "login" in self._page.url:
                    raise LoginRequired(self._page.url)
                before = len(found)
                current, mode = 1, "click"
                label = "past orders" if url.endswith("orders") else "your usual items"
                while True:
                    if self.on_progress:
                        self.on_progress(f"Reading {label}, page {current}")
                    page_before = len(found)
                    for _ in range(scrolls):
                        self._page.mouse.wheel(0, 5000)
                        self._pause(0.4)
                        more = self._page.get_by_role("button", name=re.compile(r"(load|show|view) more", re.I))
                        try:
                            if more.count() and more.first.is_visible():
                                more.first.click(timeout=5000)
                                self._pause(0.6)
                        except Exception:  # noqa: BLE001
                            pass
                    _walk_paths(self._next_data(), found, origins, ("page",))
                    for r in responses:
                        try:
                            if "json" in (r.headers.get("content-type") or ""):
                                _walk_paths(r.json(), found, origins, ("xhr",))
                        except Exception:  # noqa: BLE001
                            continue
                    responses.clear()
                    if current > 1 and len(found) == page_before:
                        if mode == "param":
                            break                      # nothing new either way: last page
                        mode = "param"                 # the click didn't load a new page; use ?page=N
                        self._goto_page_param(url, current)
                        continue
                    if current >= max_pages:
                        break
                    current += 1
                    if mode == "click" and self._click_next():
                        continue
                    mode = "param"
                    self._goto_page_param(url, current)
                pages = current
                counts[url.rsplit("/", 1)[-1]] = {"products": len(found) - before, "pages": pages}
        finally:
            self._page.remove_listener("response", handler)
        log.info("purchase history: %s", counts)
        # where products were found, so a wrong section is easy to spot in the logs
        by_origin: dict = {}
        for iid, origin in origins.items():
            by_origin.setdefault(origin, []).append(found[iid].get("name", "")[:40])
        for origin, names in sorted(by_origin.items(), key=lambda kv: -len(kv[1]))[:12]:
            log.info("  %3d from %s  e.g. %s", len(names), origin, " | ".join(names[:3]))
        return [listing_from_node(n) for n in found.values() if n.get("name")]

    # ---- cart ----
    def add_to_cart(self, lines: list[tuple[str, int]]) -> None:
        """Add items in one shot via Walmart's multi-item cart link. Adds to whatever is already there."""
        if not lines:
            return
        for i in range(0, len(lines), 20):  # keep URLs a sane length
            chunk = ",".join(f"{item_id}|{qty}" for item_id, qty in lines[i:i + 20])
            self._goto(CART_LINK + chunk)
            self._pause(1.5)

    # marks the Remove button of the first cart line we want gone; returns that line's product id
    _MARK_REMOVE = r"""(wanted) => {
      document.querySelectorAll('[data-grocer-remove]').forEach(b => b.removeAttribute('data-grocer-remove'));
      const buttons = [...document.querySelectorAll('button')].filter(b => {
        const t = ((b.getAttribute('aria-label') || '') + ' ' + (b.innerText || '')).trim().toLowerCase();
        return /^remove\b/.test(t) || /\bremove\b/.test(b.getAttribute('aria-label') || '') && !/save for later/.test(t);
      });
      for (const b of buttons) {
        let row = b, id = null;
        for (let i = 0; i < 12 && row && !id; i++) {
          row = row.parentElement;
          const a = row && row.querySelector('a[href*="/ip/"]');
          const m = a && a.getAttribute('href').match(/\/(\d{5,})(?:[?#/]|$)/);
          if (m) id = m[1];
        }
        // only buttons that belong to a real product line in the cart
        if (id && (!wanted || wanted.includes(id))) { b.setAttribute('data-grocer-remove', '1'); return id; }
      }
      return null;
    }"""

    def remove_from_cart(self, item_ids: set[str] | None = None) -> dict:
        """Take products out of the Walmart cart by tapping each line's Remove button.
        item_ids None means everything. Never touches checkout."""
        self._goto(f"{BASE}/cart")
        wanted = sorted(str(i) for i in item_ids) if item_ids is not None else None
        removed: list[str] = []
        tries: dict[str, int] = {}
        for _ in range(100):
            iid = self._page.evaluate(self._MARK_REMOVE, wanted)
            if not iid:
                break
            tries[iid] = tries.get(iid, 0) + 1
            if tries[iid] > 2:          # tapping Remove isn't taking it out: stop rather than loop
                log.warning("Walmart cart: %s won't come out; stopping", iid)
                break
            btn = self._page.locator("[data-grocer-remove='1']").first
            btn.scroll_into_view_if_needed(timeout=5_000)
            self.sleep(random.uniform(0.6, 1.4))
            btn.click(timeout=10_000)
            if iid not in removed:
                removed.append(iid)
            if wanted is not None and iid in wanted:
                wanted.remove(iid)
            if self.on_progress:
                try:
                    self.on_progress(f"Removed {len(removed)} from the Walmart cart")
                except Exception:  # noqa: BLE001
                    pass
            self.sleep(random.uniform(2.0, 3.5))      # let the cart update before the next one
            if wanted == []:
                break
        left = self.cart_contents()
        return {"removed": removed, "left": left}

    def cart_item_ids(self) -> set[str]:
        """Only actual cart lines count. Unknown pages are not an empty cart."""
        contents = self.cart_contents()
        if contents is None:
            raise RuntimeError("Walmart cart quantities could not be verified. Check the browser before retrying.")
        return set(contents)

    def cart_contents(self) -> dict[str, int] | None:
        self._goto(f"{BASE}/cart")
        self._pause(1.0)
        return cart_quantities(self._next_data())

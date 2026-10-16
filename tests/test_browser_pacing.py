"""Browser interactions remain sequential and respect navigation pacing."""
from pathlib import Path
from types import SimpleNamespace

import pytest

from grocer.walmart import BrowserChallenge, WalmartBrowser


def browser():
    return WalmartBrowser(Path("/tmp/unused-grocer-profile"))


def test_click_scrolls_and_pauses_before_interacting():
    w = browser()
    events = []
    w._wait_out_challenge = lambda: events.append("challenge")
    w._pause = lambda factor: events.append("pause")
    target = SimpleNamespace(scroll_into_view_if_needed=lambda **kw: events.append("scroll"),
                             click=lambda **kw: events.append("click"))
    w._click(target)
    assert events == ["challenge", "scroll", "pause", "click", "pause"]


def test_history_scroll_preserves_distance_in_small_steps():
    w = browser()
    steps, waits = [], []
    w._page = SimpleNamespace(mouse=SimpleNamespace(wheel=lambda x, y: steps.append(y)))
    w.sleep = waits.append
    w._scroll_history()
    assert sum(steps) == 5000
    assert all(0 < step <= 650 for step in steps)
    assert len(waits) == len(steps)
    assert all(0.3 <= wait <= 0.7 for wait in waits)


def test_pagination_throttles_and_records_navigation():
    w = browser()
    events = []
    target = SimpleNamespace(is_visible=lambda: True, get_attribute=lambda name: None,
                             is_enabled=lambda: True)
    w._page = SimpleNamespace(locator=lambda sel: SimpleNamespace(count=lambda: 1, nth=lambda i: target),
                             wait_for_load_state=lambda *a, **kw: events.append("loaded"))
    w._throttle = lambda: events.append("throttle")
    w._click = lambda el: events.append("click")
    w._arrived = lambda: events.append("arrived")
    w._wait_out_challenge = lambda: events.append("challenge")
    assert w._click_next()
    assert events == ["throttle", "click", "loaded", "arrived", "challenge"]


def test_post_challenge_reload_is_throttled():
    w = browser()
    events = []
    w._page = SimpleNamespace(goto=lambda *a, **kw: events.append("goto"))
    w._ready = lambda: events.append("ready")
    w._arrived = lambda: events.append("arrived")
    w._wait_out_challenge = lambda: True
    w._throttle = lambda: events.append("throttle")
    w._is_challenge = lambda: False
    w._goto("https://www.walmart.com/ip/7")
    assert events == ["ready", "goto", "arrived", "throttle", "goto", "arrived"]


def test_blocked_arrival_does_not_scroll_or_learn():
    w = browser()
    w._pause = lambda factor: None
    w._is_challenge = lambda: True
    w._browse_like_a_person = lambda: pytest.fail("Scrolled a challenge page")
    w._learn = lambda **kw: pytest.fail("Counted blocked page as a clean visit")
    w._arrived()


def test_click_stops_when_challenge_is_unresolved():
    w = browser()
    def blocked():
        raise BrowserChallenge("blocked")
    w._wait_out_challenge = blocked
    target = SimpleNamespace(scroll_into_view_if_needed=lambda **kw: pytest.fail("Scrolled while blocked"))
    with pytest.raises(BrowserChallenge):
        w._click(target)

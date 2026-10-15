"""Listen to Home Assistant for notification button taps and voice commands.

No HA config files needed: the service subscribes to HA's event stream with the same token it uses
for notifications. Handles:
  mobile_app_notification_action  GROCER_APPROVE / GROCER_RETRY / GROCER_DONE (from any phone)
  grocer_stock                    {"food": "...", "level": "out|low|medium|high"} fired by the voice automation
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

log = logging.getLogger(__name__)

EVENTS = ("mobile_app_notification_action", "grocer_stock")


def dispatch(event_type: str, data: dict, handlers: dict[str, Callable]) -> bool:
    """Route one HA event to its handler. Returns True if it was ours."""
    if event_type == "mobile_app_notification_action":
        fn = handlers.get(str(data.get("action", "")))
        if fn:
            fn()
            return True
        return False
    if event_type == "grocer_stock":
        food = str(data.get("food") or "").strip()
        level = str(data.get("level") or "out").strip().lower()
        if food and "stock" in handlers:
            handlers["stock"](food, level)
            return True
    return False


def _ws_url(ha_url: str) -> str:
    return ha_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/api/websocket"


def listen_forever(ha_url: str, token: str, handlers: dict[str, Callable]) -> threading.Thread:
    def run() -> None:
        from websockets.sync.client import connect
        delay = 5
        while True:
            try:
                with connect(_ws_url(ha_url), open_timeout=15, close_timeout=5) as ws:
                    json.loads(ws.recv())                                   # auth_required
                    ws.send(json.dumps({"type": "auth", "access_token": token}))
                    if json.loads(ws.recv()).get("type") != "auth_ok":
                        log.error("Home Assistant rejected the token; button taps won't work")
                        time.sleep(300)
                        continue
                    for i, ev in enumerate(EVENTS, 1):
                        ws.send(json.dumps({"id": i, "type": "subscribe_events", "event_type": ev}))
                    log.info("listening to Home Assistant for grocery buttons and voice commands")
                    delay = 5
                    for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("type") != "event":
                            continue
                        ev = msg["event"]
                        try:
                            if dispatch(ev.get("event_type", ""), ev.get("data") or {}, handlers):
                                log.info("handled %s %s", ev.get("event_type"), ev.get("data"))
                        except Exception:  # noqa: BLE001
                            log.exception("handling %s failed", ev.get("event_type"))
            except Exception as e:  # noqa: BLE001  HA restarting, network blip: reconnect
                log.warning("Home Assistant connection lost (%s), retrying in %ss", e, delay)
                time.sleep(delay)
                delay = min(delay * 2, 120)

    t = threading.Thread(target=run, name="ha-events", daemon=True)
    t.start()
    return t

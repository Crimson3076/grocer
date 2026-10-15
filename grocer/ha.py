"""Home Assistant notifications."""
from __future__ import annotations

import logging

import httpx

log = logging.getLogger(__name__)


class HA:
    def __init__(self, url: str, token: str, targets: list[str]):
        self.http = httpx.Client(base_url=url, headers={"Authorization": f"Bearer {token}"}, timeout=20.0)
        self.targets = targets

    def notify(self, title: str, message: str, *, tag: str, url: str | None = None,
               actions: list[dict] | None = None, targets: list[str] | None = None) -> None:
        data: dict = {"tag": tag, "group": "grocer"}
        if url:
            data["url"] = url            # iOS tap action
            data["clickAction"] = url    # Android tap action
        if actions:
            data["actions"] = actions
        for t in targets or self.targets:
            try:
                r = self.http.post(f"/api/services/notify/{t}", json={"title": title, "message": message, "data": data})
                r.raise_for_status()
            except httpx.HTTPError as e:
                log.warning("notify %s failed: %s", t, e)

    def clear(self, tag: str) -> None:
        for t in self.targets:
            try:
                self.http.post(f"/api/services/notify/{t}", json={"message": "clear_notification", "data": {"tag": tag}})
            except httpx.HTTPError as e:
                log.warning("clear %s failed: %s", t, e)

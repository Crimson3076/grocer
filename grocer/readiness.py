"""Explicit service checks, separate from Docker's lightweight liveness probe."""

import os

import httpx

from .db import now_iso

VERSION = "0.2.3"


def status():
    from . import jobs as j

    saved = j.db.settings("connection_checks", {}) or {}
    return {
        "version": VERSION,
        "revision": os.environ.get("GROCER_REVISION", "development")[:12],
        "checks": saved,
        "store": j.walmart_settings(),
        "walmart": j.db.settings(
            "walmart_session", {"signed_in": None, "checked": None}
        ),
        "configured": bool(j.CONFIG.mealie_token and j.walmart_settings()["store_id"]),
        "optional_notifications": bool(j.CONFIG.ha_token),
    }


def check():
    from . import jobs as j

    results = {}
    targets = [
        ("mealie", j.CONFIG.mealie_url + "/api/users/self", j.CONFIG.mealie_token),
        ("ollama", j.CONFIG.ollama_url + "/api/tags", None),
    ]
    if j.CONFIG.ha_token:
        targets.append(("notifications", j.CONFIG.ha_url + "/api/", j.CONFIG.ha_token))
    for name, url, token in targets:
        try:
            if name == "mealie" and not token:
                raise ValueError("Add your Mealie token to grocer.env")
            headers = {"Authorization": "Bearer " + token} if token else {}
            r = httpx.get(url, headers=headers, timeout=5, follow_redirects=False)
            r.raise_for_status()
            if name == "ollama":
                models = [m.get("name", "") for m in r.json().get("models", [])]
                if j.CONFIG.ollama_model not in models:
                    raise ValueError("Download the configured Ollama model first")
            results[name] = {"ok": True, "message": "Connected"}
        except ValueError as e:
            results[name] = {"ok": False, "message": str(e)}
        except httpx.HTTPError:
            results[name] = {
                "ok": False,
                "message": "Could not connect. Check the address, token, and service.",
            }
    results["checked"] = now_iso()
    j.db.save_settings("connection_checks", results)
    return status()

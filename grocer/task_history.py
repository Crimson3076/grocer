"""Durable task history. Restart recovery never automatically mutates a cart."""

import json
import uuid

from .db import now_iso

RESUMABLE = {
    "build_review",
    "run_cart",
    "send_to_walmart",
    "clear_cart",
    "sync_history",
    "mealie_cleanup_scan",
    "dedupe_foods",
    "fill_empty",
}


def record(db, job, fn, args, kwargs):
    name = getattr(fn, "__name__", "")
    row = db.latest_review()
    try:
        payload = json.dumps(
            {
                "function": name,
                "args": args,
                "kwargs": kwargs,
                "trip_id": row["trip_id"] if row else None,
            }
        )
    except TypeError:
        payload = json.dumps(
            {"function": "", "args": [], "kwargs": {}, "trip_id": None}
        )
    db.run(
        "INSERT INTO job_records VALUES (?,?,?,?,?,?,?)",
        job["id"],
        job["kind"],
        job["label"],
        "accepted",
        payload,
        now_iso(),
        None,
    )


def finish(db, job, state, error=None):
    with db.tx():
        db.run(
            "UPDATE job_records SET state=?,error=?,updated=? WHERE id=?",
            state,
            error,
            now_iso(),
            job["id"],
        )
        if state != "done":
            return
        current = db.one("SELECT * FROM job_records WHERE id=?", job["id"])
        if not current:
            return
        if current["kind"] == "build":
            db.run(
                """UPDATE job_records SET state='superseded' WHERE kind='build' AND state='queued'
                      AND rowid < (SELECT rowid FROM job_records WHERE id=?)""",
                job["id"],
            )
        trip = json.loads(current["payload"]).get("trip_id")
        for old in db.all(
            "SELECT * FROM job_records WHERE kind=? AND id!=? AND state IN ('failed','interrupted')",
            current["kind"],
            job["id"],
        ):
            if (
                current["kind"] not in {"cart", "clear"}
                or json.loads(old["payload"]).get("trip_id") == trip
            ):
                db.run(
                    "UPDATE job_records SET state='superseded' WHERE id=?", old["id"]
                )


def can_resume(db, row):
    payload = json.loads(row["payload"])
    if payload.get("function") not in RESUMABLE:
        return False
    if row["kind"] in {"cart", "clear"}:
        current = db.latest_review()
        if not current or current["trip_id"] != payload.get("trip_id"):
            return False
    if payload.get("function") == "clear_cart":
        scope = payload.get("kwargs", {}).get("scope") or next(
            iter(payload.get("args", [])), "ours"
        )
        if scope == "all":
            return False  # repeat the explicit whole-cart confirmation from its page
    return True


def recovery(db):
    rows = db.all(
        "SELECT * FROM job_records WHERE state IN ('interrupted','queued','failed') ORDER BY updated DESC"
    )
    return [
        dict(
            {k: r[k] for k in ("id", "kind", "label", "state", "error")},
            can_resume=can_resume(db, r),
        )
        for r in rows
    ]


def resume(job_id):
    from . import dinners
    from . import jobs as j

    row = j.db.one("SELECT * FROM job_records WHERE id=?", job_id)
    if not row or row["state"] not in {"interrupted", "queued", "failed"}:
        raise ValueError("That task does not need resuming")
    if not can_resume(j.db, row):
        raise RuntimeError(
            "This action belongs to an earlier trip or needs a new confirmation. Review it from the current page."
        )
    allowed = {
        fn.__name__: fn
        for fn in (
            j.build_review,
            j.run_cart,
            j.send_to_walmart,
            j.clear_cart,
            j.sync_history,
            j.mealie_cleanup_scan,
            j.dedupe_foods,
            dinners.fill_empty,
        )
    }
    payload = json.loads(row["payload"])
    j.db.run("UPDATE job_records SET state='dispatching' WHERE id=?", job_id)
    try:
        job = j.start(
            row["kind"],
            row["label"],
            allowed[payload["function"]],
            *payload["args"],
            **payload["kwargs"],
        )
    except Exception:
        j.db.run("UPDATE job_records SET state=? WHERE id=?", row["state"], job_id)
        raise
    j.db.run(
        "UPDATE job_records SET state='resumed',updated=? WHERE id=?", now_iso(), job_id
    )
    return job


def queue_rebuild():
    from . import jobs as j

    pending = {
        "id": uuid.uuid4().hex[:8],
        "kind": "build",
        "label": "Update the shopping list",
    }
    with j.db.tx():
        record(j.db, pending, j.build_review, (), {"notify": False})
        finish(j.db, pending, "queued")
    job = dispatch_rebuild()
    return job or {"queued": True, "id": pending["id"]}


def dispatch_rebuild():
    from . import jobs as j

    if j.busy():
        return None
    with j.db.tx():
        queued = j.db.all(
            "SELECT id FROM job_records WHERE kind='build' AND state='queued'"
        )
        if not queued:
            return None
        for row in queued:
            j.db.run("UPDATE job_records SET state='dispatching' WHERE id=?", row["id"])
    try:
        job = j.start("build", "Updating the list", j.build_review, notify=False)
    except RuntimeError:
        for row in queued:
            j.db.run("UPDATE job_records SET state='queued' WHERE id=?", row["id"])
        return None
    except Exception:
        for row in queued:
            j.db.run("UPDATE job_records SET state='queued' WHERE id=?", row["id"])
        raise
    for row in queued:
        j.db.run(
            "UPDATE job_records SET state='resumed',updated=? WHERE id=?",
            now_iso(),
            row["id"],
        )
    return job

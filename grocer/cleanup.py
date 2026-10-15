"""Reviewed, reversible cleanup of Grocer's own ingredient records."""

import hashlib
import json
import uuid

from .db import now_iso

TABLES = ("foods", "picks", "stock", "aliases", "parse_cache", "reservations")


def snapshot(db):
    return {
        table: [dict(r) for r in db.all(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in TABLES
    }


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def scan():
    from . import jobs as j

    groups = []
    for category in j.db.all(
        "SELECT DISTINCT category FROM foods WHERE category != 'none'"
    ):
        names = [
            r["key"]
            for r in j.db.all(
                "SELECT key FROM foods WHERE category=? ORDER BY key",
                category["category"],
            )
        ]
        if len(names) < 2:
            continue
        for g in j._confirmed(j.llm.find_duplicates(names)):
            members = [g["keep"], *g["same"]]
            keep = max(
                members,
                key=lambda k: (
                    j.PICK_RANK.get(
                        (j.db.pick(k) or {"source": None})["source"] or "", 0
                    ),
                    k == g["keep"],
                ),
            )
            groups.append(
                {
                    "keep": keep,
                    "same": [k for k in members if k != keep],
                    "products": {
                        k: dict(j.db.pick(k)) if j.db.pick(k) else None for k in members
                    },
                    "stock": {k: j.db.stock(k) for k in members},
                }
            )
    data = {"groups": groups, "version": digest(snapshot(j.db))}
    ident = uuid.uuid4().hex
    j.db.run(
        "INSERT INTO merge_proposals VALUES (?,?,?,?,?)",
        ident,
        json.dumps(data),
        "proposed",
        None,
        now_iso(),
    )
    j.changed()
    return dict(data, id=ident)


def latest():
    from . import jobs as j

    row = j.db.one("SELECT * FROM merge_proposals ORDER BY rowid DESC LIMIT 1")
    return (
        dict(json.loads(row["data"]), id=row["id"], state=row["state"]) if row else None
    )


def apply(ident, indices):
    from . import jobs as j

    if j.busy():
        raise RuntimeError(
            "Wait for the current task to finish before merging ingredients."
        )
    row = j.db.latest_review()
    if not row or row["status"] != "checked_out":
        j.editable_review()
    with j.REVIEW_LOCK, j.db.tx() as c:
        row = j.db.one("SELECT * FROM merge_proposals WHERE id=?", ident)
        if not row or row["state"] != "proposed":
            raise ValueError("Scan for duplicate ingredients again")
        data = json.loads(row["data"])
        before = snapshot(j.db)
        if digest(before) != data["version"]:
            raise RuntimeError(
                "Ingredients changed since this scan. Scan again before merging."
            )
        if not indices or any(
            isinstance(i, bool)
            or not isinstance(i, int)
            or not 0 <= i < len(data["groups"])
            for i in indices
        ):
            raise ValueError("Choose valid groups to merge")
        for i in sorted(set(indices)):
            g = data["groups"][i]
            for name in g["same"]:
                j.merge_into(name, g["keep"])
        after = snapshot(j.db)
        c.execute(
            "UPDATE merge_proposals SET state='applied',snapshot=? WHERE id=?",
            (json.dumps({"before": before, "after": after}), ident),
        )
    j.rebuild_soon()
    j.changed()


def undo(ident):
    from . import jobs as j

    if j.busy():
        raise RuntimeError(
            "Wait for the current task to finish before undoing cleanup."
        )
    row = j.db.latest_review()
    if not row or row["status"] != "checked_out":
        j.editable_review()
    with j.REVIEW_LOCK, j.db.tx() as c:
        row = j.db.one("SELECT * FROM merge_proposals WHERE id=?", ident)
        if not row or row["state"] != "applied":
            raise ValueError("This cleanup cannot be undone")
        saved = json.loads(row["snapshot"])
        if digest(snapshot(j.db)) != digest(saved["after"]):
            raise RuntimeError(
                "Ingredients or stock changed after cleanup. Restore individual ingredients instead of overwriting those changes."
            )
        for table in TABLES:
            c.execute(f"DELETE FROM {table}")
            for record in saved["before"][table]:
                cols = list(record)
                c.execute(
                    f"INSERT INTO {table} ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                    tuple(record.values()),
                )
        c.execute("UPDATE merge_proposals SET state='undone' WHERE id=?", (ident,))
    j.rebuild_soon()
    j.changed()


def apply_manual(old: str, new: str):
    from . import jobs as j

    if old == new:
        raise ValueError("Choose two different ingredients")
    data = {
        "groups": [
            {
                "keep": new,
                "same": [old],
                "products": {
                    k: dict(j.db.pick(k)) if j.db.pick(k) else None for k in (old, new)
                },
                "stock": {k: j.db.stock(k) for k in (old, new)},
            }
        ],
        "version": digest(snapshot(j.db)),
    }
    ident = uuid.uuid4().hex
    j.db.run(
        "INSERT INTO merge_proposals VALUES (?,?,?,?,?)",
        ident,
        json.dumps(data),
        "proposed",
        None,
        now_iso(),
    )
    apply(ident, [0])

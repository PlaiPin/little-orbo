"""Invalidate serialized local derivatives when their source evidence is deleted.

Call inside the existing Store transaction. Identifiers and remote revocation
fences remain; content does not. Recipient copies and owner-created external
files are not silently claimed erased.
"""

import json

from .contracts import canonical


def _references(value, identifiers):
    if isinstance(value, str):
        return value in identifiers
    if isinstance(value, dict):
        return any(_references(item, identifiers) for item in value.values())
    if isinstance(value, list):
        return any(_references(item, identifiers) for item in value)
    return False


def _contains(body, identifiers):
    try:
        return _references(json.loads(body), identifiers)
    except (ValueError, TypeError):
        return False


def scrub_derivatives(store, owner, environment, identifiers, task_ids=()):
    db = store.db
    identifiers, task_ids = set(identifiers), set(task_ids)
    tables = {
        row[0]
        for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")
    }
    tombstone = canonical(
        {
            "deleted": True,
            "reason": "source_evidence_deleted",
            "recipient_copies_erased": False,
        }
    )
    # An old sighting can also support another task's recall/result. Discover all
    # dependent owned tasks before clearing serialized results or event payloads.
    if "embodied_results" in tables:
        for row in db.execute(
            "SELECT r.task_id,r.result FROM embodied_results r JOIN embodied_tasks t ON t.id=r.task_id WHERE t.owner=? AND t.environment=?",
            (owner, environment),
        ):
            if _contains(row["result"], identifiers):
                task_ids.add(row["task_id"])
    for row in db.execute(
        "SELECT id,result FROM embodied_tasks WHERE owner=? AND environment=?",
        (owner, environment),
    ):
        if _contains(row["result"], identifiers):
            task_ids.add(row["id"])
    needles = identifiers | task_ids
    exports = [
        dict(row)
        for row in db.execute(
            "SELECT x.id,x.content,x.digest FROM exports x JOIN episodes e ON e.id=x.episode_id WHERE e.owner=?",
            (owner,),
        )
    ]
    affected = set()
    changed = True
    while changed:
        changed = False
        for row in exports:
            if row["id"] not in affected and _contains(row["content"], needles):
                affected.add(row["id"])
                needles.update((row["id"], row["digest"]))
                changed = True
    db.execute(
        "CREATE TABLE IF NOT EXISTS deleted_evidence_exports(export_id TEXT PRIMARY KEY REFERENCES exports(id), deleted REAL NOT NULL)"
    )
    for export_id in affected:
        # Keep original digest: ordinary preview/approval must fail, never silently
        # reapprove a changed package. A separate fence permits revoke-only lookup.
        db.execute("UPDATE exports SET content=? WHERE id=?", (tombstone, export_id))
        db.execute("UPDATE approvals SET revoked=1 WHERE export_id=?", (export_id,))
        db.execute(
            "INSERT OR IGNORE INTO deleted_evidence_exports VALUES(?,?)",
            (export_id, store.clock.utc()),
        )
        if "commons_reviews" in tables:
            db.execute(
                "UPDATE commons_reviews SET body=?,status=?,state='revoke_pending' WHERE export_id=?",
                (tombstone, tombstone, export_id),
            )
    # Journal copies must not regenerate a fresh export containing deleted facts.
    for row in db.execute(
        "SELECT v.id,v.payload FROM events v JOIN episodes e ON e.id=v.episode_id WHERE e.owner=?",
        (owner,),
    ).fetchall():
        if _contains(row["payload"], needles):
            db.execute(
                "UPDATE events SET kind='evidence.gap',payload=? WHERE id=?",
                (tombstone, row["id"]),
            )
    for task_id in task_ids:
        owned = db.execute(
            "SELECT 1 FROM embodied_tasks WHERE id=? AND owner=? AND environment=?",
            (task_id, owner, environment),
        ).fetchone()
        if owned:
            db.execute(
                "UPDATE embodied_tasks SET result=?,state=CASE WHEN state IN ('accepted','working') THEN 'unknown' ELSE state END WHERE id=?",
                (tombstone, task_id),
            )
            db.execute(
                "UPDATE task_operations SET result=? WHERE task_id=?",
                (tombstone, task_id),
            )
            if "embodied_results" in tables:
                db.execute("DELETE FROM embodied_results WHERE task_id=?", (task_id,))
        if "remote_task_deliveries" in tables:
            for delivery in db.execute(
                "SELECT id FROM remote_task_deliveries WHERE task_id IN "
                "(SELECT task_id FROM remote_task_bindings WHERE task_id=? AND owner=? AND environment=?)",
                (task_id, owner, environment),
            ).fetchall():
                # The table has a (task,revision,content) uniqueness constraint.
                # Distinct delivery tombstones preserve those receipt identities.
                body = canonical(
                    {
                        "deleted": True,
                        "reason": "source_evidence_deleted",
                        "delivery_id": delivery["id"],
                    }
                )
                db.execute(
                    "UPDATE remote_task_deliveries SET content=? WHERE id=?",
                    (body, delivery["id"]),
                )
        if "live_deliveries" in tables:
            sessions = [
                row[0]
                for row in db.execute(
                    "SELECT DISTINCT d.session_id FROM live_deliveries d JOIN live_sessions s ON s.id=d.session_id WHERE d.task_id=? AND s.owner=?",
                    (task_id, owner),
                )
            ]
            db.execute(
                "UPDATE live_deliveries SET content=?,state='deleted' WHERE task_id=? AND session_id IN (SELECT id FROM live_sessions WHERE owner=?)",
                (tombstone, task_id, owner),
            )
            for session_id in sessions:
                # Generated/played transcription can contain the released fact
                # without its evidence ID. Remove assistant fragments in the
                # affected session instead of pretending exact attribution exists.
                db.execute(
                    "DELETE FROM live_fragments WHERE session_id=? AND role='assistant'",
                    (session_id,),
                )
    return {
        "export_ids": sorted(affected),
        "task_ids": sorted(task_ids),
        "recipient_copies_erased": False,
    }

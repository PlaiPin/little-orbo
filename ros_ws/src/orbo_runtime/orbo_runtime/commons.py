"""Real contributor daemon IPC; exact reviewed approval requires Orbo patch 0003."""

import asyncio
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from .contracts import Rejected, canonical
from .traces import Exports, reconstruct_commons, trajectory


class ContributorIPC:
    def __init__(self, socket_path):
        self.path = Path(socket_path)

    async def call(self, method, params):
        # The daemon socket and its directory are the upstream authentication boundary.
        parent, endpoint = self.path.parent.lstat(), self.path.lstat()
        if (
            not stat.S_ISDIR(parent.st_mode)
            or not stat.S_ISSOCK(endpoint.st_mode)
            or parent.st_uid != os.getuid()
            or endpoint.st_uid != os.getuid()
            or parent.st_mode & 0o077
            or endpoint.st_mode & 0o077
        ):
            raise Rejected("private owner-controlled contributor socket required")
        request_id = uuid.uuid4().int & ((1 << 53) - 1)
        async with asyncio.timeout(60):
            reader, writer = await asyncio.open_unix_connection(
                self.path, limit=1_048_576
            )
            try:
                writer.write(
                    (
                        canonical(
                            {"id": request_id, "method": method, "params": params}
                        )
                        + "\n"
                    ).encode()
                )
                await writer.drain()
                for _ in range(32):
                    raw = await reader.readline()
                    if not raw or not raw.endswith(b"\n"):
                        raise Rejected("incomplete contributor response")
                    reply = json.loads(raw)
                    if "id" not in reply:  # bounded unsolicited daemon events
                        continue
                    if reply["id"] != request_id:
                        raise Rejected("contributor response identity mismatch")
                    if reply.get("error"):
                        # Do not echo arbitrary daemon content into the journal.
                        raise Rejected("contributor refused request")
                    if "result" not in reply:
                        raise Rejected("missing contributor result")
                    return reply["result"]
                raise Rejected("too many contributor events")
            finally:
                writer.close()
                await writer.wait_closed()

    async def review(self, entry_id):
        uuid.UUID(entry_id)
        summary = await self.call("preview", {"entry_id": entry_id})
        offset, body_digest, envelope_digest, total = 0, None, None, None
        chunks = []
        for _ in range(32):
            params = {"entry_id": entry_id, "offset": offset, "limit": 131072}
            if body_digest:
                params["body_digest"] = body_digest
            page = await self.call("preview_body", params)
            if (
                page.get("enrolled") is not True
                or page.get("entry_id") != entry_id
                or page.get("offset") != offset
            ):
                raise Rejected("enrolled complete preview required")
            if body_digest is None:
                body_digest, envelope_digest, total = (
                    page["body_digest"],
                    page["envelope_digest"],
                    page["total_bytes"],
                )
            if (page["body_digest"], page["envelope_digest"], page["total_bytes"]) != (
                body_digest,
                envelope_digest,
                total,
            ):
                raise Rejected("contributor preview changed while reading")
            if type(total) is not int or not 0 < total <= 2_097_152:
                raise Rejected("preview size outside bounded profile")
            chunk = page["chunk"].encode()
            if not chunk or len(chunk) > 131076:
                raise Rejected("invalid preview chunk")
            chunks.append(chunk)
            offset += len(chunk)
            if page["next_offset"] is None:
                break
            if page["next_offset"] != offset or offset >= total:
                raise Rejected("invalid preview continuation")
        else:
            raise Rejected("preview pagination exceeded")
        body = b"".join(chunks)
        if (
            len(body) != total
            or "sha256:" + hashlib.sha256(body).hexdigest() != body_digest
        ):
            raise Rejected("incomplete or altered contributor preview")
        return {
            "summary": summary,
            "body": body.decode(),
            "body_digest": body_digest,
            "envelope_digest": envelope_digest,
        }


class Commons:
    def __init__(self, store, ipc):
        self.store, self.ipc = store, ipc
        self.exports = Exports(store)
        self.store.db.execute("""CREATE TABLE IF NOT EXISTS commons_reviews(
            entry_id TEXT PRIMARY KEY, export_id TEXT NOT NULL REFERENCES exports(id),
            export_digest TEXT NOT NULL, envelope_digest TEXT NOT NULL, body_digest TEXT NOT NULL,
            body TEXT NOT NULL, state TEXT NOT NULL, status TEXT NOT NULL)""")

    async def review(self, owner, export_id, entry_id):
        export = self.exports.preview(owner, export_id)
        self.store.permission(owner, "commons.review", export["recipient"])
        preview = await self.ipc.review(entry_id)
        self.store.permission(owner, "commons.review", export["recipient"])
        reconstructed = reconstruct_commons(json.loads(preview["body"]))
        if (
            hashlib.sha256(canonical(reconstructed).encode()).hexdigest()
            != export["digest"]
        ):
            raise Rejected("contributor entry does not preserve this exact task export")
        expected_session = (
            "sha256:" + hashlib.sha256(trajectory(reconstructed)).hexdigest()
        )
        entry = preview["summary"].get("entry", {})
        if (
            entry.get("entry_id") != entry_id
            or entry.get("session_hash") != expected_session
        ):
            raise Rejected(
                "contributor source must match the exact staged trajectory bytes"
            )
        previous = self.store.db.execute(
            "SELECT state FROM commons_reviews WHERE entry_id=?", (entry_id,)
        ).fetchone()
        if previous and previous["state"] != "reviewed":
            raise Rejected(
                "recover the previous contribution; a new review requires a fresh entry"
            )
        # Review can disclose to the configured privacy service; this scope is separate from upload.
        self.store.db.execute(
            """INSERT INTO commons_reviews VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(entry_id) DO UPDATE SET export_id=excluded.export_id,
            export_digest=excluded.export_digest,envelope_digest=excluded.envelope_digest,
            body_digest=excluded.body_digest,body=excluded.body,state=excluded.state,status=excluded.status
            WHERE commons_reviews.state = 'reviewed'""",
            (
                entry_id,
                export_id,
                export["digest"],
                preview["envelope_digest"],
                preview["body_digest"],
                preview["body"],
                "reviewed",
                canonical(preview["summary"]),
            ),
        )
        return {
            "entry_id": entry_id,
            "export_id": export_id,
            **preview,
            "disclosure": "Review all redacted content and recipient before approving. Approval can initiate upload.",
        }

    async def submit_reviewed(
        self, owner, export_id, entry_id, envelope_digest, body_digest
    ):
        export = self.exports.preview(owner, export_id)
        self.exports.approved_bytes(owner, export_id)
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT * FROM commons_reviews WHERE entry_id=? AND export_id=?",
                (entry_id, export_id),
            ).fetchone()
            if (
                not row
                or row["state"] != "reviewed"
                or row["export_digest"] != export["digest"]
                or row["envelope_digest"] != envelope_digest
                or row["body_digest"] != body_digest
            ):
                raise Rejected(
                    "exact current review required; uncertain submissions must be reconciled"
                )
            db.execute(
                "UPDATE commons_reviews SET state='approval_unknown' WHERE entry_id=?",
                (entry_id,),
            )
            self.store.event(
                export["episode_id"],
                "commons.approval_requested",
                {
                    "entry_id": entry_id,
                    "envelope_digest": envelope_digest,
                    "body_digest": body_digest,
                },
            )
        # No retries: loss of this response leaves a durable uncertain state.
        result = await self.ipc.call(
            "approve_reviewed",
            {"entry_id": entry_id, "expected_envelope_digest": envelope_digest},
        )
        state = "approved" if result.get("approved") == 1 else "refused"
        # A revoke or recovery decision made while IPC was pending wins.
        self.store.db.execute(
            "UPDATE commons_reviews SET state=?,status=? WHERE entry_id=? AND state='approval_unknown'",
            (state, canonical(result), entry_id),
        )
        state = self.store.db.execute(
            "SELECT state FROM commons_reviews WHERE entry_id=?", (entry_id,)
        ).fetchone()[0]
        self.store.event(
            export["episode_id"],
            "commons.approval_result",
            {
                "entry_id": entry_id,
                "state": state,
                "result": result,
                "delivery_established": False,
            },
        )
        return {"state": state, "result": result, "delivery_established": False}

    def linked(self, owner, export_id, entry_id, *, allow_deleted=False):
        self.exports.preview(owner, export_id, allow_deleted=allow_deleted)
        row = self.store.db.execute(
            "SELECT * FROM commons_reviews WHERE entry_id=? AND export_id=?",
            (entry_id, export_id),
        ).fetchone()
        if not row:
            raise Rejected("unknown linked contribution")
        return row

    async def status(self, owner, export_id, entry_id):
        row = self.linked(owner, export_id, entry_id)
        package = reconstruct_commons(json.loads(row["body"]))
        session_hash = "sha256:" + hashlib.sha256(trajectory(package)).hexdigest()
        # Exact entry + source lookup; never infer an outcome from absence on a page.
        history, offset, digest = [], 0, None
        for _ in range(100):
            params = {
                "entry_id": entry_id,
                "session_hash": session_hash,
                "offset": offset,
            }
            if digest:
                params["history_digest"] = digest
            evidence = await self.ipc.call("orbo_entry_status", params)
            if (
                evidence.get("schema") != "orbo.entry-status.v1"
                or evidence.get("entry_id") != entry_id
                or evidence.get("session_hash") != session_hash
                or evidence.get("offset") != offset
                or not isinstance(evidence.get("history"), list)
                or len(evidence["history"]) > 100
                or not isinstance(evidence.get("history_digest"), str)
                or (digest and evidence["history_digest"] != digest)
            ):
                raise Rejected("qualified exact-entry recovery required")
            digest = evidence["history_digest"]
            history.extend(evidence["history"])
            next_offset = evidence.get("next_offset")
            if next_offset is None:
                break
            if (
                type(next_offset) is not int
                or next_offset != offset + len(evidence["history"])
                or next_offset <= offset
            ):
                raise Rejected("invalid recovery continuation")
            offset = next_offset
        else:
            raise Rejected("recovery history exceeds bounded profile")
        evidence = {**evidence, "history": history, "history_complete": True}
        linked = [
            item
            for item in evidence.get("history", [])
            if item.get("session_hash") == session_hash
            and item.get("previous_account") is not True
        ]
        submission_ids = sorted(
            {str(uuid.UUID(item["submission_id"])) for item in linked}
        )
        # Only positive evidence advances uncertain approval; never retry a send.
        remote = evidence.get("queue") or {}
        if remote.get("revoked") is True:
            self.exports.revoke(owner, export_id)
            self.store.db.execute(
                "UPDATE commons_reviews SET state='revoked' WHERE entry_id=?",
                (entry_id,),
            )
        elif remote.get("envelope_digest") == row["envelope_digest"]:
            next_state = {
                "approved": "approved",
                "uploading": "uploading",
                "uploaded": "uploaded",
                "refused": "refused",
                "expired": "expired",
                "superseded": "superseded",
            }.get(remote.get("state"))
            if next_state:
                self.store.db.execute(
                    "UPDATE commons_reviews SET state=? WHERE entry_id=? AND state IN ('approval_unknown','approved','uploading')",
                    (next_state, entry_id),
                )
        self.store.db.execute(
            "UPDATE commons_reviews SET status=? WHERE entry_id=?",
            (canonical(evidence), entry_id),
        )
        current = self.linked(owner, export_id, entry_id)["state"]
        return {
            "local_state": current,
            "entry_id": entry_id,
            "daemon": evidence,
            "submission_ids": submission_ids,
            "delivery_established": False,
            "accounting": "No award, received credit, or applied usage inferred from queue state",
        }

    def recovery_queue(self, owner):
        return [
            dict(row)
            for row in self.store.db.execute(
                "SELECT c.entry_id,c.export_id,c.state FROM commons_reviews c "
                "JOIN exports x ON x.id=c.export_id JOIN episodes e ON e.id=x.episode_id "
                "WHERE e.owner=? AND c.state IN ('approval_unknown','revoke_pending','approved','uploading') "
                "ORDER BY c.entry_id",
                (owner,),
            )
        ]

    async def recover(self, owner, export_id, entry_id):
        row = self.linked(owner, export_id, entry_id, allow_deleted=True)
        # Recovery is explicit and restart-safe; only cancellation is retryable.
        # Expired/revoked grants cannot leave an uncertain send authorized locally.
        try:
            self.exports.approved_bytes(owner, export_id)
        except Rejected:
            if row["state"] not in {"reviewed", "refused", "expired", "superseded"}:
                return await self.revoke(owner, export_id, entry_id)
        if row["state"] in {"revoke_pending", "revoked"}:
            return await self.revoke(owner, export_id, entry_id)
        return await self.status(owner, export_id, entry_id)

    async def revoke(self, owner, export_id, entry_id):
        self.linked(owner, export_id, entry_id, allow_deleted=True)
        self.exports.revoke(owner, export_id)
        self.store.db.execute(
            "UPDATE commons_reviews SET state='revoke_pending' WHERE entry_id=?",
            (entry_id,),
        )
        # Persistent daemon fence prevents a delayed approve from reviving this entry.
        # IPC failure leaves revoke_pending durable across runtime restarts.
        result = await self.ipc.call("orbo_revoke_reviewed", {"entry_id": entry_id})
        if (
            result.get("schema") != "orbo.revocation.v1"
            or result.get("entry_id") != entry_id
            or result.get("fenced") is not True
        ):
            raise Rejected("durable daemon revocation not established")
        self.store.db.execute(
            "UPDATE commons_reviews SET state='revoked',status=? WHERE entry_id=? AND state='revoke_pending'",
            (canonical(result), entry_id),
        )
        return {
            "local_state": "revoked",
            "result": result,
            "already_delivered_copies_recalled": False,
        }

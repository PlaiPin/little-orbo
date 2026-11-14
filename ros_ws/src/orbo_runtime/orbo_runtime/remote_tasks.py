"""Authenticated, environment-scoped task ingress over a private SSH-forwarded socket.

This gateway never exposes arbitrary owner IPC operations or ROS endpoints. Tokens
are supplied by deployment, hashed in memory and never journaled. Delivery records
are durable; returning a response is not treated as requester acknowledgment.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import math

from .contracts import Rejected, canonical, identity


@dataclass(frozen=True)
class Principal:
    name: str
    owner: str
    environments: tuple[str, ...]
    token_sha256: str
    expires: float

    def __post_init__(self):
        if (
            not self.name
            or not self.owner
            or not self.environments
            or not isinstance(self.token_sha256, str)
            or len(self.token_sha256) != 64
            or any(c not in "0123456789abcdef" for c in self.token_sha256)
            or type(self.expires) not in {int, float}
            or not math.isfinite(self.expires)
            or self.expires <= 0
            or len(self.name) > 128
            or not all(c.isalnum() or c in "_.-" for c in self.name)
            or any(
                not isinstance(e, str) or not e or len(e) > 256
                for e in self.environments
            )
        ):
            raise Rejected(
                "explicit remote identity, environments and token hash required"
            )


class RemoteTasks:
    """Application gateway; callbacks run through the same local task coordinator."""

    def __init__(self, store, principals, *, submit, get, cancel, release):
        self.store = store
        self.principals = {p.name: p for p in principals}
        if len(self.principals) != len(principals):
            raise Rejected("duplicate remote principal")
        self.submit, self.get, self.cancel, self.release = submit, get, cancel, release
        self.inflight = 0
        store.db.executescript("""
        CREATE TABLE IF NOT EXISTS remote_task_bindings(
          task_id TEXT PRIMARY KEY, principal TEXT NOT NULL, owner TEXT NOT NULL,
          environment TEXT NOT NULL, destination TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS remote_task_deliveries(
          id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES remote_task_bindings(task_id),
          revision INTEGER NOT NULL, content TEXT NOT NULL, created REAL NOT NULL,
          acknowledged REAL, UNIQUE(task_id,revision,content));
        """)

    def _event(self, task_id, kind, payload):
        task = self.store.db.execute(
            "SELECT episode_id FROM embodied_tasks WHERE id=?", (task_id,)
        ).fetchone()
        if task:
            self.store.event(task["episode_id"], kind, dict(payload, task_id=task_id))

    def authenticate(self, request):
        principal = self.principals.get(request.get("principal"))
        token = request.get("token")
        if not isinstance(token, str) or not 32 <= len(token) <= 1024:
            raise Rejected("remote authentication failed")
        supplied = hashlib.sha256(token.encode()).hexdigest()
        expected = principal.token_sha256 if principal else "0" * 64
        if not hmac.compare_digest(supplied, expected) or principal is None:
            raise Rejected("remote authentication failed")
        if principal.expires <= self.store.clock.utc():
            raise Rejected("remote authentication expired")
        self.store.permission(principal.owner, "remote.task", principal.name)
        return principal

    def owned(self, principal, task_id):
        row = self.store.db.execute(
            "SELECT * FROM remote_task_bindings WHERE task_id=? AND principal=? AND owner=?",
            (task_id, principal.name, principal.owner),
        ).fetchone()
        if row is None or row["environment"] not in principal.environments:
            raise Rejected("unknown authorized remote task")
        return dict(row)

    async def call(self, request):
        if not isinstance(request, dict):
            raise Rejected("remote request must be an object")
        principal = self.authenticate(request)
        method, args = request.get("method"), request.get("args", {})
        if not isinstance(args, dict):
            raise Rejected("remote arguments must be an object")
        if method == "task.submit":
            body = dict(args.get("request", {}))
            if body.get("environment") not in principal.environments:
                raise Rejected("environment outside remote scope")
            body["destination"] = "remote:" + principal.name
            # Caller cannot choose identity or impersonate a local provenance.
            body.pop("owner", None)
            body["requester"] = principal.name
            task = await self.submit(
                principal.owner, "remote:" + principal.name + ":" + args["key"], body
            )
            with self.store.transaction() as db:
                existing = db.execute(
                    "SELECT principal FROM remote_task_bindings WHERE task_id=?",
                    (task["id"],),
                ).fetchone()
                if existing and existing[0] != principal.name:
                    raise Rejected("task already bound to another requester")
                db.execute(
                    "INSERT OR IGNORE INTO remote_task_bindings VALUES(?,?,?,?,?)",
                    (
                        task["id"],
                        principal.name,
                        principal.owner,
                        body["environment"],
                        body["destination"],
                    ),
                )
            return {
                "task_id": task["id"],
                "revision": task["revision"],
                "state": task["state"],
            }
        binding = self.owned(principal, args.get("task_id"))
        if method == "task.cancel":
            task = await self.cancel(principal.owner, binding["task_id"])
            return {
                "task_id": binding["task_id"],
                "state": task.get("state", "cancel_requested"),
                "stop_requested": True,
                "physical_cessation": "not_independently_verified",
            }
        if method == "delivery.ack":
            with self.store.transaction() as db:
                result = db.execute(
                    "UPDATE remote_task_deliveries SET acknowledged=COALESCE(acknowledged,?) WHERE id=? AND task_id=?",
                    (
                        self.store.clock.utc(),
                        args.get("delivery_id"),
                        binding["task_id"],
                    ),
                )
                if result.rowcount != 1:
                    raise Rejected("unknown requester delivery")
            self._event(
                binding["task_id"],
                "task.remote_delivery_acknowledged",
                {"delivery_id": args["delivery_id"], "requester": principal.name},
            )
            return {"acknowledged": True}
        if method != "task.get":
            raise Rejected("unsupported remote method")
        task = self.get(principal.owner, binding["task_id"])
        # Callback returns only released facts, never the private task or journal.
        released = self.release(principal.owner, task, binding["destination"])
        self.authenticate(request)  # Recheck revocation before disclosing content.
        if released is None:
            return {
                "task_id": task["id"],
                "revision": task["revision"],
                "state": task["state"],
            }
        content = canonical(released)
        if len(content.encode()) > 32768:
            raise Rejected("remote result exceeds release bound")
        with self.store.transaction() as db:
            row = db.execute(
                "SELECT id,content,created,acknowledged FROM remote_task_deliveries WHERE task_id=? AND revision=? ORDER BY created LIMIT 1",
                (task["id"], task["revision"]),
            ).fetchone()
            if row is None:
                delivery_id = identity()
                db.execute(
                    "INSERT INTO remote_task_deliveries VALUES(?,?,?,?,?,NULL)",
                    (
                        delivery_id,
                        task["id"],
                        task["revision"],
                        content,
                        self.store.clock.utc(),
                    ),
                )
                row = db.execute(
                    "SELECT id,content,created,acknowledged FROM remote_task_deliveries WHERE id=?",
                    (delivery_id,),
                ).fetchone()
        self._event(
            task["id"],
            "task.remote_delivery_selected",
            {
                "delivery_id": row["id"],
                "revision": task["revision"],
                "requester": principal.name,
                "state": "selected_for_response",
                "acknowledged": row["acknowledged"] is not None,
            },
        )
        return {
            "task_id": task["id"],
            "revision": task["revision"],
            "state": task["state"],
            "delivery_id": row["id"],
            "acknowledged": row["acknowledged"] is not None,
            "released_at": row["created"],
            "result": json.loads(row["content"]),
        }

    async def handle(self, reader, writer):
        self.inflight += 1
        try:
            if self.inflight > 8:
                raise Rejected("remote request capacity reached")
            async with asyncio.timeout(15):
                line = await reader.readline()
                if not line or len(line) > 32768:
                    raise Rejected("remote request size limit")
                result = {"ok": True, "result": await self.call(json.loads(line))}
        except Exception as exc:
            result = {
                "ok": False,
                "error": str(exc) if isinstance(exc, Rejected) else type(exc).__name__,
            }
        finally:
            self.inflight -= 1
        try:
            writer.write((canonical(result) + "\n").encode())
            async with asyncio.timeout(5):
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

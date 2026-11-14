import asyncio
import json
from dataclasses import asdict

from .contracts import (
    Action,
    ActionResult,
    Capability,
    Rejected,
    canonical,
    digest,
    finite,
    identity,
)


class Broker:
    def __init__(self, store, adapter):
        self.store, self.adapter = store, adapter
        self.lease_deadline = None

    def acquire(self, owner, ttl=30):
        finite(ttl, 1, 60)
        self.store.permission(owner, "robot.command", self.adapter.robot_id)
        now = self.store.clock.utc()
        with self.store.transaction() as db:
            if (
                getattr(self.adapter, "shares_motion_session", False)
                and db.execute(
                    "SELECT 1 FROM commands WHERE robot=? AND state IN "
                    "('pending','accepted','executing','cancel_requested','unknown') LIMIT 1",
                    (self.adapter.robot_id,),
                ).fetchone()
            ):
                raise Rejected("robot action active; join its session or stop it first")
            old = db.execute(
                "SELECT * FROM leases WHERE robot=?", (self.adapter.robot_id,)
            ).fetchone()
            if (
                old
                and old["boot"] == self.store.boot
                and old["expires"] > now
                and old["holder"] != owner
            ):
                raise Rejected("robot already owned")
            epoch = max(
                old["epoch"] + 1 if old else 1,
                getattr(self.adapter, "epoch_seen", 0) + 1,
            )
            db.execute(
                "INSERT INTO leases VALUES(?,?,?,?,?) ON CONFLICT(robot) DO UPDATE SET holder=excluded.holder,epoch=excluded.epoch,expires=excluded.expires,boot=excluded.boot",
                (self.adapter.robot_id, owner, epoch, now + ttl, self.store.boot),
            )
            self.store.event(
                None,
                "lease.acquired",
                {"robot": self.adapter.robot_id, "owner": owner, "epoch": epoch},
            )
        self.lease_deadline = self.store.clock.mono() + ttl
        return epoch

    def check_lease(self, owner, epoch):
        self.store.permission(owner, "robot.command", self.adapter.robot_id)
        row = self.store.db.execute(
            "SELECT * FROM leases WHERE robot=?", (self.adapter.robot_id,)
        ).fetchone()
        if (
            not row
            or row["holder"] != owner
            or row["epoch"] != epoch
            or row["boot"] != self.store.boot
            or row["expires"] <= self.store.clock.utc()
            or self.lease_deadline is None
            or self.lease_deadline <= self.store.clock.mono()
        ):
            raise Rejected("expired or fenced lease")

    def gesture_epoch(self, owner):
        """Join this owner's driving lease without extending or replacing it."""
        if getattr(self.adapter, "shares_motion_session", False):
            epoch = self.adapter.shared_wheel_epoch
            if epoch is not None:
                self.check_lease(owner, epoch)
                return epoch
            # Planning/arming/stopping and uncertain actions must not be fenced
            # by a new stationary gesture lease. The caller can retry when ready.
            busy = self.store.db.execute(
                "SELECT 1 FROM commands WHERE robot=? AND state IN "
                "('pending','accepted','executing','cancel_requested','unknown') LIMIT 1",
                (self.adapter.robot_id,),
            ).fetchone()
            if busy:
                raise Rejected("existing robot action has no joinable wheel session")
        return self.acquire(owner)

    async def dispatch(
        self, owner, episode_id, key, capability, parameters, epoch, duration=2
    ):
        finite(duration, 0.05, 30)
        spec = self.adapter.capabilities.get(capability)
        if not spec or (not spec.qualified and not self.adapter.simulated):
            raise Rejected("unqualified capability")
        if duration > spec.max_duration:
            raise Rejected("capability duration exceeded")
        # Only named, commissioned gestures. Navigation has its own typed adapter.
        if capability == "attention.gesture" and parameters != {"gesture": "attention"}:
            raise Rejected("unsupported gesture parameters")
        body = {
            "episode": episode_id,
            "capability": capability,
            "parameters": parameters,
            "epoch": epoch,
            "duration": duration,
        }
        with self.store.transaction() as db:
            replay = self.store.replay(owner, "command:" + key, body)
            if replay is not None:
                return self.command(replay["command_id"])
            self.store.require_episode(episode_id, owner)
            self.check_lease(owner, epoch)
            rows = db.execute(
                "SELECT resources FROM commands WHERE robot=? AND state IN ('pending','accepted','executing','cancel_requested','unknown')",
                (self.adapter.robot_id,),
            ).fetchall()
            if any(set(json.loads(row[0])) & set(spec.resources) for row in rows):
                raise Rejected("resource busy or outcome unreconciled")
            action = Action(
                identity(),
                episode_id,
                self.adapter.robot_id,
                capability,
                parameters,
                epoch,
                self.store.clock.utc() + duration,
                duration,
            )
            encoded = canonical(asdict(action))
            db.execute(
                "INSERT INTO commands VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    action.command_id,
                    episode_id,
                    self.adapter.robot_id,
                    capability,
                    canonical(spec.resources),
                    encoded,
                    digest(asdict(action)),
                    epoch,
                    action.deadline,
                    "pending",
                    "none",
                ),
            )
            db.execute("INSERT INTO outbox VALUES(?,'intent')", (action.command_id,))
            self.store.event(episode_id, "action.intent", asdict(action))
            self.store.remember(
                owner, "command:" + key, body, {"command_id": action.command_id}
            )
        try:
            self.check_lease(owner, epoch)
            async with asyncio.timeout(duration):
                result = await self._dispatch_with_lease(owner, epoch, action)
            if result.command_id != action.command_id:
                raise Rejected("uncorrelated adapter result")
            self.result(result)
        except BaseException:
            self.result(ActionResult(action.command_id, "unknown", "none"))
            # Bounded stop is independent of the failed call. Its success is not motion evidence.
            try:
                async with asyncio.timeout(1):
                    await asyncio.shield(
                        self.adapter.controlled_stop("dispatch uncertain")
                    )
            except (Exception, asyncio.CancelledError):
                pass
            raise
        return self.command(action.command_id)

    async def _dispatch_with_lease(self, owner, epoch, action):
        async def guard():
            while True:
                self.check_lease(owner, epoch)
                await asyncio.sleep(0.02)

        work = asyncio.create_task(self.adapter.dispatch(action))
        monitor = asyncio.create_task(guard())
        try:
            done, _ = await asyncio.wait(
                {work, monitor}, return_when=asyncio.FIRST_COMPLETED
            )
            if monitor in done:
                await monitor
            self.check_lease(owner, epoch)
            return await work
        finally:
            work.cancel()
            monitor.cancel()
            await asyncio.gather(work, monitor, return_exceptions=True)

    def command(self, command_id):
        row = self.store.db.execute(
            "SELECT * FROM commands WHERE id=?", (command_id,)
        ).fetchone()
        if not row:
            raise Rejected("unknown command")
        return dict(row)

    def result(self, result, *, reconciliation=False):
        allowed = {
            "accepted",
            "executing",
            "completed",
            "cancelled",
            "rejected",
            "failed",
            "unknown",
        }
        if result.state not in allowed:
            raise Rejected("invalid adapter state")
        with self.store.transaction() as db:
            row = self.command(result.command_id)
            if row["robot"] != self.adapter.robot_id:
                raise Rejected("wrong robot")
            old = row["state"]
            spec = self.adapter.capabilities[row["capability"]]
            if result.evidence != "none" and result.evidence not in spec.evidence:
                raise Rejected("unsupported evidence")
            if result.state in {"completed", "cancelled"} and result.evidence == "none":
                raise Rejected("terminal state requires evidence")
            if old in {"completed", "cancelled", "failed", "rejected"}:
                return False
            if old == "unknown" and not reconciliation:
                return False
            if old == "cancel_requested" and result.state not in {
                "cancelled",
                "failed",
                "unknown",
            }:
                return False
            if old == "executing" and result.state == "accepted":
                return False
            if (
                self.store.clock.utc() >= row["deadline"]
                and result.state in {"accepted", "executing", "completed"}
                and not reconciliation
            ):
                result = ActionResult(
                    result.command_id, "unknown", "none", {"reason": "late feedback"}
                )
            db.execute(
                "UPDATE commands SET state=?,evidence=? WHERE id=?",
                (result.state, result.evidence, result.command_id),
            )
            db.execute(
                "UPDATE outbox SET state=? WHERE id=?",
                (
                    "reconcile" if result.state == "unknown" else "acknowledged",
                    result.command_id,
                ),
            )
            self.store.event(
                row["episode_id"], "action." + result.state, asdict(result)
            )
            return True

    async def cancel(self, owner, command_id):
        with self.store.transaction() as db:
            row = self.command(command_id)
            episode = db.execute(
                "SELECT owner FROM episodes WHERE id=?", (row["episode_id"],)
            ).fetchone()
            if episode[0] != owner:
                raise Rejected("command owner mismatch")
            if row["state"] in {"completed", "cancelled", "failed", "rejected"}:
                return row
            db.execute(
                "UPDATE commands SET state='cancel_requested' WHERE id=?", (command_id,)
            )
            self.store.event(
                row["episode_id"], "action.cancel_requested", {"command_id": command_id}
            )
        try:
            async with asyncio.timeout(1):
                result = await self.adapter.cancel(command_id)
            if result.command_id != command_id:
                raise Rejected("uncorrelated cancellation")
            self.result(result)
        except BaseException:
            self.result(ActionResult(command_id, "unknown", "none"))
            raise
        return self.command(command_id)

    async def stop(self, reason="owner stop"):
        with self.store.transaction() as db:
            db.execute(
                "UPDATE leases SET expires=0 WHERE robot=?", (self.adapter.robot_id,)
            )
            ids = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM commands WHERE robot=? AND state IN ('pending','accepted','executing','cancel_requested')",
                    (self.adapter.robot_id,),
                )
            ]
        self.lease_deadline = None
        for command_id in ids:
            self.result(ActionResult(command_id, "unknown", "none", {"reason": reason}))
        async with asyncio.timeout(1):
            await self.adapter.controlled_stop(reason)

    async def reconcile(self, command_id):
        async with asyncio.timeout(2):
            result = await self.adapter.reconcile(command_id)
        if result.command_id != command_id:
            raise Rejected("uncorrelated reconciliation")
        self.result(result, reconciliation=True)
        return self.command(command_id)


class FakeRobot:
    """Deterministic development adapter; cannot claim physical evidence."""

    robot_id = "orbo-simulated"
    simulated = True
    capabilities = {
        "attention.gesture": Capability(
            "attention.gesture", ("flippers",), 5, ("simulation",), False
        )
    }

    def __init__(self, *, lose_feedback=False):
        self.results = {}
        self.lose_feedback = lose_feedback
        self.dispatches = 0

    async def dispatch(self, action):
        self.dispatches += 1
        result = ActionResult(
            action.command_id,
            "accepted" if self.lose_feedback else "completed",
            "simulation",
        )
        self.results[action.command_id] = result
        return result

    async def cancel(self, command_id):
        result = ActionResult(command_id, "cancelled", "simulation")
        self.results[command_id] = result
        return result

    async def controlled_stop(self, reason):
        for key, result in self.results.items():
            if result.state == "accepted":
                self.results[key] = ActionResult(key, "cancelled", "simulation")

    async def reconcile(self, command_id):
        return self.results.get(command_id, ActionResult(command_id, "unknown", "none"))

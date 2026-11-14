"""Durable, revision-fenced embodied tasks over the existing journal and broker.

A task authorizes a bounded sequence of operations, never a long-lived motor
lease. The coordinator is the only writer; async callbacks must carry revisions.
"""

import asyncio
import json

from .contracts import Rejected, canonical, finite, identity

TERMINAL = {"completed", "cancelled", "failed"}
STATES = TERMINAL | {
    "accepted",
    "working",
    "awaiting_clarification",
    "paused",
    "unknown",
}
KINDS = {"inspect", "find", "compare", "recall", "describe"}
ACTIONS = {"navigate", "capture", "infer", "recall", "compare", "report"}


def _text(value, name, limit=256):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise Rejected(f"invalid {name}")
    return value.strip()


def _strings(values, name, limit=32):
    if not isinstance(values, list) or len(values) > limit:
        raise Rejected(f"invalid {name}")
    return [_text(value, name) for value in values]


class PlaceRegistry:
    """Owner-surveyed names resolve only to explicitly commissioned waypoints."""

    def __init__(self, store):
        self.store = store

    def register(
        self,
        owner,
        environment,
        map_version,
        name,
        *,
        aliases=None,
        region="",
        surface="",
        viewpoints=None,
        accessible=True,
        visibility_limits="",
    ):
        for value, label in (
            (owner, "owner"),
            (environment, "environment"),
            (map_version, "map"),
            (name, "name"),
        ):
            _text(value, label)
        aliases = _strings(aliases or [], "aliases")
        viewpoints = _strings(viewpoints or [], "viewpoints")
        if not viewpoints or type(accessible) is not bool:
            raise Rejected("surveyed viewpoints and explicit access required")
        body = dict(
            aliases=aliases,
            region=region,
            surface=surface,
            viewpoints=viewpoints,
            accessible=accessible,
            visibility_limits=visibility_limits,
        )
        if len(canonical(body)) > 8000:
            raise Rejected("place description too large")
        with self.store.transaction() as db:
            old = db.execute(
                "SELECT id FROM semantic_places WHERE owner=? AND environment=? AND map_version=? AND name=?",
                (owner, environment, map_version, name),
            ).fetchone()
            place_id = old[0] if old else identity()
            db.execute(
                "INSERT INTO semantic_places VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (place_id, owner, environment, map_version, name, canonical(body)),
            )
            self.store.event(
                None,
                "place.registered",
                dict(
                    place_id=place_id,
                    owner=owner,
                    environment=environment,
                    map_version=map_version,
                ),
            )
        return place_id

    def resolve(self, owner, environment, map_version, name):
        name = _text(name, "place").casefold()
        matches = []
        for row in self.store.db.execute(
            "SELECT * FROM semantic_places WHERE owner=? AND environment=? AND map_version=?",
            (owner, environment, map_version),
        ):
            place = {**dict(row), **json.loads(row["body"])}
            del place["body"]
            if name in {
                place["name"].casefold(),
                *(a.casefold() for a in place["aliases"]),
            }:
                matches.append(place)
        if len(matches) != 1:
            raise Rejected("place unknown or ambiguous in this environment/map")
        if not matches[0]["accessible"]:
            raise Rejected("place inaccessible")
        return matches[0]


class TaskService:
    """Persist admission and operation intent before any external side effect."""

    def __init__(self, store):
        self.store = store
        self.places = PlaceRegistry(store)
        self._monotonic_deadlines = {}

    def admit(self, owner, key, request):
        _text(owner, "owner")
        _text(key, "idempotency key")
        if not isinstance(request, dict):
            raise Rejected("task request required")
        request = json.loads(canonical(request))
        required = {
            "kind",
            "environment",
            "map_version",
            "purpose",
            "target",
            "places",
            "allowed_actions",
            "budget",
            "destination",
            "provenance",
        }
        if not required <= request.keys():
            raise Rejected("incomplete task request")
        if request["kind"] not in KINDS:
            raise Rejected("unsupported task kind")
        for name in ("environment", "map_version", "purpose", "target", "destination"):
            _text(request[name], name, 2000 if name == "target" else 256)
        request["places"] = _strings(request["places"], "places")
        actions = _strings(request["allowed_actions"], "actions")
        if not actions or not set(actions) <= ACTIONS:
            raise Rejected("invalid task actions")
        if request["provenance"] not in {"physical", "synthetic", "recorded"}:
            raise Rejected("invalid task provenance")
        budget = request["budget"]
        if not isinstance(budget, dict) or set(budget) != {
            "seconds",
            "observations",
            "provider_calls",
        }:
            raise Rejected(
                "explicit time, observation and provider-call budgets required"
            )
        finite(budget["seconds"], 1, 3600)
        for name in ("observations", "provider_calls"):
            if type(budget[name]) is not int or not 0 <= budget[name] <= 64:
                raise Rejected("invalid count budget")
        if len(canonical(request)) > 16384:
            raise Rejected("task request too large")
        now = self.store.clock.utc()
        with self.store.transaction() as db:
            replay = self.store.replay(owner, "task:" + key, request)
            if replay is not None:
                return self.get(owner, replay["task_id"])
            self.store.permission(owner, "task.execute", request["environment"])
            # Unknown physical outcomes continue holding exclusivity until reconciliation.
            if (
                request["kind"] != "recall"
                and db.execute(
                    "SELECT 1 FROM embodied_tasks WHERE state NOT IN ('completed','cancelled','failed') "
                    "AND json_extract(request,'$.kind') != 'recall'"
                ).fetchone()
            ):
                raise Rejected(
                    "physical task active; revise, cancel or reconcile it first"
                )
            for name in request["places"]:
                self.places.resolve(
                    owner, request["environment"], request["map_version"], name
                )
            task_id, episode_id = identity(), identity()
            db.execute(
                "INSERT INTO episodes VALUES(?,?,?,?,?,?,NULL)",
                (
                    episode_id,
                    owner,
                    "physical_task.v1",
                    request["provenance"],
                    "active",
                    now,
                ),
            )
            self.store.event(
                episode_id,
                "episode.started",
                {"profile": "physical_task.v1", "provenance": request["provenance"]},
            )
            db.execute(
                "INSERT INTO embodied_tasks VALUES(?,?,?,?,?,1,'accepted',?,?,?,?,?,?,'{}')",
                (
                    task_id,
                    episode_id,
                    owner,
                    request["environment"],
                    request["map_version"],
                    canonical(request),
                    canonical(budget),
                    canonical({"observations": 0, "provider_calls": 0}),
                    now,
                    now + budget["seconds"],
                    now,
                ),
            )
            self.store.remember(owner, "task:" + key, request, {"task_id": task_id})
            self.store.event(
                episode_id,
                "task.accepted",
                {
                    "task_id": task_id,
                    "revision": 1,
                    "kind": request["kind"],
                    "environment": request["environment"],
                    "map_version": request["map_version"],
                },
            )
        self._monotonic_deadlines[task_id] = self.store.clock.mono() + budget["seconds"]
        return self.get(owner, task_id)

    def get(self, owner, task_id):
        row = self.store.db.execute(
            "SELECT * FROM embodied_tasks WHERE id=? AND owner=?", (task_id, owner)
        ).fetchone()
        if not row:
            raise Rejected("owned task required")
        task = dict(row)
        for name in ("request", "budget", "used", "result"):
            task[name] = json.loads(task[name])
        return task

    def check_revision(self, owner, task_id, revision, *, working=True):
        task = self.get(owner, task_id)
        if type(revision) is not int or task["revision"] != revision:
            raise Rejected("stale task revision")
        if working:
            if task["state"] not in {"accepted", "working"}:
                raise Rejected("task not executable")
            if self.store.clock.utc() >= task[
                "deadline"
            ] or self.store.clock.mono() >= self._monotonic_deadlines.get(
                task_id, float("-inf")
            ):
                raise Rejected(
                    "task deadline expired or restart requires reconciliation"
                )
            self.store.permission(owner, "task.execute", task["environment"])
        return task

    def transition(self, owner, task_id, revision, state, result=None):
        if state not in STATES:
            raise Rejected("invalid task state")
        with self.store.transaction() as db:
            task = self.check_revision(owner, task_id, revision, working=False)
            if task["state"] in TERMINAL:
                if state == task["state"]:
                    return task
                raise Rejected("terminal task cannot be revived")
            if (
                state == "completed"
                and db.execute(
                    "SELECT 1 FROM task_operations WHERE task_id=? AND state IN ('admitted','working','unknown')",
                    (task_id,),
                ).fetchone()
            ):
                raise Rejected("unresolved operation prevents completion")
            db.execute(
                "UPDATE embodied_tasks SET state=?,updated=?,result=? WHERE id=?",
                (state, self.store.clock.utc(), canonical(result or {}), task_id),
            )
            self.store.event(
                task["episode_id"],
                "task." + state,
                {"task_id": task_id, "revision": revision, "result": result or {}},
            )
        if state in TERMINAL:
            # Existing broker prevents falsely closing an episode with active commands.
            try:
                self.store.end_episode(task["episode_id"], owner, state)
            except Rejected:
                pass
        return self.get(owner, task_id)

    def revise(self, owner, task_id, expected_revision, changes):
        if (
            not isinstance(changes, dict)
            or not set(changes) <= {"target", "places", "reference_id"}
            or not changes
        ):
            raise Rejected("corrections may change target, places or reference only")
        with self.store.transaction() as db:
            task = self.check_revision(owner, task_id, expected_revision, working=False)
            if task["state"] in TERMINAL or task["state"] == "unknown":
                raise Rejected("task cannot be revised before reconciliation")
            self.store.permission(owner, "task.execute", task["environment"])
            if "target" in changes:
                _text(changes["target"], "target", 2000)
            if "reference_id" in changes:
                _text(changes["reference_id"], "reference")
            if "places" in changes:
                for name in _strings(changes["places"], "places"):
                    self.places.resolve(
                        owner, task["environment"], task["map_version"], name
                    )
            request = task["request"] | changes
            revision = expected_revision + 1
            db.execute(
                "UPDATE task_operations SET state='cancelled',result='{}' "
                "WHERE task_id=? AND capability!='navigate' AND state IN ('admitted','working')",
                (task_id,),
            )
            active = db.execute(
                "SELECT 1 FROM task_operations WHERE task_id=? AND state IN ('admitted','working','unknown')",
                (task_id,),
            ).fetchone()
            db.execute(
                "UPDATE embodied_tasks SET request=?,revision=?,state=?,updated=? WHERE id=?",
                (
                    canonical(request),
                    revision,
                    "paused" if active else "accepted",
                    self.store.clock.utc(),
                    task_id,
                ),
            )
            self.store.event(
                task["episode_id"],
                "task.revised",
                {
                    "task_id": task_id,
                    "revision": revision,
                    "changed_fields": sorted(changes),
                },
            )
        return self.get(owner, task_id)

    def reserve_operation(
        self,
        owner,
        task_id,
        revision,
        key,
        capability,
        parameters,
        duration=30,
        *,
        observations=0,
        provider_calls=0,
    ):
        _text(key, "operation key")
        if capability not in ACTIONS:
            raise Rejected("unsupported task capability")
        finite(duration, 0.05, 3600)
        for value in (observations, provider_calls):
            if type(value) is not int or value < 0:
                raise Rejected("invalid budget reservation")
        with self.store.transaction() as db:
            task = self.check_revision(owner, task_id, revision)
            old = db.execute(
                "SELECT * FROM task_operations WHERE task_id=? AND revision=? AND operation_key=?",
                (task_id, revision, key),
            ).fetchone()
            if old:
                if old["capability"] != capability or old["parameters"] != canonical(
                    parameters
                ):
                    raise Rejected("operation key reused with different content")
                return self.operation(owner, old["id"])
            if capability not in task["request"]["allowed_actions"]:
                raise Rejected("capability outside task scope")
            for label, count in (
                ("observations", observations),
                ("provider_calls", provider_calls),
            ):
                task["used"][label] += count
                if task["used"][label] > task["budget"][label]:
                    raise Rejected("task budget exhausted")
            operation_id = identity()
            deadline = min(task["deadline"], self.store.clock.utc() + duration)
            db.execute(
                "INSERT INTO task_operations VALUES(?,?,?,?,?,?,'admitted',?,NULL,'{}')",
                (
                    operation_id,
                    task_id,
                    revision,
                    key,
                    capability,
                    canonical(parameters),
                    deadline,
                ),
            )
            db.execute(
                "UPDATE embodied_tasks SET used=?,state='working',updated=? WHERE id=?",
                (canonical(task["used"]), self.store.clock.utc(), task_id),
            )
            self.store.event(
                task["episode_id"],
                "task.operation_admitted",
                {
                    "task_id": task_id,
                    "revision": revision,
                    "operation_id": operation_id,
                    "capability": capability,
                    "parameters": parameters,
                    "deadline": deadline,
                },
            )
        return self.operation(owner, operation_id)

    def operation(self, owner, operation_id):
        row = self.store.db.execute(
            "SELECT o.* FROM task_operations o JOIN embodied_tasks t ON t.id=o.task_id WHERE o.id=? AND t.owner=?",
            (operation_id, owner),
        ).fetchone()
        if not row:
            raise Rejected("owned operation required")
        op = dict(row)
        op["parameters"], op["result"] = (
            json.loads(op["parameters"]),
            json.loads(op["result"]),
        )
        return op

    def finish_operation(self, owner, operation_id, revision, state, result=None):
        if state not in {"completed", "failed", "cancelled", "unknown"}:
            raise Rejected("invalid operation result")
        with self.store.transaction() as db:
            op = self.operation(owner, operation_id)
            task = self.get(owner, op["task_id"])
            if (
                op["revision"] != revision
                or task["revision"] != revision
                or task["state"] in TERMINAL
            ):
                if op["state"] in {"admitted", "working"}:
                    db.execute(
                        "UPDATE task_operations SET state=?,result='{}' WHERE id=?",
                        (
                            "unknown"
                            if op["capability"] == "navigate"
                            else "cancelled",
                            operation_id,
                        ),
                    )
                return False
            if op["state"] not in {"admitted", "working"}:
                return False
            if self.store.clock.utc() >= op["deadline"] and state == "completed":
                state, result = "unknown", {"reason": "late operation result"}
            db.execute(
                "UPDATE task_operations SET state=?,result=? WHERE id=?",
                (state, canonical(result or {}), operation_id),
            )
            self.store.event(
                task["episode_id"],
                "task.operation_" + state,
                {
                    "task_id": task["id"],
                    "revision": revision,
                    "operation_id": operation_id,
                    "result": result or {},
                },
            )
        return True


class TaskNavigator:
    """Bind registered views to the broker, never directly to ROS or motors."""

    def __init__(self, tasks, broker):
        self.tasks, self.broker = tasks, broker
        self._running = {}

    async def navigate(
        self,
        owner,
        task_id,
        revision,
        place_name,
        operation_key,
        *,
        viewpoint=None,
        duration=30,
    ):
        task = self.tasks.check_revision(owner, task_id, revision)
        place = self.tasks.places.resolve(
            owner, task["environment"], task["map_version"], place_name
        )
        allowed = [
            self.tasks.places.resolve(
                owner, task["environment"], task["map_version"], n
            )["id"]
            for n in task["request"]["places"]
        ]
        if place["id"] not in allowed:
            raise Rejected("place outside task scope")
        viewpoint = viewpoint or place["viewpoints"][0]
        if viewpoint not in place["viewpoints"]:
            raise Rejected("viewpoint outside surveyed registry")
        finite(duration, 0.05, 600)
        duration = min(duration, task["deadline"] - self.tasks.store.clock.utc())
        op = self.tasks.reserve_operation(
            owner,
            task_id,
            revision,
            operation_key,
            "navigate",
            {"place_id": place["id"], "waypoint": viewpoint},
            duration,
        )
        if op["state"] != "admitted" or op["id"] in self._running:
            return op
        # An admitted operation found after restart must not issue movement.
        if task_id not in self.tasks._monotonic_deadlines:
            raise Rejected("recovered navigation requires reconciliation")
        self._running[op["id"]] = asyncio.current_task()
        try:
            epoch = self.broker.acquire(owner, ttl=2)
            with self.tasks.store.transaction() as db:
                db.execute(
                    "UPDATE task_operations SET state='working' WHERE id=?", (op["id"],)
                )
            command = await self.broker.dispatch(
                owner,
                task["episode_id"],
                "task:" + op["id"],
                "navigation.named_goal",
                {"waypoint": viewpoint},
                epoch,
                duration,
                supervisor=lambda: bool(
                    self.tasks.check_revision(owner, task_id, revision)
                ),
            )
            with self.tasks.store.transaction() as db:
                db.execute(
                    "UPDATE task_operations SET command_id=? WHERE id=?",
                    (command["id"], op["id"]),
                )
            state = (
                command["state"]
                if command["state"] in {"completed", "failed", "cancelled"}
                else "unknown"
            )
            self.tasks.finish_operation(
                owner,
                op["id"],
                revision,
                state,
                {
                    "command_id": command["id"],
                    "evidence": command["evidence"],
                    "waypoint": viewpoint,
                },
            )
            if state == "unknown":
                self.tasks.transition(
                    owner, task_id, revision, "unknown", {"operation_id": op["id"]}
                )
        except BaseException as error:
            admitted = self.tasks.store.db.execute(
                "SELECT 1 FROM operations WHERE owner=? AND key=?",
                (owner, "command:task:" + op["id"]),
            ).fetchone()
            self.tasks.finish_operation(
                owner,
                op["id"],
                revision,
                "unknown" if admitted else "failed",
                {
                    "reason": str(error)
                    if isinstance(error, Rejected)
                    else "navigation interrupted or rejected",
                    "error_type": type(error).__name__,
                },
            )
            raise
        finally:
            # The broker persists its command ID before dispatch; recover that link
            # even when dispatch raises or this coroutine is cancelled.
            row = self.tasks.store.db.execute(
                "SELECT result FROM operations WHERE owner=? AND key=?",
                (owner, "command:task:" + op["id"]),
            ).fetchone()
            if row:
                self.tasks.store.db.execute(
                    "UPDATE task_operations SET command_id=? WHERE id=?",
                    (json.loads(row[0])["command_id"], op["id"]),
                )
            self._running.pop(op["id"], None)
        return self.tasks.operation(owner, op["id"])

    async def pause(self, owner, task_id, revision):
        task = self.tasks.check_revision(owner, task_id, revision, working=False)
        self.tasks.transition(owner, task_id, revision, "paused")
        await self._stop(owner, task)
        return self.tasks.get(owner, task_id)

    async def cancel(self, owner, task_id, revision):
        task = self.tasks.check_revision(owner, task_id, revision, working=False)
        self.tasks.transition(owner, task_id, revision, "paused")
        await self._stop(owner, task)
        return self.tasks.transition(
            owner,
            task_id,
            revision,
            "cancelled",
            {"stop": "requested; physical cessation not independently verified"},
        )

    async def _stop(self, owner, task):
        # Stop authority first; do not wait for cloud work or a callback to finish.
        owns_motion = self.tasks.store.db.execute(
            "SELECT 1 FROM commands WHERE episode_id=? AND robot=? "
            "AND state IN ('pending','accepted','executing','cancel_requested','unknown')",
            (task["episode_id"], self.broker.adapter.robot_id),
        ).fetchone()
        if owns_motion:
            await self.broker.stop("task pause/cancel")
        rows = self.tasks.store.db.execute(
            "SELECT id FROM task_operations WHERE task_id=? AND state IN ('admitted','working')",
            (task["id"],),
        ).fetchall()
        pending = []
        for row in rows:
            work = self._running.get(row[0])
            if work and work is not asyncio.current_task():
                work.cancel()
                pending.append(work)
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        with self.tasks.store.transaction() as db:
            db.execute(
                "UPDATE task_operations SET state=CASE WHEN capability='navigate' THEN 'unknown' ELSE 'cancelled' END "
                "WHERE task_id=? AND state IN ('admitted','working')",
                (task["id"],),
            )

    async def reconcile(self, owner, task_id):
        """Observe uncertain commands; never retry navigation or grant authority."""
        task = self.tasks.get(owner, task_id)
        for row in self.tasks.store.db.execute(
            "SELECT id,command_id FROM task_operations WHERE task_id=? AND state='unknown'",
            (task_id,),
        ).fetchall():
            command_id = row["command_id"]
            if command_id is None:
                remembered = self.tasks.store.db.execute(
                    "SELECT result FROM operations WHERE owner=? AND key=?",
                    (owner, "command:task:" + row["id"]),
                ).fetchone()
                command_id = (
                    json.loads(remembered[0])["command_id"] if remembered else None
                )
            if command_id is None:
                continue
            command = await self.broker.reconcile(command_id)
            if command["state"] not in {"completed", "cancelled", "failed", "rejected"}:
                continue
            with self.tasks.store.transaction() as db:
                db.execute(
                    "UPDATE task_operations SET state=?,command_id=?,result=? WHERE id=?",
                    (
                        command["state"],
                        command_id,
                        canonical(
                            {
                                "command_id": command_id,
                                "evidence": command["evidence"],
                                "reconciled": True,
                            }
                        ),
                        row["id"],
                    ),
                )
                self.tasks.store.event(
                    task["episode_id"],
                    "task.operation_reconciled",
                    {
                        "task_id": task_id,
                        "operation_id": row["id"],
                        "state": command["state"],
                    },
                )
        return self.tasks.get(owner, task_id)

    def resume(self, owner, task_id, revision):
        """Explicit owner resume after pause/reconciliation; never replays old work."""
        task = self.tasks.check_revision(owner, task_id, revision, working=False)
        self.tasks.store.permission(owner, "task.execute", task["environment"])
        if task["state"] not in {"paused", "unknown", "awaiting_clarification"}:
            raise Rejected("task is not resumable")
        if self.tasks.store.db.execute(
            "SELECT 1 FROM task_operations WHERE task_id=? AND state IN ('admitted','working','unknown')",
            (task_id,),
        ).fetchone():
            raise Rejected("reconcile operations before resuming")
        remaining = task["deadline"] - self.tasks.store.clock.utc()
        if remaining <= 0:
            raise Rejected("task deadline expired")
        self.tasks._monotonic_deadlines[task_id] = (
            self.tasks.store.clock.mono() + remaining
        )
        return self.tasks.transition(owner, task_id, revision, "accepted")

    async def revise(self, owner, task_id, revision, changes):
        """Fence old work, stop it, and resume only if its outcome is established."""
        task = self.tasks.revise(owner, task_id, revision, changes)
        if task["state"] == "paused":
            await self._stop(owner, task)
            await self.reconcile(owner, task_id)
            try:
                return self.resume(owner, task_id, task["revision"])
            except Rejected:
                # A later explicit reconciliation/resume can proceed after ROS
                # reports the old goal cancelled. Never guess cessation here.
                pass
        return self.tasks.get(owner, task_id)

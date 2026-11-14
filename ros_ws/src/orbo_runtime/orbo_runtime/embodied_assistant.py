"""Bounded task execution joining the existing broker, private vision and memory.

Only this coordinator schedules task effects. Providers propose findings; registered
viewpoints, permissions, revisions and budgets decide which effects are admitted.
"""

import asyncio
import json

from .contracts import Rejected, canonical, identity
from .embodied_tasks import TaskService, TaskNavigator, TERMINAL
from .vision import release_finding


class EmbodiedAssistant:
    def __init__(self, store, broker, memory, observations, vision, *, notify=None):
        self.store, self.memory = store, memory
        self.tasks = TaskService(store)
        self.navigator = TaskNavigator(self.tasks, broker)
        self.observations, self.vision, self.notify = observations, vision, notify
        self.jobs = {}
        self.closing = False
        self.vision.usage_recorder = self.record_usage
        store.db.executescript("""
        CREATE TABLE IF NOT EXISTS embodied_results(
          task_id TEXT NOT NULL REFERENCES embodied_tasks(id), revision INTEGER NOT NULL,
          result TEXT NOT NULL, PRIMARY KEY(task_id,revision));
        """)

    async def submit(self, owner, key, request):
        if self.closing:
            raise Rejected("assistant shutting down")
        task = self.tasks.admit(owner, key, request)
        if task["state"] == "accepted" and task["id"] not in self.jobs:
            self.start(owner, task)
        return task

    def start(self, owner, task):
        if self.closing:
            raise Rejected("assistant shutting down")
        if task["id"] in self.jobs:
            raise Rejected("task already running")
        self.tasks.check_revision(owner, task["id"], task["revision"])
        worker = asyncio.create_task(self.run(owner, task["id"], task["revision"]))
        self.jobs[task["id"]] = worker
        worker.add_done_callback(
            lambda done: self.jobs.pop(task["id"], None)
            if self.jobs.get(task["id"]) is done
            else None
        )

    def record_usage(self, owner, task_id, revision, proof, usage):
        """Account verified usage even when subsequent interpretation is rejected."""
        from .economics import Ledger

        task = self.tasks.get(owner, task_id)
        Ledger(self.store).observe(
            "near",
            owner,
            proof["response_id"],
            "usage",
            {
                "task_id": task_id,
                "revision": revision,
                "model": proof["model"],
                "usage": usage,
                "amount_known": False,
            },
            provenance="fixture"
            if task["request"]["provenance"] != "physical"
            else "live_service",
        )

    async def _effect(
        self,
        owner,
        task,
        key,
        capability,
        body,
        factory,
        *,
        observations=0,
        provider_calls=0,
    ):
        operation = self.tasks.reserve_operation(
            owner,
            task["id"],
            task["revision"],
            key,
            capability,
            body,
            duration=min(120, max(0.05, task["deadline"] - self.store.clock.utc())),
            observations=observations,
            provider_calls=provider_calls,
        )
        if operation["state"] != "admitted":
            raise Rejected(
                "operation already attempted; explicit reconciliation required"
            )
        try:
            async with asyncio.timeout(
                max(0.01, operation["deadline"] - self.store.clock.utc())
            ):
                result = await factory()
            self.tasks.check_revision(owner, task["id"], task["revision"])
            self.tasks.finish_operation(
                owner,
                operation["id"],
                task["revision"],
                "completed",
                {"record_id": result.get("id")} if isinstance(result, dict) else {},
            )
            if self.tasks.operation(owner, operation["id"])["state"] != "completed":
                raise Rejected("operation outcome uncertain")
            if capability in {"capture", "infer", "compare"}:
                fields = (
                    "id",
                    "task_id",
                    "revision",
                    "map_version",
                    "captured_at",
                    "clock_domain",
                    "frame_id",
                    "source_stamp_ns",
                    "image_hash",
                    "quality",
                    "pose",
                    "viewpoint",
                    "calibration_version",
                    "status",
                    "identity",
                    "occluded",
                    "reason",
                    "coverage",
                    "observed_at",
                    "observation_ids",
                    "source_type",
                    "proof",
                    "usage",
                    "baseline_id",
                    "changes",
                )
                self.store.event(
                    task["episode_id"],
                    "task."
                    + {
                        "capture": "observation",
                        "infer": "finding",
                        "compare": "comparison",
                    }[capability],
                    {key: result[key] for key in fields if key in result},
                )
            return result
        except BaseException as exc:
            self.tasks.finish_operation(
                owner,
                operation["id"],
                task["revision"],
                "cancelled" if isinstance(exc, asyncio.CancelledError) else "failed",
            )
            raise

    async def run(self, owner, task_id, revision):
        task = self.tasks.check_revision(owner, task_id, revision)
        request = task["request"]
        pose_provider = getattr(self.observations.source, "pose_provider", None)
        pose_source = getattr(pose_provider, "__self__", pose_provider)
        if hasattr(pose_source, "select_current_view") and request["kind"] != "recall":
            pose_source.select_current_view(
                request["kind"] == "describe" and not request["places"]
            )
        attempt = identity()  # Explicit resume starts a fresh bounded attempt; never replay uncertain effects.
        result = {
            "finding_ids": [],
            "comparison_ids": [],
            "recall_ids": [],
            "coverage": [],
            "partial": False,
            "reason": None,
        }

        def check():
            return self.tasks.check_revision(owner, task_id, revision)

        try:
            if request["kind"] == "recall":
                op = self.tasks.reserve_operation(
                    owner, task_id, revision, attempt + ":recall", "recall", {}
                )
                found = self.memory.recall(
                    owner,
                    task["environment"],
                    reference_id=request.get("reference_id"),
                    description=None
                    if request.get("reference_id")
                    else request["target"],
                )
                result["recall_ids"] = [
                    f["id"] for f in found if f["map_version"] == task["map_version"]
                ]
                self.tasks.finish_operation(owner, op["id"], revision, "completed")
            else:
                views = []
                for name in request["places"]:
                    place = self.tasks.places.resolve(
                        owner, task["environment"], task["map_version"], name
                    )
                    views.extend((name, view) for view in place["viewpoints"])
                if request["kind"] == "describe" and not views:
                    views = [(None, "current")]
                if not views:
                    raise Rejected("registered observation place required")
                remaining = list(dict.fromkeys(views))
                while remaining:
                    check()
                    current = self.tasks.get(owner, task_id)
                    if (
                        current["used"]["observations"]
                        >= current["budget"]["observations"]
                        or current["used"]["provider_calls"]
                        >= current["budget"]["provider_calls"]
                    ):
                        result.update(partial=True, reason="budget_exhausted")
                        break
                    name, viewpoint = remaining.pop(0)
                    step = len(result["coverage"])
                    if name is not None:
                        nav = await self.navigator.navigate(
                            owner,
                            task_id,
                            revision,
                            name,
                            f"{attempt}-view-{step}-navigate",
                            viewpoint=viewpoint,
                            duration=min(
                                120,
                                max(0.05, task["deadline"] - self.store.clock.utc()),
                            ),
                        )
                        check()
                        if nav["state"] != "completed":
                            result.update(partial=True, reason="navigation_incomplete")
                            break
                    capture = await self._effect(
                        owner,
                        task,
                        f"{attempt}-view-{step}-capture",
                        "capture",
                        {"viewpoint": viewpoint},
                        lambda: self.observations.capture(
                            owner,
                            task["environment"],
                            task["map_version"],
                            task_id,
                            revision,
                            viewpoint,
                            check_revision=check,
                        ),
                        observations=1,
                    )
                    result["coverage"].append(viewpoint)
                    if request["kind"] == "compare":
                        if not request.get("baseline_id"):
                            raise Rejected("owner-accepted baseline required")
                        comparison = await self._effect(
                            owner,
                            task,
                            f"{attempt}-view-{step}-compare",
                            "compare",
                            {
                                "observation_id": capture["id"],
                                "baseline_id": request["baseline_id"],
                            },
                            lambda: self.vision.compare(
                                owner,
                                task["environment"],
                                task["map_version"],
                                task_id,
                                revision,
                                request["baseline_id"],
                                capture["id"],
                                check_revision=check,
                            ),
                            provider_calls=1,
                        )
                        result["comparison_ids"].append(comparison["id"])
                        if comparison["status"] == "unknown":
                            result.update(partial=True, reason="comparison_uncertain")
                        break
                    finding = await self._effect(
                        owner,
                        task,
                        f"{attempt}-view-{step}-infer",
                        "infer",
                        {"observation_id": capture["id"]},
                        lambda: self.vision.inspect(
                            owner,
                            task["environment"],
                            task["map_version"],
                            task_id,
                            revision,
                            [capture["id"]],
                            request["target"],
                            reference_id=request.get("reference_id"),
                            allowed_viewpoints=[v for _, v in remaining],
                            check_revision=check,
                        ),
                        provider_calls=1,
                    )
                    result["finding_ids"].append(finding["id"])
                    # A positive exact finding can end a bounded search. A class match
                    # without a reference is useful but never upgraded to exact identity.
                    if finding["status"] == "present" and (
                        not request.get("reference_id")
                        or finding["identity"] == "exact"
                    ):
                        break
                    if request["kind"] == "describe":
                        break
                    proposed = finding.get("next_viewpoint")
                    if proposed:
                        remaining.sort(key=lambda pair: pair[1] != proposed)
                if not result["finding_ids"] and not result["comparison_ids"]:
                    result.update(
                        partial=True, reason=result["reason"] or "no_usable_evidence"
                    )
                elif result["finding_ids"]:
                    observed = [
                        self.memory.get("finding", owner, task["environment"], fid)
                        for fid in result["finding_ids"]
                    ]
                    strongest = max(
                        observed,
                        key=lambda f: {
                            "present": 4,
                            "similar": 3,
                            "unknown": 2,
                            "not_visible": 1,
                        }[f["status"]],
                    )
                    if strongest["status"] in {"unknown", "similar"}:
                        result.update(
                            partial=True, reason="identity_or_visibility_uncertain"
                        )
            check()
            self._save(task_id, revision, result)
            self.tasks.transition(
                owner,
                task_id,
                revision,
                "completed",
                result,
            )
        except asyncio.CancelledError:
            # Cancellation/revision owns state and stop; provider cancellation is not a new outcome.
            raise
        except Exception as exc:
            current = self.tasks.get(owner, task_id)
            if current["revision"] == revision and current["state"] in {
                "accepted",
                "working",
                "unknown",
            }:
                result.update(partial=True, reason=type(exc).__name__)
                self._save(task_id, revision, result)
                uncertain = self.store.db.execute(
                    "SELECT 1 FROM task_operations WHERE task_id=? AND capability='navigate' AND state IN ('working','admitted','unknown')",
                    (task_id,),
                ).fetchone()
                self.tasks.transition(
                    owner,
                    task_id,
                    revision,
                    "unknown" if uncertain else "failed",
                    result,
                )
        finally:
            if self.notify:
                current = self.tasks.get(owner, task_id)
                if current["revision"] == revision and current["state"] in TERMINAL | {
                    "unknown"
                }:
                    try:
                        await self.notify(owner, current)
                    except Exception as exc:
                        self.store.event(
                            current["episode_id"],
                            "task.delivery_pending",
                            {"reason": type(exc).__name__},
                        )
        return self.tasks.get(owner, task_id)

    def _save(self, task_id, revision, result):
        self.store.db.execute(
            "INSERT OR REPLACE INTO embodied_results VALUES(?,?,?)",
            (task_id, revision, canonical(result)),
        )

    def release(self, owner, task, destination):
        self.store.permission(owner, "result.release", destination)
        if "report" not in task["request"]["allowed_actions"]:
            raise Rejected("report outside admitted task scope")
        self.tasks.check_revision(owner, task["id"], task["revision"], working=False)
        if task["request"]["destination"] != destination:
            raise Rejected("result destination does not match task admission")
        row = self.store.db.execute(
            "SELECT result FROM embodied_results WHERE task_id=? AND revision=?",
            (task["id"], task["revision"]),
        ).fetchone()
        if not row:
            return None
        result = json.loads(row[0])
        if result.get("deleted"):
            raise Rejected("source evidence deleted")

        def check():
            return self.tasks.check_revision(
                owner, task["id"], task["revision"], working=False
            )

        findings = [
            release_finding(
                self.memory,
                owner,
                task["environment"],
                fid,
                destination,
                check_revision=check,
            )
            for fid in result["finding_ids"]
        ]
        recalls = []
        if result["recall_ids"]:
            self.store.permission(owner, "memory.read", task["environment"])
        for fid in result["recall_ids"]:
            f = self.memory.get("finding", owner, task["environment"], fid)
            recalls.append(
                {
                    "observed_at": f["observed_at"],
                    "age_seconds": max(0, self.store.clock.utc() - f["observed_at"]),
                    "coverage": f["coverage"],
                    "identity": f["identity"],
                    "current_truth": False,
                }
            )
        comparisons = []
        for cid in result["comparison_ids"]:
            c = self.memory.get("comparison", owner, task["environment"], cid)
            comparisons.append(
                {
                    "status": c["status"],
                    "reason": c["reason"],
                    "changes": c["changes"],
                    "observation_ids": c["observation_ids"],
                    "observed_at": c["observed_at"],
                }
            )
        if findings:
            strongest = max(
                findings,
                key=lambda f: {
                    "present": 4,
                    "similar": 3,
                    "unknown": 2,
                    "not_visible": 1,
                }[f["status"]],
            )
            text = strongest["text"]
        elif comparisons:
            text = {
                "changed": "The comparable inspected view shows a change from the accepted baseline.",
                "unchanged": "No supported change was found in the comparable inspected view.",
                "unknown": "I cannot reliably compare these views.",
            }[comparisons[-1]["status"]]
        elif recalls:
            text = f"The last stored sighting was {recalls[0]['age_seconds']:.0f} seconds ago. This is historical evidence, not its current location."
        else:
            text = "I do not have sufficient permitted evidence to answer this request."
        if result["partial"]:
            text += " The task is partial; unchecked areas remain unknown."
        return {
            "task_id": task["id"],
            "revision": task["revision"],
            "text": text,
            "partial": result["partial"],
            "coverage": result["coverage"],
            "findings": findings,
            "comparisons": comparisons,
            "recall": recalls,
        }

    async def _cancel_worker(self, task_id):
        worker = self.jobs.get(task_id)
        if worker and worker is not asyncio.current_task():
            worker.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(worker), timeout=3)
            except (asyncio.CancelledError, TimeoutError):
                pass

    async def cancel(self, owner, task_id):
        task = self.tasks.get(owner, task_id)
        result = await self.navigator.cancel(owner, task_id, task["revision"])
        await self._cancel_worker(task_id)
        return result

    async def pause(self, owner, task_id):
        task = self.tasks.get(owner, task_id)
        result = await self.navigator.pause(owner, task_id, task["revision"])
        await self._cancel_worker(task_id)
        return result

    async def revise(self, owner, task_id, revision, changes):
        task = await self.navigator.revise(owner, task_id, revision, changes)
        await self._cancel_worker(task_id)
        if task["state"] == "accepted":
            self.start(owner, task)
        return task

    async def close(self):
        self.closing = True
        for task_id, worker in list(self.jobs.items()):
            row = self.store.db.execute(
                "SELECT owner FROM embodied_tasks WHERE id=?", (task_id,)
            ).fetchone()
            if row:
                await self.pause(row["owner"], task_id)

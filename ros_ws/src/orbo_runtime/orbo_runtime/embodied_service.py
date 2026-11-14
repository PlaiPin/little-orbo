"""Owner IPC composition for embodied tasks, evidence and optional Live sessions."""

import asyncio
import hashlib
import json

from .contracts import Rejected, digest
from .embodied_assistant import EmbodiedAssistant
from .evidence_memory import EvidenceMemory
from .observations import LatestFrame, ObservationService
from .providers import credential
from .remote_tasks import Principal, RemoteTasks
from .vision import VerifiedVision


class EmbodiedService:
    def __init__(self, local):
        self.local, self.store, self.owner = local, local.store, local.owner
        self.config = local.config["embodied"]
        self.environment, self.map_version = (
            self.config["environment"],
            self.config["map_version"],
        )
        self.memory = EvidenceMemory(self.store, self.config["image_directory"])
        self.frames = LatestFrame()
        self.pose_source = None
        adapter = local.broker.adapter
        if hasattr(adapter, "node") and self.config.get("camera_enabled", False):
            from .observations import ROSFrameSource, ROSPoseSource

            self.pose_source = getattr(adapter, "pose_source", None) or ROSPoseSource(
                adapter.node,
                map_version=self.map_version,
                waypoints=adapter.navigation.waypoints
                if getattr(adapter, "navigation", None)
                else {},
                topic=self.config.get("pose_topic", "/orbo/amcl_pose"),
            )
            self.frames = ROSFrameSource(
                adapter.node,
                self.pose_source.snapshot,
                frame_id=self.config["camera_frame"],
                calibration_version=self.config["camera_calibration"],
                clock_domain="gazebo"
                if local.config["runtime"]["profile"] == "gazebo"
                else "ros",
                image_topic=self.config.get("image_topic", "/orbo/camera/image_raw"),
                info_topic=self.config.get(
                    "camera_info_topic", "/orbo/camera/camera_info"
                ),
            )
        self.assistant = EmbodiedAssistant(
            self.store,
            local.broker,
            self.memory,
            ObservationService(self.memory, self.frames),
            VerifiedVision(local.reasoner, self.memory),
            notify=self.notify,
        )
        self.live, self.live_job = None, None
        self.live_error = None
        self.remote = None
        remote = self.config.get("remote", {})
        if remote.get("enabled", False):
            principals = []
            for item in remote.get("principals", []):
                token = credential(item["credential"])
                if len(token) < 32:
                    raise Rejected(
                        "remote credential must contain at least 32 characters"
                    )
                principals.append(
                    Principal(
                        item["name"],
                        self.owner,
                        tuple(item["environments"]),
                        hashlib.sha256(token.encode()).hexdigest(),
                        item["expires"],
                    )
                )
            if not principals:
                raise Rejected("remote principals required")
            self.remote = RemoteTasks(
                self.store,
                principals,
                submit=self.submit,
                get=self.assistant.tasks.get,
                cancel=self.assistant.cancel,
                release=self.assistant.release,
            )

    async def submit(self, owner, key, request):
        if (
            request.get("environment") != self.environment
            or request.get("map_version") != self.map_version
        ):
            raise Rejected("task does not match active environment/map")
        request = dict(
            request,
            provenance="synthetic"
            if self.local.broker.adapter.simulated
            else "physical",
        )
        if (
            request.get("kind") == "describe"
            and not request.get("places")
            and self.pose_source
        ):
            self.pose_source.select_current_view(True)
        elif self.pose_source:
            self.pose_source.select_current_view(False)
        return await self.assistant.submit(owner, key, request)

    def backend(self, destination="local", active_task_id=None):
        from .embodied_conversation import ConversationBackend

        return ConversationBackend(
            self.assistant,
            self.local.reasoner,
            self.owner,
            self.environment,
            self.map_version,
            destination=destination,
            active_task_id=active_task_id,
            provenance="synthetic"
            if self.local.broker.adapter.simulated
            else "physical",
            usage_provenance="live_service",
            timeout=self.local.config.get("near", {}).get("timeout_seconds", 45),
        )

    async def notify(self, owner, task):
        if (
            task["request"]["destination"] == "openai"
            and self.live
            and self.live.running
        ):
            from .live_voice import ReleasedUpdate

            release = self.assistant.release(owner, task, "openai")
            if release:
                await self.live.publish(
                    ReleasedUpdate(release["text"], task["id"], task["revision"])
                )

    async def call(self, method, args):
        tasks = self.assistant.tasks
        if method == "task.evidence":
            # Owner-local diagnostic metadata only: never raw images, prompts,
            # model output or verification payloads. Existing records lacking
            # explicit provider provenance cannot establish live qualification.
            from .providers import NearReasoner

            task = tasks.get(self.owner, args["task_id"])
            self.store.permission(self.owner, "memory.read", task["environment"])
            rows = self.store.db.execute(
                "SELECT body FROM evidence_records WHERE owner=? AND environment=? "
                "AND kind IN ('finding','comparison') "
                "AND json_extract(body,'$.task_id')=? "
                "AND json_extract(body,'$.revision')=? ORDER BY created LIMIT 101",
                (self.owner, task["environment"], task["id"], task["revision"]),
            ).fetchall()
            evidence = []
            for row in rows[:100]:
                body = json.loads(row["body"])
                proof = body.get("proof", {})
                verification = proof.get("verification_sha256")
                verified = (
                    proof.get("provider") == "near"
                    and proof.get("provenance") == "live_service"
                    and proof.get("e2ee") is True
                    and proof.get("signature_kind") == "provider_tee"
                    and isinstance(proof.get("response_id"), str)
                    and bool(proof["response_id"])
                    and isinstance(proof.get("model"), str)
                    and bool(proof["model"])
                    and isinstance(verification, str)
                    and len(verification) == 64
                    and all(c in "0123456789abcdef" for c in verification)
                )
                evidence.append({
                    "record_id": body["id"], "task_revision": body["revision"],
                    "model": proof.get("model"), "e2ee": proof.get("e2ee", False),
                    "signature_kind": proof.get("signature_kind"),
                    "response_id": proof.get("response_id"),
                    "proof_sha256": digest(proof),
                    "live_verified": verified,
                })
            configured = self.local.config.get("near", {}).get("enabled", False)
            live_adapter = (
                isinstance(self.local.reasoner, NearReasoner)
                and self.local.reasoner.provenance == "live_service"
            )
            count = sum(item["live_verified"] for item in evidence)
            return {
                "task_id": task["id"], "revision": task["revision"],
                "runtime_near_enabled": configured,
                "live_service": bool(configured and live_adapter and count),
                "verified_response_count": count, "evidence": evidence,
                "truncated": len(rows) > 100,
                "verification": "recorded_at_inference_not_reverified",
            }
        if method == "place.register":
            return tasks.places.register(
                self.owner, self.environment, self.map_version, **args
            )
        if method == "task.submit":
            request = dict(args["request"], destination="local")
            request.setdefault("environment", self.environment)
            request.setdefault("map_version", self.map_version)
            return await self.submit(self.owner, args["key"], request)
        if method == "task.delegate":
            from .live_voice import DelegationContext

            body = args.get("context")
            if not isinstance(body, dict) or set(body) - {
                "session_id", "delegation_id", "offset_ms", "fragments",
                "task_id", "revision", "complete_turn",
            }:
                raise Rejected("bounded Live delegation context required")
            fragments = body.get("fragments")
            if (
                not isinstance(fragments, list) or len(fragments) > 256
                or any(not isinstance(item, dict) for item in fragments)
                or type(body.get("offset_ms")) is not int
                or not 0 <= body["offset_ms"] <= 3_600_000
            ):
                raise Rejected("invalid Live delegation fragments or offset")
            context = DelegationContext(
                **dict(body, fragments=tuple(fragments), complete_turn=False)
            )
            result = await self.backend("openai")(context)
            return {
                "text": result.text, "task_id": result.task_id,
                "revision": result.revision, "kind": result.kind,
            }
        if method == "task.text":
            destination = args.get("destination", "local")
            if destination not in {"local", "openai"}:
                raise Rejected("owner text destination must be local or openai")
            backend = self.backend(destination, active_task_id=args.get("task_id"))
            result = await backend.text(args["text"], args["key"])
            return {
                "text": result.text,
                "task_id": result.task_id,
                "revision": result.revision,
                "kind": result.kind,
            }
        if method == "task.metrics":
            from .embodied_metrics import report

            return report(
                self.store,
                self.owner,
                environment=self.environment,
                task_ids=args.get("task_ids"),
                max_tasks=args.get("max_tasks", 100),
            )
        if method == "task.get":
            return tasks.get(self.owner, args["task_id"])
        if method == "task.result":
            task = tasks.get(self.owner, args["task_id"])
            released = self.assistant.release(
                self.owner, task, task["request"]["destination"]
            )
            if released:
                self.store.event(
                    task["episode_id"],
                    "task.result_released",
                    {
                        "task_id": task["id"],
                        "revision": task["revision"],
                        "destination": task["request"]["destination"],
                        "text": released["text"],
                        "state": "selected_for_owner_response",
                        "played": False,
                    },
                )
            return released
        if method == "task.revise":
            return await self.assistant.revise(
                self.owner, args["task_id"], args["revision"], args["changes"]
            )
        if method in {"task.pause", "task.cancel"}:
            return await getattr(self.assistant, method.split(".")[1])(
                self.owner, args["task_id"]
            )
        if method == "task.reconcile":
            return await self.assistant.navigator.reconcile(self.owner, args["task_id"])
        if method == "task.resume":
            task = self.assistant.navigator.resume(
                self.owner, args["task_id"], args["revision"]
            )
            self.assistant.start(self.owner, task)
            return task
        if method == "memory.reference":
            return self.memory.reference(self.owner, self.environment, **args)
        if method == "memory.baseline":
            return self.memory.accept_baseline(self.owner, self.environment, **args)
        if method == "memory.recall":
            return self.memory.recall(self.owner, self.environment, **args)
        if method == "memory.delete_record":
            removed = self.memory.delete_record(
                args["kind"], self.owner, self.environment, args["record_id"]
            )
            return {
                "deleted_record_ids": removed,
                "delivered_recipient_copies": "not_recalled",
            }
        if method == "memory.delete_observation":
            self.memory.delete_observation(
                self.owner, self.environment, args["observation_id"]
            )
            return {"deleted": True, "delivered_recipient_copies": "not_recalled"}
        if method == "evaluation.annotate":
            from .embodied_evaluation import validate_label

            task = tasks.get(self.owner, args["task_id"])
            label = validate_label(args["label"])
            event_id = self.store.event(
                task["episode_id"], "evaluation.object_location", label
            )
            return {"event_id": event_id}
        if method == "live.start":
            if not self.local.config.get("live", {}).get("enabled", False):
                raise Rejected("Live profile not enabled")
            if self.live_job and not self.live_job.done():
                raise Rejected("Live session already active")
            from .live_voice import DuplexAudio, LiveBridge, LiveConfig, LiveWebSocket

            config = self.local.config["live"]
            episode = args.get("episode_id") or self.store.episode(
                self.owner,
                provenance="synthetic"
                if self.local.broker.adapter.simulated
                else "physical",
            )
            self.store.require_episode(episode, self.owner)
            audio = DuplexAudio(
                sample_rate=config.get("sample_rate", 24000),
                input_device=config.get("input_device"),
                output_device=config.get("output_device"),
                aec_qualified=config.get("aec_qualified", False),
            )
            self.live = LiveBridge(
                self.store,
                self.owner,
                episode,
                self.backend("openai", args.get("task_id")),
                LiveWebSocket(),
                audio,
                config=LiveConfig(
                    model=config.get("model", "gpt-live-1"),
                    voice=config.get("voice", "marin"),
                    sample_rate=config.get("sample_rate", 24000),
                    idle_seconds=config.get("idle_seconds", 60),
                    max_session_seconds=config.get("max_session_seconds", 900),
                    account=config.get("account", "owner"),
                ),
                current_revision=lambda task_id: tasks.get(self.owner, task_id)[
                    "revision"
                ],
            )
            self.live_error = None
            bridge = self.live

            async def run():
                try:
                    await bridge.run(wake="local")
                except Exception as exc:
                    self.live_error = type(exc).__name__
                    self.store.event(
                        episode, "live.session_failed", {"reason": self.live_error}
                    )

            self.live_job = asyncio.create_task(run())
            return {
                "session_id": self.live.session_id,
                "episode_id": episode,
                "state": "starting",
            }
        if method == "live.status":
            return {
                "session_id": self.live.session_id if self.live else None,
                "running": bool(self.live and self.live.running),
                "error": self.live_error,
            }
        if method == "live.interrupt":
            if self.live:
                self.live.interrupt_speech()
            return {"speech_interrupted": True, "task_cancelled": False}
        if method == "live.close":
            if self.live:
                await self.live.close()
            return {"session_close_requested": True, "task_cancelled": False}
        raise Rejected("unknown embodied operation")

    async def close(self):
        await self.assistant.close()
        if self.live:
            await self.live.close()
            await self.live.wait_backend()
        if self.live_job:
            await asyncio.gather(self.live_job, return_exceptions=True)

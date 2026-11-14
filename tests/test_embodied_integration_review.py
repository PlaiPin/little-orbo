"""Independent review of natural-language, durable execution and release seams."""

import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from orbo_runtime.broker import FakeRobot
from orbo_runtime.cli import LocalService
from orbo_runtime.contracts import Capability, Rejected, digest
from orbo_runtime.embodied_conversation import ConversationBackend
from orbo_runtime.observations import RGBFrame
from orbo_runtime.providers import VerifiedReply
from orbo_runtime.remote_tasks import Principal, RemoteTasks


class ReviewRobot(FakeRobot):
    capabilities = {
        "navigation.named_goal": Capability(
            "navigation.named_goal", ("wheels",), 600, ("simulation",), False
        ),
    }
    viewpoint = "current"

    async def dispatch(self, action):
        self.viewpoint = action.parameters["waypoint"]
        return await super().dispatch(action)


class ReviewFrames:
    def __init__(self, store, robot):
        self.store, self.robot = store, robot

    def snapshot(self):
        clock = self.store.clock
        return RGBFrame(
            bytes([90, 120, 150]) * 32 * 32,
            32,
            32,
            clock.utc(),
            clock.mono(),
            "camera",
            "fixture",
            1,
            "map",
            "calibration",
            self.robot.viewpoint,
            {"frame_id": "map", "x": 0.0, "y": 0.0, "yaw": 0.0},
            clock.mono(),
        )


class ReviewReasoner:
    model = "fixture"

    def __init__(self):
        self.calls = []
        self.action = "submit"
        self.kind = "inspect"
        self.vision_entered = None
        self.vision_resume = None

    async def complete(self, messages, tools):
        self.calls.append(messages)
        payload = messages[1]["content"]
        if isinstance(payload, str):
            control = self.action in {"cancel", "pause", "status"}
            body = {
                "action": self.action,
                "complete_request": True,
                "kind": None if control else self.kind,
                "target": None if control else "wrench",
                "places": [] if control or self.kind == "describe" else ["shelf"],
                "reference_id": None,
                "baseline_id": None,
                "identity": "unspecified" if control else "category",
                "clarification": None,
            }
        else:
            if self.vision_entered:
                self.vision_entered.set()
                await self.vision_resume.wait()
            context = json.loads(payload[0]["text"])
            body = {
                "task_id": context["task_id"],
                "revision": context["revision"],
                "status": "present",
                "identity": "unconfirmed",
                "observation_ids": context["required_json"]["observation_ids"],
                "occluded": False,
                "reason": "clear_view",
                "next_viewpoint": None,
                "matched_attributes": [],
            }
        message = {"content": json.dumps(body)}
        return VerifiedReply(
            message,
            {
                "provider": "near",
                "model": self.model,
                "e2ee": True,
                "signature_kind": "provider_tee",
                "message_digest": digest(message),
                "response_id": f"review-{len(self.calls)}",
            },
            {"total_tokens": 20},
        )


@pytest.fixture
def service(tmp_path, monkeypatch):
    from orbo_runtime import cli

    monkeypatch.setattr(cli, "FakeRobot", ReviewRobot)
    local = LocalService(
        {
            "runtime": {
                "owner": "owner",
                "profile": "simulated",
                "database": str(tmp_path / "review.db"),
            },
            "embodied": {
                "enabled": True,
                "environment": "lab",
                "map_version": "map",
                "image_directory": str(tmp_path / "images"),
            },
        }
    )
    reasoner = ReviewReasoner()
    local.reasoner = reasoner
    local.embodied.assistant.vision.reasoner = reasoner
    local.embodied.assistant.observations.source = ReviewFrames(
        local.store, local.broker.adapter
    )
    for scope, destination in [
        ("task.execute", "lab"),
        ("robot.command", local.broker.adapter.robot_id),
        ("observation.capture", "lab"),
        ("observation.retain", "lab"),
        ("observation.infer", "lab"),
        ("cloud.reasoning", "near"),
        ("memory.read", "lab"),
        ("memory.write", "lab"),
        ("result.release", "local"),
        ("result.release", "openai"),
        ("result.release", "remote:alice"),
        ("remote.task", "alice"),
        ("remote.task", "bob"),
    ]:
        local.store.grant(
            "owner", scope, destination, True, local.store.clock.utc() + 3600
        )
    local.embodied.assistant.tasks.places.register(
        "owner", "lab", "map", "shelf", viewpoints=["front"]
    )
    yield local, reasoner
    local.store.close()


async def settle(local, task_id):
    job = local.embodied.assistant.jobs.get(task_id)
    if job:
        await job
    return local.embodied.assistant.tasks.get("owner", task_id)


async def test_natural_owner_ipc_reaches_evidence_linked_result_once(service):
    local, reasoner = service
    request = {
        "method": "task.text",
        "args": {"text": "Check for a wrench on the shelf", "key": "one"},
    }
    accepted = await local.call(request)
    assert accepted["kind"] == "progress"
    completed = await settle(local, accepted["task_id"])
    assert completed["state"] == "completed"
    result = await local.call(
        {"method": "task.result", "args": {"task_id": completed["id"]}}
    )
    assert result["findings"][0]["observation_ids"]
    assert "unconfirmed" in result["text"]
    assert local.broker.adapter.dispatches == 1
    assert await local.call(request) == accepted
    assert local.broker.adapter.dispatches == 1 and len(reasoner.calls) == 2
    assert (
        local.store.db.execute(
            "SELECT count(*) FROM records WHERE provider='near' AND kind='usage'"
        ).fetchone()[0]
        == 2
    )


async def test_natural_social_description_uses_current_view_without_navigation(service):
    local, reasoner = service
    reasoner.kind = "describe"
    reply = await local.call(
        {
            "method": "task.text",
            "args": {"text": "Describe what is in front of you", "key": "social"},
        }
    )
    task = await settle(local, reply["task_id"])
    assert task["state"] == "completed"
    assert local.broker.adapter.dispatches == 0
    assert task["result"]["coverage"] == ["current"]


async def test_live_notify_only_announces_completed_current_revision(service):
    local, reasoner = service
    received = []

    async def publish(update):
        task = local.embodied.assistant.tasks.get("owner", update.task_id)
        assert task["revision"] == update.revision and task["state"] == "completed"
        received.append(update)

    local.embodied.live = SimpleNamespace(running=True, publish=publish)
    backend = local.embodied.backend("openai")
    reasoner.vision_entered, reasoner.vision_resume = asyncio.Event(), asyncio.Event()
    first = await backend.text("Check shelf", "live-one")
    await reasoner.vision_entered.wait()
    assert not received
    reasoner.action = "revise"
    revised = await backend.text("Check shelf again instead", "live-two")
    assert revised.revision == 2
    reasoner.vision_resume.set()
    task = await settle(local, revised.task_id)
    assert task["state"] == "completed"
    assert len(received) == 1 and received[0].revision == 2
    assert received[0].task_id == first.task_id


async def test_remote_real_pipeline_results_are_bound_to_requester_and_revocation(
    service,
):
    local, _ = service
    tokens = {"alice": "a" * 40, "bob": "b" * 40}
    principals = [
        Principal(
            name,
            "owner",
            ("lab",),
            hashlib.sha256(token.encode()).hexdigest(),
            local.store.clock.utc() + 3600,
        )
        for name, token in tokens.items()
    ]
    assistant = local.embodied.assistant
    gateway = RemoteTasks(
        local.store,
        principals,
        submit=local.embodied.submit,
        get=assistant.tasks.get,
        cancel=assistant.cancel,
        release=assistant.release,
    )

    def request(name, method, args):
        return {
            "principal": name,
            "token": tokens[name],
            "method": method,
            "args": args,
        }

    task = await gateway.call(
        request(
            "alice",
            "task.submit",
            {
                "key": "remote",
                "request": {
                    "kind": "inspect",
                    "target": "wrench",
                    "places": ["shelf"],
                    "environment": "lab",
                    "map_version": "map",
                    "purpose": "inspection",
                    "allowed_actions": ["navigate", "capture", "infer", "report"],
                    "budget": {"seconds": 300, "observations": 2, "provider_calls": 2},
                    "destination": "public",
                },
            },
        )
    )
    completed = await settle(local, task["task_id"])
    assert completed["request"]["destination"] == "remote:alice"
    with pytest.raises(Rejected):
        await gateway.call(request("bob", "task.get", {"task_id": completed["id"]}))
    result = await gateway.call(
        request("alice", "task.get", {"task_id": completed["id"]})
    )
    assert result["result"]["findings"][0]["status"] == "present"
    assert "request" not in result and "image" not in json.dumps(result)
    assert result["acknowledged"] is False
    local.store.grant(
        "owner", "result.release", "remote:alice", False, local.store.clock.utc() + 60
    )
    with pytest.raises(Rejected):
        await gateway.call(request("alice", "task.get", {"task_id": completed["id"]}))


def test_remote_principal_rejects_nonfinite_expiry():
    for expiry in [float("nan"), float("inf"), float("-inf")]:
        with pytest.raises(Rejected):
            Principal(
                "alice",
                "owner",
                ("lab",),
                hashlib.sha256(b"a" * 40).hexdigest(),
                expiry,
            )


async def test_text_idempotency_includes_explicit_task_binding(service):
    local, reasoner = service
    first = await local.embodied.backend().text("check shelf", "first")
    await settle(local, first.task_id)
    second = await local.embodied.backend().text("check shelf", "second")
    await settle(local, second.task_id)
    reasoner.action = "status"
    bound_first = local.embodied.backend(active_task_id=first.task_id)
    await bound_first.text("What is the task state?", "status")
    bound_second = local.embodied.backend(active_task_id=second.task_id)
    with pytest.raises(Rejected, match="different context"):
        await bound_second.text("What is the task state?", "status")


async def test_deletion_scrubs_interpreter_reference_derivatives(service):
    local, _ = service
    backend = ConversationBackend(
        local.embodied.assistant, local.reasoner, "owner", "lab", "map"
    )
    reference = local.embodied.memory.reference(
        "owner", "lab", "private reference with engraved name"
    )
    local.store.db.execute(
        "INSERT INTO conversation_requests VALUES(?,?,?,'resolved',?,?,?)",
        (
            "owner",
            "saved",
            "hash",
            json.dumps(
                {
                    "reference_id": reference["id"],
                    "target": "private reference with engraved name",
                }
            ),
            json.dumps(
                {
                    "text": "accepted",
                    "task_id": None,
                    "revision": None,
                    "kind": "progress",
                }
            ),
            local.store.clock.utc(),
        ),
    )
    local.embodied.memory.delete_record("reference", "owner", "lab", reference["id"])
    row = local.store.db.execute(
        "SELECT state,proposal FROM conversation_requests WHERE key='saved'"
    ).fetchone()
    assert "private reference" not in (row["proposal"] or "")
    assert row["state"] != "resolved"
    assert backend.latest_task is None


async def test_shutdown_fences_pending_voice_admission_before_effects(service):
    local, reasoner = service
    entered, resume = asyncio.Event(), asyncio.Event()
    complete = reasoner.complete

    async def delayed(messages, tools):
        entered.set()
        await resume.wait()
        return await complete(messages, tools)

    reasoner.complete = delayed
    backend = local.embodied.backend("openai")
    pending = asyncio.create_task(backend.text("Check shelf", "closing"))
    await entered.wait()
    await local.embodied.assistant.close()
    resume.set()
    with pytest.raises(Rejected, match="clos|shut"):
        await pending
    assert (
        local.store.db.execute("SELECT count(*) FROM embodied_tasks").fetchone()[0] == 0
    )
    assert local.broker.adapter.dispatches == 0


async def test_task_export_carries_voice_delivery_with_explicit_audio_attribution_gap(
    service,
):
    from orbo_runtime.live_voice import LiveBridge, ReleasedUpdate
    from orbo_runtime.traces import Exports

    local, _ = service
    accepted = await local.embodied.backend("openai").text(
        "check shelf", "voice-export"
    )
    task = await settle(local, accepted.task_id)
    episode = local.store.episode("owner", provenance="synthetic")
    sent = []

    async def send(event):
        sent.append(event)

    audio = SimpleNamespace(
        sample_rate=24000,
        generated_frames=24000,
        played_frames=12000,
        resume=lambda: None,
        interrupt=lambda: None,
    )
    bridge = LiveBridge(
        local.store,
        "owner",
        episode,
        lambda ctx: None,
        SimpleNamespace(send=send),
        audio,
        current_revision=lambda tid: local.embodied.assistant.tasks.get("owner", tid)[
            "revision"
        ],
    )
    local.store.db.execute(
        "INSERT INTO live_sessions VALUES(?,?,?,?,?,?,?,?,?)",
        (
            bridge.session_id,
            "owner",
            episode,
            "fixture-live",
            "active",
            1,
            0,
            local.store.clock.utc(),
            local.store.boot,
        ),
    )
    for scope, destination in [("cloud.voice", "openai"), ("speech.play", "local")]:
        local.store.grant(
            "owner", scope, destination, True, local.store.clock.utc() + 100
        )
    bridge.started.set()
    release = local.embodied.assistant.release("owner", task, "openai")
    delivery = await bridge.publish(
        ReleasedUpdate(release["text"], task["id"], task["revision"])
    )
    await bridge.handle(
        {"type": "session.commentary.appended", "client_event_id": delivery}
    )
    bridge.interrupt_speech()
    bridge._session_audio_evidence()
    exported = Exports(local.store).prepare(
        "owner", task["episode_id"], "embodied_process", "local-evaluator"
    )
    voice = [
        event["payload"]
        for event in exported["package"]["events"]
        if event["kind"] == "task.voice_delivery"
    ]
    assert [event["state"] for event in voice] == [
        "pending",
        "submitted",
        "context_accepted",
        "interrupted",
    ]
    assert all(event["spoken_result"] == "unconfirmed" for event in voice)
    frames = [
        event["payload"]
        for event in exported["package"]["events"]
        if event["kind"] == "task.voice_audio_evidence"
    ]
    assert frames[0]["attribution"] == "uncorrelated_session_totals"
    assert (
        frames[0]["generated_frames"] == 24000 and frames[0]["played_frames"] == 12000
    )
    assert frames[0]["acoustic_audibility_verified"] is False
    assert "no_audio_to_task" in frames[0]["gap"]
    assert exported["package"]["episode"]["provenance"] == "synthetic"

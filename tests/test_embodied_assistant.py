import json
from types import SimpleNamespace

import pytest

from orbo_runtime.broker import Broker, FakeRobot
from orbo_runtime.contracts import Capability, ManualClock, Rejected, digest
from orbo_runtime.embodied_assistant import EmbodiedAssistant
from orbo_runtime.evidence_memory import EvidenceMemory
from orbo_runtime.observations import ObservationService, RGBFrame
from orbo_runtime.providers import VerifiedReply
from orbo_runtime.storage import Store
from orbo_runtime.vision import VerifiedVision


class Robot(FakeRobot):
    capabilities = {
        "navigation.named_goal": Capability(
            "navigation.named_goal", ("wheels",), 600, ("simulation",), False
        )
    }
    viewpoint = "current"

    async def dispatch(self, action):
        self.viewpoint = action.parameters["waypoint"]
        return await super().dispatch(action)


class Frames:
    def __init__(self, store, robot):
        self.store, self.robot = store, robot
        self.stale = False

    def snapshot(self):
        clock = self.store.clock
        return RGBFrame(
            bytes([80, 100, 140]) * 32 * 32,
            32,
            32,
            clock.utc() - (5 if self.stale else 0),
            clock.mono(),
            "camera",
            "fixture",
            1,
            "map",
            "fixture-calibration",
            self.robot.viewpoint,
            {"x": 0.0, "y": 0.0, "yaw": 0.0, "frame_id": "map"},
            clock.mono(),
        )


class Reasoner:
    def __init__(self, statuses=("present",)):
        self.statuses, self.calls, self.verified = list(statuses), [], True

    async def complete(self, messages, tools):
        self.calls.append(messages)
        context = json.loads(messages[1]["content"][0]["text"])
        if "required_json" in context:
            status = (
                self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            )
            body = {
                "task_id": context["task_id"],
                "revision": context["revision"],
                "status": status,
                "identity": "visual_similarity"
                if status == "similar"
                else "unconfirmed"
                if status in ("present", "unknown")
                else "not_applicable",
                "observation_ids": context["required_json"]["observation_ids"],
                "occluded": status == "unknown",
                "reason": "occluded" if status == "unknown" else "clear_view",
                "next_viewpoint": None,
                "matched_attributes": [],
            }
        else:
            body = {
                "task_id": context["task_id"],
                "revision": context["revision"],
                "status": "unchanged",
                "reason": "clear_view",
                "changes": [],
            }
        if isinstance(messages[1]["content"], list):
            context = json.loads(messages[1]["content"][0]["text"])
            if "visible_details" in context.get("required_json", {}):
                body["visible_details"] = [
                    {"item": "shirt", "color": "blue", "count": "one"}
                ]
        message = {"content": json.dumps(body)}
        return VerifiedReply(
            message,
            {
                "provider": "near",
                "model": "fixture",
                "e2ee": True,
                "signature_kind": "provider_tee" if self.verified else "invalid",
                "response_id": f"fixture-{len(self.calls)}",
                "message_digest": digest(message),
            },
            {},
        )


@pytest.fixture
def system(tmp_path):
    store = Store(tmp_path / "state.db", ManualClock())
    robot = Robot()
    memory = EvidenceMemory(store, tmp_path / "images")
    source = Frames(store, robot)
    reasoner = Reasoner()
    assistant = EmbodiedAssistant(
        store,
        Broker(store, robot),
        memory,
        ObservationService(memory, source),
        VerifiedVision(reasoner, memory),
    )
    for scope, dest in [
        ("task.execute", "lab"),
        ("robot.command", robot.robot_id),
        ("observation.capture", "lab"),
        ("observation.retain", "lab"),
        ("observation.infer", "lab"),
        ("cloud.reasoning", "near"),
        ("memory.read", "lab"),
        ("memory.write", "lab"),
        ("result.release", "local"),
    ]:
        store.grant("owner", scope, dest, True, store.clock.utc() + 3600)
    assistant.tasks.places.register(
        "owner", "lab", "map", "shelf", viewpoints=["front", "side"]
    )
    request = {
        "kind": "inspect",
        "environment": "lab",
        "map_version": "map",
        "purpose": "inspect object",
        "target": "red wrench",
        "places": ["shelf"],
        "allowed_actions": [
            "navigate",
            "capture",
            "infer",
            "compare",
            "recall",
            "report",
        ],
        "budget": {"seconds": 300, "observations": 3, "provider_calls": 3},
        "destination": "local",
        "provenance": "synthetic",
    }
    yield store, assistant, robot, source, reasoner, request
    store.close()


async def execute(assistant, request, key="one"):
    task = await assistant.submit("owner", key, request)
    worker = assistant.jobs.get(task["id"])
    if worker:
        await worker
    return assistant.tasks.get("owner", task["id"])


async def test_live_evidence_does_not_promote_fixture_records_after_provider_enable(system):
    from orbo_runtime.embodied_service import EmbodiedService
    from orbo_runtime.providers import NearReasoner

    store, assistant, robot, source, reasoner, request = system
    task = await execute(assistant, request)
    # Model a newly enabled provider without contacting it. Existing fixture
    # findings must stay fixtures even when the active runtime changes.
    provider = NearReasoner.__new__(NearReasoner)
    provider.provenance = "live_service"
    service = EmbodiedService.__new__(EmbodiedService)
    service.assistant, service.store, service.owner = assistant, store, "owner"
    service.local = SimpleNamespace(config={"near": {"enabled": True}}, reasoner=provider)
    report = await service.call("task.evidence", {"task_id": task["id"]})
    assert report["runtime_near_enabled"] is True
    assert report["verified_response_count"] == 0 and report["live_service"] is False
    assert report["evidence"] and not report["truncated"]
    assert "red wrench" not in json.dumps(report)
    assert "message_digest" not in json.dumps(report)
    store.grant("owner", "memory.read", "lab", False, store.clock.utc() + 30)
    with pytest.raises(Rejected):
        await service.call("task.evidence", {"task_id": task["id"]})


async def test_provider_accounting_is_independent_of_simulated_robot(system):
    store, assistant, robot, source, reasoner, request = system
    task = await execute(assistant, request)
    assert task["request"]["provenance"] == "synthetic"
    assistant.record_usage("owner", task["id"], task["revision"], {
        "model": "test-model", "response_id": "live-request-fixture-for-test",
        "provenance": "live_service",
    }, {"total_tokens": 17})
    row = store.db.execute(
        "SELECT body FROM records WHERE external_id='live-request-fixture-for-test'"
    ).fetchone()
    assert json.loads(row[0])["provenance"] == "live_service"
    original = store.db.execute(
        "SELECT body FROM records WHERE external_id='fixture-1'"
    ).fetchone()
    assert json.loads(original[0])["provenance"] == "fixture"


async def test_inspect_recall_compare_and_duplicate_are_integrated(system):
    store, assistant, robot, _, reasoner, request = system
    task = await execute(assistant, request)
    assert task["state"] == "completed", task
    release = assistant.release("owner", task, "local")
    assert release["findings"][0]["identity"] == "unconfirmed"
    assert "Exact identity is unconfirmed" in release["text"]
    assert robot.dispatches == 1
    await execute(assistant, request)
    assert robot.dispatches == 1 and len(reasoner.calls) == 1
    store.clock.advance(30)
    recall = await execute(assistant, request | {"kind": "recall"}, "recall")
    recalled = assistant.release("owner", recall, "local")
    assert (
        recalled["recall"][0]["age_seconds"] == 30
        and not recalled["recall"][0]["current_truth"]
    )
    baseline = assistant.memory.accept_baseline(
        "owner", "lab", release["findings"][0]["finding_id"], name="accepted shelf"
    )
    compare = await execute(
        assistant,
        request | {"kind": "compare", "baseline_id": baseline["id"]},
        "compare",
    )
    assert (
        assistant.release("owner", compare, "local")["comparisons"][0]["status"]
        == "unchanged"
    )


async def test_bounded_search_partial_and_no_navigation_description(system):
    _, assistant, robot, _, reasoner, request = system
    reasoner.statuses = ["unknown", "not_visible"]
    task = await execute(
        assistant,
        request
        | {
            "kind": "find",
            "budget": {"seconds": 300, "observations": 1, "provider_calls": 1},
        },
    )
    release = assistant.release("owner", task, "local")
    assert release["partial"] and release["coverage"] == ["front"]
    assert robot.dispatches == 1 and len(reasoner.calls) == 1
    robot.viewpoint = "current"
    reasoner.statuses = ["present"]
    task = await execute(
        assistant, request | {"kind": "describe", "places": []}, "social"
    )
    assert task["state"] == "completed" and robot.dispatches == 1


async def test_stale_capture_and_failed_verification_never_release_facts(system):
    _, assistant, _, source, reasoner, request = system
    source.stale = True
    task = await execute(assistant, request)
    release = assistant.release("owner", task, "local")
    assert task["state"] == "failed" and not reasoner.calls and not release["findings"]
    source.stale = False
    reasoner.verified = False
    task = await execute(assistant, request, "unverified")
    assert task["state"] == "failed"
    release = assistant.release("owner", task, "local")
    assert not release["findings"]
    with pytest.raises(Rejected):
        assistant.release("owner", task, "other-requester")


async def test_owner_ipc_composes_actual_pipeline(tmp_path, monkeypatch):
    from orbo_runtime import cli

    monkeypatch.setattr(cli, "FakeRobot", Robot)
    service = cli.LocalService(
        {
            "runtime": {
                "owner": "owner",
                "profile": "simulated",
                "database": str(tmp_path / "ipc.db"),
            },
            "embodied": {
                "enabled": True,
                "environment": "lab",
                "map_version": "map",
                "image_directory": str(tmp_path / "images"),
            },
        }
    )
    assistant = service.embodied.assistant
    assistant.observations.source = Frames(service.store, service.broker.adapter)
    assistant.vision.reasoner = Reasoner()
    for scope, dest in [
        ("task.execute", "lab"),
        ("robot.command", service.broker.adapter.robot_id),
        ("observation.capture", "lab"),
        ("observation.retain", "lab"),
        ("observation.infer", "lab"),
        ("cloud.reasoning", "near"),
        ("result.release", "local"),
    ]:
        service.store.grant(
            "owner", scope, dest, True, service.store.clock.utc() + 3600
        )
    await service.call(
        {"method": "place.register", "args": {"name": "shelf", "viewpoints": ["front"]}}
    )
    request = {
        "kind": "inspect",
        "target": "wrench",
        "places": ["shelf"],
        "purpose": "inspection",
        "allowed_actions": ["navigate", "capture", "infer", "report"],
        "budget": {"seconds": 300, "observations": 2, "provider_calls": 2},
    }
    task = await service.call(
        {"method": "task.submit", "args": {"key": "ipc-one", "request": request}}
    )
    await assistant.jobs[task["id"]]
    result = await service.call(
        {"method": "task.result", "args": {"task_id": task["id"]}}
    )
    assert result["findings"][0]["status"] == "present"
    assert (
        await service.call({"method": "task.get", "args": {"task_id": task["id"]}})
    )["request"]["provenance"] == "synthetic"
    events = service.store.db.execute(
        "SELECT kind FROM events WHERE episode_id=?", (task["episode_id"],)
    ).fetchall()
    assert "task.finding" in [r[0] for r in events] and "task.observation" in [
        r[0] for r in events
    ]
    await service.close()


async def test_search_retains_earlier_similar_sighting_when_last_view_empty(system):
    _, assistant, _, _, reasoner, request = system
    reasoner.statuses = ["similar", "not_visible"]
    task = await execute(assistant, request | {"kind": "find"})
    result = assistant.release("owner", task, "local")
    assert task["state"] == "completed" and result["partial"]
    assert result["coverage"] == ["front", "side"]
    assert result["findings"][0]["identity"] == "visual_similarity"
    assert "similar" in result["text"].lower()


def test_gazebo_profile_binds_adapter_and_scope_without_fake_fallback(
    tmp_path, monkeypatch
):
    from orbo_runtime import gazebo_adapter
    from orbo_runtime.cli import LocalService

    selected = []

    def factory(goals, *, map_version):
        selected.append((goals, map_version))
        return Robot()

    monkeypatch.setattr(gazebo_adapter, "GazeboAdapter", factory)
    service = LocalService(
        {
            "runtime": {
                "owner": "owner",
                "profile": "gazebo",
                "database": str(tmp_path / "state.db"),
            },
            "robot": {"goals": "surveyed-sim-goals.yaml"},
            "embodied": {
                "enabled": True,
                "environment": "simulation",
                "map_version": "sim-map",
                "image_directory": str(tmp_path / "images"),
            },
        }
    )
    assert selected == [("surveyed-sim-goals.yaml", "sim-map")]
    assert service.embodied.map_version == "sim-map"
    service.store.close()

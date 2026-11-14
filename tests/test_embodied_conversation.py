import json

import pytest

from orbo_runtime.contracts import Rejected, digest
from orbo_runtime.embodied_conversation import ConversationBackend
from orbo_runtime.embodied_tasks import TaskService
from orbo_runtime.evidence_memory import EvidenceMemory
from orbo_runtime.live_voice import DelegationContext
from orbo_runtime.providers import VerifiedReply
from orbo_runtime.storage import Store


def proposal(**changes):
    return (
        dict(
            action="submit",
            complete_request=True,
            kind="inspect",
            target="red wrench",
            places=["shelf"],
            reference_id=None,
            baseline_id=None,
            identity="category",
            clarification=None,
        )
        | changes
    )


class Reasoner:
    model = "z-ai/glm-5.3-flash"

    def __init__(self, output):
        self.output = output
        self.calls = []
        self.tampered = False
        self.hook = None

    async def complete(self, messages, tools):
        self.calls.append((messages, tools))
        if self.hook:
            self.hook()
        message = {"role": "assistant", "content": json.dumps(self.output)}
        return VerifiedReply(
            message,
            {
                "provider": "near",
                "e2ee": not self.tampered,
                "signature_kind": "provider_tee",
                "message_digest": digest(message),
                "model": self.model,
                "response_id": "fixture-response-" + str(len(self.calls)),
            },
            {"total_tokens": 20},
        )


class Assistant:
    """Real durable task service; external effects deliberately omitted in unit tests."""

    def __init__(self, store, memory):
        self.store, self.memory = store, memory
        self.tasks = TaskService(store)
        self.submitted = []
        self.controls = []

    async def submit(self, owner, key, request):
        task = self.tasks.admit(owner, key, request)
        self.submitted.append(task)
        return task

    async def revise(self, owner, task_id, revision, changes):
        return self.tasks.revise(owner, task_id, revision, changes)

    async def pause(self, owner, task_id):
        self.controls.append("pause")
        task = self.tasks.get(owner, task_id)
        return self.tasks.transition(owner, task_id, task["revision"], "paused")

    async def cancel(self, owner, task_id):
        self.controls.append("cancel")
        task = self.tasks.get(owner, task_id)
        return self.tasks.transition(owner, task_id, task["revision"], "cancelled")


@pytest.fixture
def configured(tmp_path):
    store = Store(":memory:")
    for scope, destination in [
        ("task.execute", "lab"),
        ("cloud.reasoning", "near"),
        ("memory.read", "lab"),
        ("memory.write", "lab"),
    ]:
        store.grant("owner", scope, destination, True, store.clock.utc() + 3600)
    memory = EvidenceMemory(store, tmp_path / "images")
    assistant = Assistant(store, memory)
    assistant.tasks.places.register(
        "owner",
        "lab",
        "map-a",
        "assembly shelf",
        aliases=["shelf"],
        viewpoints=["front"],
    )
    assistant.tasks.places.register(
        "owner", "lab", "map-a", "lower shelf", viewpoints=["lower"]
    )
    reasoner = Reasoner(proposal())
    backend = ConversationBackend(
        assistant, reasoner, "owner", "lab", "map-a", provenance="synthetic"
    )
    yield store, assistant, reasoner, backend
    store.close()


async def test_verified_text_admits_fixed_scope_and_deduplicates_before_inference(
    configured,
):
    store, assistant, reasoner, backend = configured
    result = await backend.text("Check for a red wrench on the shelf", "request-1")
    assert result.kind == "progress" and result.revision == 1
    task = backend.latest_task
    assert task["request"] == {
        "kind": "inspect",
        "target": "red wrench",
        "places": ["assembly shelf"],
        "environment": "lab",
        "map_version": "map-a",
        "purpose": "owner_assistance",
        "destination": "local",
        "allowed_actions": ["navigate", "capture", "infer", "report"],
        "budget": {"seconds": 300, "observations": 4, "provider_calls": 4},
        "provenance": "synthetic",
    }
    assert (
        await backend.text("Check for a red wrench on the shelf", "request-1") == result
    )
    assert len(assistant.submitted) == len(reasoner.calls) == 1
    assert reasoner.calls[0][1] == []
    with pytest.raises(Rejected, match="different context"):
        await backend.text("Check the bowl", "request-1")
    assert (
        store.db.execute("SELECT state FROM conversation_requests").fetchone()[0]
        == "resolved"
    )


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "attacker"},
        {"budget": {"seconds": 3600}},
        {"x": 1},
        {"action": "move_wheels"},
    ],
)
async def test_model_cannot_supply_authority_fields(configured, change):
    store, assistant, reasoner, backend = configured
    reasoner.output.update(change)
    with pytest.raises(Rejected, match="schema"):
        await backend.text("A request", "one")
    assert not assistant.submitted
    assert (
        store.db.execute("SELECT state FROM conversation_requests").fetchone()[0]
        == "unknown"
    )
    with pytest.raises(Rejected, match="unresolved"):
        await backend.text("A request", "one")
    assert len(reasoner.calls) == 1


async def test_failed_verification_releases_no_text_or_actions(configured):
    _, assistant, reasoner, backend = configured
    reasoner.tampered = True
    with pytest.raises(Rejected, match="verified"):
        await backend.text("Check wrench", "one")
    assert not assistant.submitted


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({"identity": "specific"}, "exact object"),
        ({"reference_id": "not-permitted"}, "exact object"),
        ({"places": ["bedroom"]}, "registered place"),
        ({"complete_request": False}, "complete request"),
        ({"kind": "compare"}, "accepted baseline"),
    ],
)
async def test_ambiguity_and_unknown_catalog_clarify_without_motion(
    configured, change, expected
):
    _, assistant, reasoner, backend = configured
    reasoner.output.update(change)
    result = await backend.text("Check this", "one")
    assert result.kind == "clarification" and expected in result.text
    assert not assistant.submitted


async def test_contextual_live_fragments_never_assumed_complete(configured):
    _, assistant, reasoner, backend = configured
    reasoner.output = proposal(
        action="clarify",
        complete_request=False,
        kind=None,
        target=None,
        places=[],
        identity="unspecified",
        clarification="intent",
    )
    context = DelegationContext(
        "live-session",
        "delegation",
        900,
        ({"role": "user", "text": "Could you check", "start_ms": 0, "end_ms": 800},),
    )
    result = await backend(context)
    assert result.kind == "clarification"
    selected = json.loads(reasoner.calls[0][0][1]["content"])
    assert selected["complete_turn"] is False
    assert selected["delegation_offset_ms"] == 900
    assert selected["transcript_fragments"][0]["text"] == "Could you check"
    assert not assistant.submitted


async def test_empty_context_never_calls_provider(configured):
    _, assistant, reasoner, backend = configured
    result = await backend(DelegationContext("s", "d", 0, ()))
    assert result.kind == "clarification"
    assert not reasoner.calls and not assistant.submitted


async def test_revision_and_explicit_task_controls_share_service(configured):
    _, assistant, reasoner, backend = configured
    await backend.text("Check the shelf", "one")
    original = backend.latest_task
    reasoner.output = proposal(action="revise", places=["lower shelf"])
    result = await backend.text("Check the lower shelf first instead", "two")
    assert result.revision == 2
    task = assistant.tasks.get("owner", original["id"])
    assert task["request"]["places"] == ["lower shelf"]
    assert task["request"]["budget"] == original["request"]["budget"]
    reasoner.output = proposal(
        action="pause", kind=None, target=None, places=[], identity="unspecified"
    )
    paused = await backend.text("Pause the task", "three")
    assert assistant.controls == ["pause"]
    assert "not been independently verified" in paused.text
    reasoner.output["action"] = "cancel"
    await backend.text("Cancel that task", "four")
    assert assistant.controls == ["pause", "cancel"]


async def test_revision_race_rejects_before_effect(configured):
    _, assistant, reasoner, backend = configured
    await backend.text("Check the shelf", "one")
    task = backend.latest_task
    reasoner.output = proposal(action="revise", places=["lower shelf"])
    reasoner.hook = lambda: assistant.tasks.revise(
        "owner", task["id"], 1, {"target": "bowl"}
    )
    with pytest.raises(Rejected, match="changed"):
        await backend.text("Check lower shelf", "two")
    assert assistant.tasks.get("owner", task["id"])["request"]["places"] == [
        "assembly shelf"
    ]


async def test_reference_scope_and_memory_permission_are_rechecked(configured):
    store, assistant, reasoner, backend = configured
    reference = assistant.memory.reference(
        "owner", "lab", "red wrench with star engraving"
    )
    reasoner.output = proposal(identity="specific", reference_id=reference["id"])
    store.grant("owner", "memory.read", "lab", False, store.clock.utc() + 100)
    result = await backend.text("Check my exact wrench", "one")
    assert result.kind == "clarification"
    assert json.loads(reasoner.calls[0][0][1]["content"])["references"] == []
    store.grant("owner", "memory.read", "lab", True, store.clock.utc() + 100)
    result = await backend.text("Check my exact wrench", "two")
    assert result.task_id
    assert (
        assistant.tasks.get("owner", result.task_id)["request"]["reference_id"]
        == reference["id"]
    )


async def test_local_request_cannot_steal_remote_task_audience(configured):
    _, assistant, reasoner, backend = configured
    live_backend = ConversationBackend(
        assistant, reasoner, "owner", "lab", "map-a", destination="openai"
    )
    result = await live_backend.text("check shelf", "live")
    ctx = DelegationContext(
        "s", "d", 0, ({"role": "user", "text": "cancel"},), result.task_id, 1
    )
    with pytest.raises(Rejected, match="audience"):
        await backend(ctx)
    assert not assistant.controls


async def test_memory_revoked_during_interpretation_never_admits_reference(configured):
    store, assistant, reasoner, backend = configured
    reference = assistant.memory.reference("owner", "lab", "distinct red wrench")
    reasoner.output = proposal(identity="specific", reference_id=reference["id"])
    reasoner.hook = lambda: store.grant(
        "owner", "memory.read", "lab", False, store.clock.utc() + 100
    )
    with pytest.raises(Rejected, match="permission"):
        await backend.text("check exact wrench", "one")
    assert not assistant.submitted


async def test_interpretation_usage_is_separate_from_task_and_unpriced(configured):
    store, _, reasoner, backend = configured
    result = await backend.text("Check shelf", "one")
    row = store.db.execute(
        "SELECT * FROM records WHERE provider='near' AND kind='usage'"
    ).fetchone()
    body = json.loads(row["body"])
    assert row["account"] == "owner"
    assert body["provenance"] == "fixture"
    assert body["data"]["raw_usage"] == {"total_tokens": 20}
    assert body["data"]["amount"] is None
    assert body["data"]["task_id"] is None  # Task does not exist before interpretation.
    request = store.db.execute(
        "SELECT result FROM conversation_requests WHERE key=?",
        (body["data"]["request_key"],),
    ).fetchone()
    assert json.loads(request[0])["task_id"] == result.task_id

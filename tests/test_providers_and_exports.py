import asyncio
import json
from types import SimpleNamespace as NS

import pytest

from orbo_runtime.broker import Broker, FakeRobot
from orbo_runtime.contracts import ManualClock, Rejected
from orbo_runtime.coordinator import Coordinator
from orbo_runtime.providers import (
    NearReasoner,
    VerifiedReply,
)
from orbo_runtime.storage import Store
from orbo_runtime.traces import Exports, analyze, trajectory


class Dump:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **kwargs):
        return self.value


class SDK:
    def __init__(
        self, *, fail=False, models=("test-attestation",), kind="provider_tee"
    ):
        self.fail, self.models, self.kind = fail, models, kind
        self.calls = []
        self.chat = NS(completions=NS(create=self.create))

    async def verify(self, model):
        self.calls.append("attest")
        return NS(models=self.models, verified_at=123)

    async def create(self, **kwargs):
        self.calls.append("create")
        return NS(
            id="reply-1",
            model=kwargs["model"],
            choices=[
                NS(
                    finish_reason="stop",
                    message=Dump({"role": "assistant", "content": "verified text"}),
                )
            ],
            usage=Dump({"total_tokens": 12}),
        )

    async def verify_response(self, response_id):
        self.calls.append("verify")
        if self.fail:
            raise RuntimeError("synthetic verification failure")
        from dataclasses import make_dataclass

        Result = make_dataclass("Result", [("id", str), ("signature_kind", str)])
        return Result(response_id, self.kind)


async def test_near_verifies_before_release():
    sdk = SDK()
    reply = await NearReasoner(client=sdk).complete([], [])
    assert sdk.calls == ["attest", "create", "verify"]
    assert reply.message["content"] == "verified text"
    assert reply.evidence["e2ee"] is True


@pytest.mark.parametrize(
    "options", [{"fail": True}, {"models": ()}, {"kind": "gateway"}]
)
async def test_near_verification_failure_releases_nothing(options):
    sdk = SDK(**options)
    with pytest.raises((RuntimeError, Rejected)):
        await NearReasoner(client=sdk).complete([], [])
    if not sdk.models:
        assert sdk.calls == ["attest"]


def setup_store(tmp_path):
    store = Store(tmp_path / "journal.db", ManualClock())
    episode = store.episode("owner", provenance="synthetic")
    for scope, destination in [("cloud.reasoning", "near"), ("tools.execute", "local")]:
        store.grant("owner", scope, destination, True, store.clock.utc() + 3600)
    return store, episode


async def test_tools_correlated_bounded_and_durable(tmp_path):
    store, episode = setup_store(tmp_path)
    call = {
        "id": "call-1",
        "type": "function",
        "function": {
            "name": "schedule_reminder",
            "arguments": json.dumps(
                {"text": "stretch", "due_utc": store.clock.utc() + 10}
            ),
        },
    }

    class Reasoner:
        async def complete(self, messages, tools):
            if messages[-1]["role"] == "tool":
                assert messages[-1]["tool_call_id"] == "call-1"
                assert (
                    store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0]
                    == 1
                )
                return VerifiedReply(
                    {"role": "assistant", "content": "saved"}, {"synthetic": True}, {}
                )
            return VerifiedReply(
                {"role": "assistant", "tool_calls": [call, call]},
                {"synthetic": True},
                {},
            )

    coordinator = Coordinator(store, Broker(store, FakeRobot()), Reasoner())
    assert await coordinator.converse("owner", episode, "remind me") == "saved"
    assert store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 1
    with pytest.raises(Rejected):
        await coordinator.execute(
            "owner",
            episode,
            "bad",
            {**call, "function": {"name": "shell", "arguments": "{}"}},
        )
    store.close()


async def test_failed_verification_cannot_invoke_tools(tmp_path):
    store, episode = setup_store(tmp_path)
    coordinator = Coordinator(
        store, Broker(store, FakeRobot()), NearReasoner(client=SDK(fail=True))
    )
    with pytest.raises(RuntimeError):
        await coordinator.converse("owner", episode, "remind me")
    assert store.db.execute("SELECT COUNT(*) FROM reminders").fetchone()[0] == 0
    store.close()


async def test_cancelled_reasoning_late_reply_is_discarded(tmp_path):
    store, episode = setup_store(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()

    class LateReasoner:
        async def complete(self, messages, tools):
            entered.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                pass  # Deliberately broken provider ignores cancellation.
            return VerifiedReply({"content": "obsolete"}, {}, {})

    coordinator = Coordinator(store, Broker(store, FakeRobot()), LateReasoner())
    task = asyncio.create_task(coordinator.converse("owner", episode, "hello"))
    await entered.wait()
    await coordinator.interrupt()
    with pytest.raises(Rejected):
        await task
    assert (
        store.db.execute(
            "SELECT COUNT(*) FROM events WHERE kind='turn.completed'"
        ).fetchone()[0]
        == 0
    )
    store.close()


def test_export_requires_exact_review_revocation_and_permission_revision(tmp_path):
    store, episode = setup_store(tmp_path)
    store.event(
        episode,
        "observation.independent",
        {"content": "synthetic sentinel, outcome unknown"},
    )
    store.end_episode(episode, "owner", "unknown")
    exports = Exports(store)
    export = exports.prepare("owner", episode, "attention_timing", "consumer-fixture")
    package = export["package"]
    assert analyze(package)["independent_observation_count"] == 1
    assert b"synthetic sentinel" in trajectory(package)
    assert b'"role":"observation"' in trajectory(package)
    with pytest.raises(Rejected):
        exports.approved_bytes("owner", export["export_id"])
    store.grant("owner", "contribute", "consumer-fixture", True, store.clock.utc() + 60)
    exports.approve(
        "owner", export["export_id"], export["sha256"], expires=store.clock.utc() + 30
    )
    assert json.loads(exports.approved_bytes("owner", export["export_id"])) == package
    store.grant("owner", "contribute", "consumer-fixture", True, store.clock.utc() + 60)
    with pytest.raises(Rejected):
        exports.approved_bytes("owner", export["export_id"])
    exports.approve(
        "owner", export["export_id"], export["sha256"], expires=store.clock.utc() + 30
    )
    exports.revoke("owner", export["export_id"])
    with pytest.raises(Rejected):
        exports.approved_bytes("owner", export["export_id"])
    with store.transaction() as db:
        db.execute("UPDATE exports SET content='{}' WHERE id=?", (export["export_id"],))
    with pytest.raises(Rejected):
        exports.preview("owner", export["export_id"])
    store.close()

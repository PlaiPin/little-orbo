"""Adversarial scheduling, restart recovery and immutable minimized exports."""

import asyncio
import hashlib
import json
import uuid

import httpx
import pytest

from orbo_runtime.commons import Commons
from orbo_runtime.consumer import EvidenceConsumer
from orbo_runtime.contracts import ManualClock, Rejected, canonical
from orbo_runtime.storage import Store
from orbo_runtime.traces import Exports, trajectory


class Daemon:
    def __init__(self, package):
        self.package = package
        self.hash = "sha256:" + hashlib.sha256(trajectory(package)).hexdigest()
        self.envelope = "sha256:" + "a" * 64
        self.calls = []
        self.queue_state = "pending"
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.block = False
        self.lose_approval = False
        self.lose_revoke = False
        self.history = []
        self.change_page = False

    async def review(self, entry):
        events = [
            {
                "event_type": "feedback",
                "redacted_content": r["content"],
                "structured_payload": {"record_type": "trajectory_observation"},
            }
            for r in map(json.loads, trajectory(self.package).splitlines())
            if r["role"] == "observation"
        ]
        body = canonical(events)
        return {
            "body": body,
            "body_digest": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
            "envelope_digest": self.envelope,
            "summary": {"entry": {"entry_id": entry, "session_hash": self.hash}},
        }

    async def call(self, method, params):
        self.calls.append(method)
        if method == "approve_reviewed":
            self.queue_state = "approved"
            self.started.set()
            if self.block:
                await self.release.wait()
            if self.lose_approval:
                raise TimeoutError("lost approval response")
            return {"approved": 1}
        if method == "orbo_revoke_reviewed":
            self.queue_state = "refused"
            if self.lose_revoke:
                raise TimeoutError("lost revoke response")
            return {
                "schema": "orbo.revocation.v1",
                "entry_id": params["entry_id"],
                "fenced": True,
            }
        assert method == "orbo_entry_status"
        offset = params["offset"]
        end = offset + 100
        return {
            "schema": "orbo.entry-status.v1",
            "entry_id": params["entry_id"],
            "session_hash": self.hash,
            "queue": {"state": self.queue_state, "envelope_digest": self.envelope},
            "offset": offset,
            "next_offset": end if end < len(self.history) else None,
            "history_digest": (
                "changed" if offset and self.change_page else "sha256:" + "c" * 64
            ),
            "history": self.history[offset:end],
        }


async def setup(path=":memory:"):
    store = Store(str(path), ManualClock())
    ep = store.episode("owner", provenance="synthetic")
    store.event(ep, "turn.started", {"text": "Meet Alice at private address"})
    store.event(
        ep, "attention.awaiting_ack", {"reminder_id": "private-id", "generation": 1}
    )
    store.event(
        ep, "attention.acknowledged", {"reminder_id": "private-id", "generation": 1}
    )
    store.end_episode(ep, "owner", "completed")
    export = Exports(store).prepare("owner", ep, "attention_timing", "commons")
    for scope in ["contribute", "commons.review"]:
        store.grant("owner", scope, "commons", True, store.clock.utc() + 100)
    ipc = Daemon(export["package"])
    commons = Commons(store, ipc)
    entry = str(uuid.uuid4())
    preview = await commons.review("owner", export["export_id"], entry)
    Exports(store).approve(
        "owner", export["export_id"], export["sha256"], expires=store.clock.utc() + 100
    )
    args = (
        "owner",
        export["export_id"],
        entry,
        preview["envelope_digest"],
        preview["body_digest"],
    )
    return store, export, commons, ipc, args


async def test_revoke_wins_delayed_approval_and_entry_cannot_be_reused():
    store, export, commons, ipc, args = await setup()
    ipc.block = True
    task = asyncio.create_task(commons.submit_reviewed(*args))
    await ipc.started.wait()
    await commons.revoke(*args[:3])
    ipc.release.set()
    assert (await task)["state"] == "revoked"
    with pytest.raises(Rejected):
        Exports(store).approved_bytes("owner", export["export_id"])
    with pytest.raises(Rejected):
        await commons.review(*args[:3])
    store.close()


async def test_restart_recovers_lost_approval_without_resending(tmp_path):
    path = tmp_path / "journal.db"
    store, export, commons, ipc, args = await setup(path)
    ipc.lose_approval = True
    with pytest.raises(TimeoutError):
        await commons.submit_reviewed(*args)
    store.close()
    store = Store(str(path), ManualClock())
    commons = Commons(store, ipc)
    assert commons.recovery_queue("owner")[0]["state"] == "approval_unknown"
    assert commons.recovery_queue("other") == []
    assert (await commons.recover(*args[:3]))["local_state"] == "approved"
    assert ipc.calls.count("approve_reviewed") == 1
    with pytest.raises(Rejected):
        await commons.submit_reviewed(*args)
    store.close()


async def test_lost_cancel_retries_durable_revocation_after_restart(tmp_path):
    path = tmp_path / "journal.db"
    store, _, commons, ipc, args = await setup(path)
    ipc.lose_revoke = True
    with pytest.raises(TimeoutError):
        await commons.revoke(*args[:3])
    assert commons.recovery_queue("owner")[0]["state"] == "revoke_pending"
    store.close()
    store = Store(str(path), ManualClock())
    commons = Commons(store, ipc)
    ipc.lose_revoke = False
    assert (await commons.recover(*args[:3]))["local_state"] == "revoked"
    assert "approve_reviewed" not in ipc.calls
    store.close()


async def test_unknown_pending_is_not_authorization_to_resend_and_expiry_revokes():
    store, _, commons, ipc, args = await setup()
    ipc.lose_approval = True
    with pytest.raises(TimeoutError):
        await commons.submit_reviewed(*args)
    ipc.queue_state = "pending"
    assert (await commons.recover(*args[:3]))["local_state"] == "approval_unknown"
    store.db.execute("UPDATE approvals SET expires=0")
    assert (await commons.recover(*args[:3]))["local_state"] == "revoked"
    assert ipc.calls.count("approve_reviewed") == 1
    store.close()


async def test_history_pages_past_old_limit_and_rejects_changed_snapshot():
    store, _, commons, ipc, args = await setup()
    ipc.history = [
        {"session_hash": ipc.hash, "submission_id": str(uuid.uuid4())}
        for _ in range(1101)
    ]
    result = await commons.status(*args[:3])
    assert len(result["submission_ids"]) == 1101
    assert result["daemon"]["history_complete"]
    ipc.change_page = True
    with pytest.raises(Rejected, match="exact-entry recovery"):
        await commons.status(*args[:3])
    store.close()


async def test_minimized_export_requires_new_approval_and_preserves_original():
    store, export, commons, ipc, args = await setup()
    exports = Exports(store)
    original = exports.preview("owner", export["export_id"])["content"]
    events = export["package"]["events"]
    child = exports.derive(
        "owner",
        export["export_id"],
        redactions=[
            ["events", 1, "payload", "text"],
            ["events", 2, "payload", "reminder_id"],
            ["episode", "owner"],
        ],
        omit_event_ids=[events[3]["id"]],
    )
    assert exports.preview("owner", export["export_id"])["content"] == original
    assert "Alice" not in canonical(child["package"])
    assert "private-id" not in canonical(child["package"])
    assert child["sha256"] != export["sha256"]
    assert child["package"]["evaluation"]["ack_latency_seconds"] == []
    assert child["package"]["evaluation"]["gap_event_ids"] == [events[3]["id"]]
    assert child["package"]["evaluation"]["metrics_complete"] is False
    for eid in [export["export_id"], child["export_id"]]:
        with pytest.raises(Rejected):
            exports.approved_bytes("owner", eid)
    with pytest.raises(Rejected):
        exports.approve(
            "owner",
            child["export_id"],
            export["sha256"],
            expires=store.clock.utc() + 50,
        )
    exports.approve(
        "owner", child["export_id"], child["sha256"], expires=store.clock.utc() + 50
    )
    child_ipc = Daemon(child["package"])
    child_client = Commons(store, child_ipc)
    entry = str(uuid.uuid4())
    preview = await child_client.review("owner", child["export_id"], entry)
    result = await child_client.submit_reviewed(
        "owner",
        child["export_id"],
        entry,
        preview["envelope_digest"],
        preview["body_digest"],
    )
    assert result["state"] == "approved"
    store.close()


async def test_derivation_rejects_fabricated_metadata_and_active_parent():
    store, export, commons, ipc, args = await setup()
    exports = Exports(store)
    for path in [
        ["episode", "state"],
        ["events", 0, "kind"],
        ["events", -1, "payload", "text"],
    ]:
        with pytest.raises(Rejected):
            exports.derive("owner", export["export_id"], redactions=[path])
    await commons.submit_reviewed(*args)
    with pytest.raises(Rejected, match="revoke and recover"):
        exports.derive("owner", export["export_id"], redactions=[["episode", "owner"]])
    await commons.revoke(*args[:3])
    assert exports.derive(
        "owner", export["export_id"], redactions=[["episode", "owner"]]
    )["approval_required"]
    store.close()


async def test_targeted_consumer_rejects_wrong_identity_and_reports_missing():
    store, export, _, ipc, _ = await setup()
    target = str(uuid.uuid4())
    wrong = True
    events = json.loads((await ipc.review(str(uuid.uuid4())))["body"])

    async def handler(request):
        assert request.url.params["submission_ids"] == target
        items = (
            [
                {
                    "submission_id": str(uuid.uuid4()),
                    "events": events,
                    "evidence_schema_version": "orbo.task-evidence.v1",
                }
            ]
            if wrong
            else []
        )
        return httpx.Response(
            200,
            headers={"x-orbo-task-selection": target},
            json={
                "export_id": str(uuid.uuid4()),
                "manifest": {},
                "items": items,
                "item_count": len(items),
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        consumer = EvidenceConsumer(
            "https://fixture.invalid", client=transport, token="fixture"
        )
        with pytest.raises(Rejected, match="identity and digest"):
            await consumer.retrieve(
                "attention_timing",
                [export["sha256"]],
                privacy_risk="low",
                submission_ids=[target],
            )
        wrong = False
        assert not (
            await consumer.retrieve(
                "attention_timing",
                [export["sha256"]],
                privacy_risk="low",
                submission_ids=[target],
            )
        )["complete"]
        with pytest.raises(Rejected):
            await consumer.retrieve(
                "attention_timing",
                [export["sha256"]],
                privacy_risk="low",
                submission_ids=[],
            )
    store.close()


async def test_status_imports_positive_daemon_revocation_without_new_send():
    store, export, commons, ipc, args = await setup()
    original = ipc.call

    async def fenced_status(method, params):
        result = await original(method, params)
        result["queue"]["revoked"] = True
        result["queue"]["envelope_digest"] = None
        return result

    ipc.call = fenced_status
    assert (await commons.status(*args[:3]))["local_state"] == "revoked"
    with pytest.raises(Rejected):
        Exports(store).approved_bytes("owner", export["export_id"])
    assert ipc.calls == ["orbo_entry_status"]
    store.close()


async def test_consumer_refuses_legacy_server_ignoring_selector():
    store, export, _, _, _ = await setup()

    async def handler(request):
        return httpx.Response(
            200,
            json={
                "export_id": str(uuid.uuid4()),
                "manifest": {},
                "items": [],
                "item_count": 0,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        consumer = EvidenceConsumer(
            "https://fixture.invalid", client=transport, token="fixture"
        )
        with pytest.raises(Rejected, match="acknowledged exact submission selection"):
            await consumer.retrieve(
                "attention_timing",
                [export["sha256"]],
                privacy_risk="low",
                submission_ids=[str(uuid.uuid4())],
            )
    store.close()

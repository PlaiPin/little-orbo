import asyncio
import hashlib
import json
import os
import uuid
import tempfile
from pathlib import Path

import pytest

from orbo_runtime.commons import Commons, ContributorIPC
from orbo_runtime.contracts import ManualClock, Rejected, canonical
from orbo_runtime.storage import Store
from orbo_runtime.traces import Exports, reconstruct_commons, trajectory


def fixture():
    store = Store(":memory:", ManualClock())
    episode = store.episode("owner", provenance="synthetic")
    store.event(episode, "observation.independent", {"observed": "fixture only"})
    store.end_episode(episode, "owner", "completed")
    export = Exports(store).prepare("owner", episode, "attention_timing", "commons")
    events = [
        {
            "event_type": "feedback",
            "redacted_content": r["content"],
            "structured_payload": {"record_type": "trajectory_observation"},
        }
        for r in map(json.loads, trajectory(export["package"]).splitlines())
        if r["role"] == "observation"
    ]
    return store, export, events


def test_consumer_requires_complete_preserved_export():
    store, export, events = fixture()
    assert reconstruct_commons(events) == export["package"]
    with pytest.raises(Rejected):
        reconstruct_commons(events[:-1])
    with pytest.raises(Rejected):
        reconstruct_commons(events + [events[-1]])
    events[-1]["redacted_content"] = events[-1]["redacted_content"].replace(
        "completed", "redacted"
    )
    with pytest.raises(Rejected):
        reconstruct_commons(events)
    store.close()


@pytest.fixture
def short_tmp():
    with tempfile.TemporaryDirectory(prefix="orbo-", dir="/tmp") as directory:
        yield Path(directory)


async def test_ipc_pages_exact_utf8_body_and_refuses_changed_digest(short_tmp):
    tmp_path = short_tmp
    os.chmod(tmp_path, 0o700)
    path = tmp_path / "daemon.sock"
    body = '["évidence"]'.encode()
    digest = "sha256:" + hashlib.sha256(body).hexdigest()
    entry = str(uuid.uuid4())
    changed = False

    async def handle(reader, writer):
        request = json.loads(await reader.readline())
        if request["method"] == "preview":
            result = {"envelope_digest": "sha256:" + "a" * 64}
        else:
            offset = request["params"]["offset"]
            if offset:
                assert request["params"]["body_digest"] == digest
            end = 4 if not offset else len(body)
            result = {
                "entry_id": entry,
                "offset": offset,
                "chunk": body[offset:end].decode(),
                "next_offset": end if end < len(body) else None,
                "total_bytes": len(body),
                "body_digest": digest
                if not changed or not offset
                else "sha256:changed",
                "envelope_digest": "sha256:" + "a" * 64,
                "enrolled": True,
            }
        writer.write(
            (canonical({"id": request["id"], "result": result}) + "\n").encode()
        )
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_unix_server(handle, str(path))
    path.chmod(0o600)
    async with server:
        client = ContributorIPC(path)
        result = await client.review(entry)
        assert result["body"].encode() == body
        changed = True
        with pytest.raises(Rejected, match="changed"):
            await client.review(entry)
    path.unlink()


async def test_lost_approval_response_is_not_retried():
    store, export, events = fixture()
    entry = str(uuid.uuid4())
    calls = []

    class IPC:
        async def review(self, entry_id):
            body = canonical(events)
            return {
                "body": body,
                "body_digest": "sha256:" + hashlib.sha256(body.encode()).hexdigest(),
                "envelope_digest": "sha256:" + "b" * 64,
                "summary": {
                    "entry": {
                        "entry_id": entry_id,
                        "session_hash": "sha256:"
                        + hashlib.sha256(trajectory(export["package"])).hexdigest(),
                    }
                },
            }

        async def call(self, method, params):
            calls.append((method, params))
            raise TimeoutError("synthetic lost response")

    for scope in ["contribute", "commons.review"]:
        store.grant("owner", scope, "commons", True, store.clock.utc() + 60)
    client = Commons(store, IPC())
    preview = await client.review("owner", export["export_id"], entry)
    Exports(store).approve(
        "owner", export["export_id"], export["sha256"], expires=store.clock.utc() + 60
    )
    args = (
        "owner",
        export["export_id"],
        entry,
        preview["envelope_digest"],
        preview["body_digest"],
    )
    with pytest.raises(TimeoutError):
        await client.submit_reviewed(*args)
    with pytest.raises(Rejected):
        await client.submit_reviewed(*args)
    assert len(calls) == 1 and calls[0][0] == "approve_reviewed"
    assert (
        store.db.execute("SELECT state FROM commons_reviews").fetchone()[0]
        == "approval_unknown"
    )
    store.close()


async def test_authorized_consumer_reconstructs_only_expected_delivered_bytes():
    import httpx
    from orbo_runtime.consumer import EvidenceConsumer
    from orbo_runtime.providers import ProviderError

    store, export, events = fixture()
    deny = False
    submission = str(uuid.uuid4())

    async def handler(request):
        assert request.url.path == "/v1/datasets/task-evidence"
        assert request.headers["authorization"] == "Bearer fixture-export-token"
        assert request.url.params["consent_scope"] == "debugging-evaluation"
        assert request.url.params["submission_ids"] == submission
        if deny:
            return httpx.Response(403)
        return httpx.Response(
            200,
            headers={"x-orbo-task-selection": submission},
            json={
                "export_id": str(uuid.uuid4()),
                "manifest": {"purpose": "fixture"},
                "item_count": 1,
                "items": [
                    {
                        "submission_id": submission,
                        "evidence_schema_version": "orbo.task-evidence.v1",
                        "events": events,
                    }
                ],
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as transport:
        consumer = EvidenceConsumer(
            "https://synthetic.invalid", client=transport, token="fixture-export-token"
        )
        result = await consumer.retrieve(
            "attention_timing",
            [export["sha256"]],
            privacy_risk="low",
            submission_ids=[submission],
        )
        assert (
            result["complete"]
            and result["tasks"][export["sha256"]]["package"] == export["package"]
        )
        assert (
            result["tasks"][export["sha256"]]["analysis"]["physical_success"]
            == "not_established"
        )
        deny = True
        with pytest.raises(ProviderError):
            await consumer.retrieve(
                "attention_timing",
                [export["sha256"]],
                privacy_risk="low",
                submission_ids=[submission],
            )
    store.close()


async def test_history_correlates_source_and_excludes_previous_account():
    store, export, events = fixture()
    entry, matched, foreign = (str(uuid.uuid4()) for _ in range(3))
    session_hash = "sha256:" + hashlib.sha256(trajectory(export["package"])).hexdigest()

    class IPC:
        async def review(self, entry_id):
            return {
                "body": canonical(events),
                "body_digest": "sha256:" + "a" * 64,
                "envelope_digest": "sha256:" + "b" * 64,
                "summary": {
                    "entry": {"entry_id": entry_id, "session_hash": session_hash}
                },
            }

        async def call(self, method, params):
            assert method == "orbo_entry_status"
            assert params == {
                "entry_id": entry,
                "session_hash": session_hash,
                "offset": 0,
            }
            return {
                "schema": "orbo.entry-status.v1",
                "entry_id": entry,
                "session_hash": session_hash,
                "offset": 0,
                "next_offset": None,
                "history_digest": "sha256:" + "c" * 64,
                "history": [
                    {
                        "session_hash": session_hash,
                        "submission_id": matched,
                        "status": "accepted",
                    },
                    {"session_hash": "other", "submission_id": foreign},
                    {
                        "session_hash": session_hash,
                        "submission_id": foreign,
                        "previous_account": True,
                    },
                ],
            }

    store.grant("owner", "commons.review", "commons", True, store.clock.utc() + 60)
    client = Commons(store, IPC())
    await client.review("owner", export["export_id"], entry)
    status = await client.status("owner", export["export_id"], entry)
    assert status["submission_ids"] == [matched]
    assert status["delivery_established"] is False
    store.close()

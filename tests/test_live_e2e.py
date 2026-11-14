import argparse
import asyncio
import base64
import json
import wave

import pytest

from orbo_runtime.contracts import Rejected
from orbo_runtime import live_e2e as e2e
from orbo_runtime.providers import VerifiedReply


@pytest.fixture
def config(tmp_path):
    image = tmp_path / "scene.png"
    image.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\0\0\0\rIHDR"
        + (16).to_bytes(4, "big") * 2
        + b"\0" * 20
    )
    audio = tmp_path / "request.wav"
    with wave.open(str(audio), "wb") as stream:
        stream.setnchannels(1)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\x01\0" * 2400)
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "stages": ["near", "live"],
                "image": "scene.png",
                "audio": "request.wav",
                "live_seconds": 10,
            }
        )
    )
    return e2e.load_config(path)


def flags(**changes):
    return argparse.Namespace(
        **{
            **{
                f"allow_{name}": False
                for name in ("cloud", "task", "motion", "commons", "export")
            },
            **changes,
        }
    )


def reply(text="ORBO_OK", **proof):
    return VerifiedReply(
        {"content": text},
        {
            "provider": "near",
            "e2ee": True,
            "signature_kind": "provider_tee",
            "response_id": "provider-response",
            **proof,
        },
        {"total_tokens": 10, "unsafe_extra": "secret"},
    )


class Reasoner:
    def __init__(self, **kwargs):
        self.calls, self.closed = [], False

    async def complete(self, messages, tools):
        self.calls.append(messages)
        return reply()

    async def close(self):
        self.closed = True


def test_preflight_never_contacts_network(config, monkeypatch):
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    monkeypatch.delenv("NEARAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    result = e2e.preflight(config)
    assert not result["ready"]
    assert "credential:NEARAI_API_KEY" in result["missing"]
    assert result["network_contacted"] is False
    assert len(result["inputs"]) == 2
    assert "scene.png" not in str(result)


def test_auth_fences_all_effects(config):
    with pytest.raises(Rejected):
        e2e.authorize(config, flags())
    e2e.authorize(config, flags(allow_cloud=True))
    config.update(stages=["task"], task={"allowed_actions": ["navigate"]})
    with pytest.raises(Rejected):
        e2e.authorize(config, flags(allow_cloud=True, allow_task=True))
    e2e.authorize(config, flags(allow_cloud=True, allow_task=True, allow_motion=True))
    config.update(stages=["live"], live_backend="runtime")
    with pytest.raises(Rejected):
        e2e.authorize(config, flags(allow_cloud=True))
    config.update(stages=["commons"], commons={"submit": True})
    with pytest.raises(Rejected):
        e2e.authorize(config, flags(allow_commons=True))
    e2e.authorize(config, flags(allow_commons=True, allow_export=True))


def test_input_bounds_before_any_provider(config):
    from pathlib import Path

    Path(config["image"]).write_bytes(b"not png" * 100)
    with pytest.raises(Rejected):
        e2e.read_image(config["image"])
    with wave.open(config["audio"], "wb") as stream:
        stream.setnchannels(2)
        stream.setsampwidth(2)
        stream.setframerate(24000)
        stream.writeframes(b"\0" * 9600)
    with pytest.raises(Rejected):
        e2e.read_audio(config["audio"])


@pytest.mark.parametrize(
    "proof", [{"e2ee": False}, {"signature_kind": "gateway"}, {"provider": "fixture"}]
)
def test_unverified_response_never_passes(proof):
    with pytest.raises(Rejected):
        e2e.verified_summary(reply(**proof))


async def test_near_uses_selected_image_and_closes(config):
    provider = Reasoner()
    result = await e2e.near_stage(config, factory=lambda **kw: provider)
    assert provider.closed and len(provider.calls) == 2
    assert provider.calls[1][0]["content"][1]["image_url"]["url"].startswith(
        "data:image/png;base64,"
    )
    assert "unsafe_extra" not in str(result)
    assert "provider-response" not in str(result)
    assert result["visual_accuracy"] == "requires_scene_review"


class Transport:
    def __init__(self, *, output=True, delegate=True, audio_before_ack=False):
        self.events = asyncio.Queue()
        self.output, self.delegate = output, delegate
        self.audio_before_ack = audio_before_ack
        self.closed = False

    async def connect(self):
        pass

    async def send(self, event):
        if event["type"] == "session.start":
            await self.events.put(
                {"type": "session.started", "session": {"id": "live-test"}}
            )
            if self.delegate:
                await self.events.put(
                    {
                        "type": "session.input_transcript.delta",
                        "event_id": "transcript",
                        "start_ms": 0,
                        "end_ms": 10,
                        "delta": "Describe your capabilities",
                    }
                )
                await self.events.put(
                    {
                        "type": "session.delegation.created",
                        "offset_ms": 10,
                        "delegation": {"id": "delegation", "target": "client"},
                    }
                )
            else:
                await self.finish()
        elif event["type"] == "session.commentary.append":
            if self.audio_before_ack:
                await self.events.put(
                    {
                        "type": "session.output_audio.delta",
                        "delta": base64.b64encode(b"\x01\0" * 100).decode(),
                    }
                )
            await self.events.put(
                {
                    "type": "session.commentary.appended",
                    "client_event_id": event["event_id"],
                }
            )
            if self.output:
                await self.events.put(
                    {
                        "type": "session.output_audio.delta",
                        "delta": base64.b64encode(b"\x01\0" * 100).decode(),
                    }
                )
            await self.finish()
        elif event["type"] == "session.close":
            await self.finish()

    async def finish(self):
        await self.events.put(
            {
                "type": "session.closed",
                "session": {"id": "live-test"},
                "usage": {"seconds": 1},
            }
        )

    async def receive(self):
        return await self.events.get()

    async def close(self):
        self.closed = True


async def test_live_requires_delegation_context_audio_and_usage(config):
    transport = Transport()
    result = await e2e.live_stage(config, factory=Reasoner, transport=transport)
    assert result["verified_delegations"] == 1
    assert result["context_accepted"] == 1
    assert result["frames_after_context_acceptance"] == 100
    assert result["audibility"] == "not_tested"
    assert transport.closed


@pytest.mark.parametrize(
    "options",
    [
        {"output": False},
        {"delegate": False},
        {"output": False, "audio_before_ack": True},
    ],
)
async def test_session_start_alone_is_not_success(config, options):
    transport = Transport(**options)
    with pytest.raises(Rejected):
        await e2e.live_stage(config, factory=Reasoner, transport=transport)
    assert transport.closed


async def test_live_runtime_chain_uses_openai_release_and_real_provider_evidence(
    config, monkeypatch
):
    config["live_backend"] = "runtime"
    calls = []

    async def call(config, method, args):
        calls.append((method, args))
        if method == "task.delegate":
            context = args["context"]
            assert context["complete_turn"] is False
            assert context["delegation_id"] == "delegation"
            assert context["offset_ms"] == 10
            assert context["fragments"][0]["text"] == "Describe your capabilities"
            return {"task_id": "task"}
        if method == "task.get":
            return {
                "id": "task",
                "episode_id": "episode",
                "revision": 1,
                "state": "completed",
                "request": {"provenance": "synthetic"},
            }
        if method == "task.result":
            return {"text": "The camera shows a desk."}
        if method == "task.evidence":
            return {"live_service": True, "verified_response_count": 1}
        raise AssertionError(method)

    monkeypatch.setattr(e2e, "runtime_call", call)
    # wait_task's injectable default is resolved at function definition; supply
    # the same IPC replacement explicitly to exercise the production workflow.
    original_wait = e2e.wait_task

    async def wait(config, task):
        return await original_wait(config, task, call=call)

    monkeypatch.setattr(e2e, "wait_task", wait)
    result = await e2e.live_stage(config, transport=Transport())
    assert result["tasks"][0]["task_id"] == "task"
    assert result["tasks"][0]["provenance"] == "synthetic"
    assert result["frames_after_context_acceptance"] == 100


async def test_task_rejects_fixture_and_records_cancellation(config):
    calls = []

    async def call(config, method, args):
        calls.append(method)
        if method == "task.result":
            return {"text": "something"}
        if method == "task.evidence":
            return {"live_service": False, "verified_response_count": 1}
        if method == "task.cancel":
            return {"state": "cancelled"}

    task = {"id": "task-id", "episode_id": "episode-id", "state": "completed"}
    with pytest.raises(e2e.StageFailure) as failure:
        await e2e.wait_task(config, task, call=call)
    assert calls[-1] == "task.cancel"
    assert failure.value.evidence["task_id"] == "task-id"
    assert failure.value.evidence["cancellation"] == "runtime_recorded_cancelled"


async def test_task_cancels_known_id_on_timeout(config):
    calls = []

    async def call(config, method, args):
        calls.append((method, args))
        return {"state": "cancelled"}

    with pytest.raises(e2e.StageFailure):
        async with asyncio.timeout(0.01):
            await e2e.wait_task(
                config,
                {"id": "id", "episode_id": "episode", "state": "working"},
                call=call,
            )
    assert calls == [("task.cancel", {"task_id": "id"})]


async def test_commons_requires_exact_reconstructed_delivery(config):
    config["commons"] = {
        "export_id": "export",
        "entry_id": "entry",
        "source_digest": "d" * 64,
        "privacy_risk": "low",
        "submit": True,
        "envelope_digest": "e" * 64,
        "body_digest": "b" * 64,
    }
    calls = []

    async def call(config, method, args):
        calls.append((method, args))
        if method == "commons.status":
            return {"local_state": "uploaded", "submission_ids": ["submission"]}
        if method == "consumer.retrieve":
            return {"complete": True, "response_sha256": "f" * 64}

    result = await e2e.commons_stage(config, call=call)
    assert [method for method, args in calls] == [
        "commons.submit_reviewed",
        "commons.status",
        "consumer.retrieve",
    ]
    assert calls[0][1]["body_digest"] == "b" * 64
    assert calls[2][1]["source_digests"] == ["d" * 64]
    assert result["accounting"] == "not_established"


async def test_report_redacts_errors_and_stops(config, tmp_path, monkeypatch):
    async def fail(config):
        raise RuntimeError("secret-key private transcript")

    monkeypatch.setattr(e2e, "near_stage", fail)
    path = tmp_path / "report.json"
    report = {"run_id": "run", "results": []}
    assert not await e2e.run(config, report, path)
    assert len(report["results"]) == 1
    assert "secret-key" not in path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert report["results"][0]["error_class"] == "RuntimeError"

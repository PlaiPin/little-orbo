import asyncio
import json
import time

import pytest

from orbo_runtime.contracts import Rejected
from orbo_runtime.economics import Ledger
from orbo_runtime.live_voice import (
    DuplexAudio,
    LiveBridge,
    LiveConfig,
    LiveWebSocket,
    ReleasedUpdate,
    LIVE_URL,
)
from orbo_runtime.storage import Store


class Audio:
    sample_rate = 24000
    output_bytes = 0
    generated_frames = 0
    played_frames = 0
    last_activity = time.monotonic()

    def start(self):
        self.running = True

    async def read(self):
        await asyncio.Future()

    def enqueue(self, chunk):
        self.generated_frames += len(chunk) // 2

    def resume(self):
        self.interrupted = False

    def interrupt(self):
        self.interrupted = True

    def check(self):
        pass

    def close(self):
        self.running = False


class Transport:
    def __init__(self):
        self.events = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def connect(self):
        pass

    async def send(self, event):
        self.sent.append(event)
        if event["type"] == "session.start":
            await self.events.put(
                {"type": "session.started", "session": {"id": "live-fixture"}}
            )
        elif event["type"] == "session.close":
            await self.events.put(
                {
                    "type": "session.closed",
                    "session": {"id": "live-fixture"},
                    "usage": {"seconds": 15},
                    "reason": "close_requested",
                }
            )
        elif event["type"] == "session.commentary.append":
            await self.events.put(
                {
                    "type": "session.commentary.appended",
                    "client_event_id": event["event_id"],
                }
            )

    async def receive(self):
        return await self.events.get()

    async def close(self):
        self.closed = True


def setup(backend, **kwargs):
    store = Store(":memory:")
    episode = store.episode("alice", provenance="synthetic")
    for scope, destination in [
        ("cloud.voice", "openai"),
        ("speech.play", "local"),
        ("cloud.reasoning", "near"),
    ]:
        store.grant("alice", scope, destination, True, store.clock.utc() + 1000)
    transport = Transport()
    bridge = LiveBridge(
        store,
        "alice",
        episode,
        backend,
        transport,
        Audio(),
        config=LiveConfig(provenance="fixture"),
        **kwargs,
    )
    return store, transport, bridge


async def ready(bridge):
    job = asyncio.create_task(bridge.run(wake="local"))
    await asyncio.wait_for(bridge.started.wait(), 1)
    await asyncio.sleep(0)
    return job


async def finish(bridge, job):
    await bridge.close()
    await job
    if bridge._worker:
        await bridge._worker


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.001)


async def test_wire_context_delegation_dedup_and_cumulative_usage():
    contexts = []

    async def backend(context):
        contexts.append(context)
        return ReleasedUpdate(
            "A similar wrench is visible; identity is uncertain.", "task", 2
        )

    store, wire, bridge = setup(backend, current_revision=lambda _: 2)
    job = await ready(bridge)
    config = wire.sent[0]["session"]
    assert config["delegation"]["type"] == "client"
    assert config["store"] is False
    assert config["audio"]["format"] == {"type": "audio/pcm", "rate": 24000}
    for index, text in enumerate(["check ", "this wrench"]):
        await wire.events.put(
            {
                "type": "session.input_transcript.delta",
                "event_id": str(index),
                "delta": text,
                "start_ms": index * 200,
                "end_ms": (index + 1) * 200,
            }
        )
    event = {
        "type": "session.delegation.created",
        "offset_ms": 500,
        "delegation": {"id": "d1", "target": "client"},
    }
    await wire.events.put(event)
    await wire.events.put(event)
    await until(lambda: len(contexts) == 1)
    await bridge.work.join()
    assert "".join(x["text"] for x in contexts[0].fragments) == "check this wrench"
    assert contexts[0].complete_turn is False
    assert contexts[0].delegation_id == "d1"
    spoken = [e for e in wire.sent if e["type"] == "session.commentary.append"]
    assert spoken[0]["delegation_id"] == "d1"
    assert "uncertain" in spoken[0]["content"]
    for seconds in [12, 15]:
        await wire.events.put(
            {"type": "session.usage.updated", "usage": {"seconds": seconds}}
        )
    await until(lambda: bridge.seconds == 15)
    await finish(bridge, job)
    record = json.loads(
        store.db.execute("SELECT body FROM records WHERE provider='openai'").fetchone()[
            0
        ]
    )
    assert record["data"]["seconds"] == 15
    assert record["data"]["finalized"] is True
    assert record["data"]["amount"] is None
    assert len(contexts) == 1
    assert Ledger(store).summary()["openai_funding"] == "separate_owner_account"


async def test_stale_results_never_leave_and_speech_interrupt_does_not_cancel_task():
    started, release = asyncio.Event(), asyncio.Event()
    revision = [1]

    async def backend(context):
        started.set()
        await release.wait()
        return ReleasedUpdate("old task", "task", 1)

    store, wire, bridge = setup(backend, current_revision=lambda _: revision[0])
    job = await ready(bridge)
    await wire.events.put(
        {
            "type": "session.delegation.created",
            "offset_ms": 100,
            "delegation": {"id": "d", "target": "client"},
        }
    )
    await started.wait()
    bridge.interrupt_speech()
    revision[0] = 2
    release.set()
    await bridge.work.join()
    assert not any(e["type"] == "session.commentary.append" for e in wire.sent)
    assert (
        store.db.execute("SELECT state FROM live_delegations").fetchone()[0]
        == "unknown"
    )
    payload = json.loads(
        store.db.execute(
            "SELECT payload FROM events WHERE kind='live.speech_interrupted'"
        ).fetchone()[0]
    )
    assert payload["task_cancelled"] is False
    await finish(bridge, job)


async def test_close_keeps_application_job_alive_and_saves_result_pending():
    entered, release = asyncio.Event(), asyncio.Event()

    async def backend(context):
        entered.set()
        await release.wait()
        return ReleasedUpdate("Task accepted.", "task", 1)

    store, wire, bridge = setup(backend, current_revision=lambda _: 1)
    job = await ready(bridge)
    await wire.events.put(
        {
            "type": "session.delegation.created",
            "offset_ms": 10,
            "delegation": {"id": "d", "target": "client"},
        }
    )
    await entered.wait()
    await bridge.close()
    await job
    assert not bridge._worker.done()
    release.set()
    await bridge._worker
    assert (
        store.db.execute("SELECT state FROM live_deliveries").fetchone()[0] == "pending"
    )
    assert not any(e["type"] == "session.commentary.append" for e in wire.sent)


async def test_reconnect_history_excludes_unheard_generated_answer():
    async def backend(context):
        return ReleasedUpdate("Please clarify the place.")

    store, wire, bridge = setup(backend)
    job = await ready(bridge)
    for role in ["input", "output"]:
        await wire.events.put(
            {
                "type": f"session.{role}_transcript.delta",
                "event_id": role,
                "delta": role,
                "start_ms": 0,
                "end_ms": 100,
            }
        )
    await until(lambda: len(bridge.resume_history()) == 1)
    await finish(bridge, job)
    assert bridge.resume_history() == [
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "input"}],
        }
    ]


async def test_permission_revocation_stops_transport_without_motion_action():
    async def backend(context):
        raise AssertionError("not delegated")

    store, wire, bridge = setup(backend)
    job = await ready(bridge)
    store.grant("alice", "cloud.voice", "openai", False, store.clock.utc() + 100)
    with pytest.raises(Rejected, match="permission"):
        await job
    assert wire.closed
    assert store.db.execute("SELECT state,finalized FROM live_sessions").fetchone()[
        :
    ] == ("unconfirmed", 0)
    if bridge._worker:
        await bridge._worker


async def test_websocket_endpoint_bounds_and_auth_without_sdk_assumptions():
    arguments = []

    class Socket:
        async def send(self, value):
            assert json.loads(value)["type"] == "session.start"

        async def recv(self):
            return '{"type":"session.started"}'

        async def close(self):
            pass

    async def connector(*args, **kwargs):
        arguments.append((args, kwargs))
        return Socket()

    transport = LiveWebSocket(api_key="fixture-key", connector=connector)
    await transport.connect()
    await transport.send({"type": "session.start"})
    assert (await transport.receive())["type"] == "session.started"
    await transport.close()
    assert arguments[0][0] == (LIVE_URL,)
    assert arguments[0][1]["additional_headers"] == {
        "Authorization": "Bearer fixture-key"
    }
    assert arguments[0][1]["max_queue"] == 16
    assert arguments[0][1]["proxy"] is None


def test_pcm_bounds_interruption_and_aec_gate():
    audio = DuplexAudio()
    with pytest.raises(Rejected, match="echo"):
        audio.start()
    with pytest.raises(Rejected, match="PCM"):
        audio.enqueue(b"x")
    audio.enqueue(b"\0\0" * 20)
    audio.interrupt()
    audio.enqueue(b"\0\0" * 10)
    assert audio.output_bytes == 0
    audio.resume()
    audio.enqueue(b"\0\0" * 10)
    output = bytearray(20)
    audio._callback(b"\0\0" * 10, output, 10, None, False)
    assert audio.played_frames == 10 and audio.generated_frames == 40
    assert audio.output_bytes == 0
    with pytest.raises(Rejected, match="queue"):
        audio.enqueue(b"\0" * audio.limit)
        audio.enqueue(b"\0\0")


def test_openai_near_credit_is_rejected_and_update_limit_is_conservative():
    store = Store(":memory:")
    with pytest.raises(Rejected, match="OpenAI"):
        Ledger(store).observe(
            "openai",
            "a",
            "id",
            "usage",
            {"amount": "1", "instrument": "near_restricted_credit"},
            provenance="fixture",
        )
    with pytest.raises(Rejected):
        ReleasedUpdate("é" * 241)
    with pytest.raises(Rejected):
        ReleasedUpdate("hello", "task")


async def test_unqualified_aec_refused_before_billed_connection():
    async def backend(context):
        raise AssertionError

    store, wire, bridge = setup(backend)
    bridge.audio = DuplexAudio()
    with pytest.raises(Rejected, match="echo"):
        await bridge.run(wake="local")
    assert wire.sent == []
    assert store.db.execute("SELECT count(*) FROM live_sessions").fetchone()[0] == 0


def test_restart_fences_unresolved_voice_work_without_replay(tmp_path):
    async def backend(context):
        raise AssertionError("recovery must never execute a delegation")

    path = tmp_path / "journal.db"
    store = Store(path)
    episode = store.episode("owner", provenance="synthetic")
    LiveBridge(store, "owner", episode, backend, Transport(), Audio())
    store.db.execute(
        "INSERT INTO live_sessions VALUES(?,?,?,?,?,?,?,?,?)",
        (
            "s",
            "owner",
            episode,
            "old-provider",
            "active",
            5,
            0,
            store.clock.utc(),
            store.boot,
        ),
    )
    store.db.execute("INSERT INTO live_delegations VALUES('s','d',10,'working','t',1)")
    store.close()
    recovered = Store(path)
    bridge = LiveBridge(recovered, "owner", episode, backend, Transport(), Audio())
    assert recovered.db.execute(
        "SELECT state,seconds,finalized FROM live_sessions"
    ).fetchone()[:] == ("unconfirmed", 5, 0)
    assert (
        recovered.db.execute("SELECT state FROM live_delegations").fetchone()[0]
        == "unknown"
    )
    assert bridge.work.empty()
    assert bridge.binding == ("t", 1)
    recovered.close()

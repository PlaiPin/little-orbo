"""GPT-Live client delegation; conversation sessions never own robot authority.

The primary WebSocket has no final-turn marker, output audio timestamps, or
output-audio-done event. We therefore retain timed fragments and report software
playback separately, never turning generated speech into heard conversation.
"""

import asyncio
import base64
from collections import deque
from dataclasses import dataclass
import json
import math
import threading
import time
from typing import Awaitable, Callable

from .contracts import Rejected, canonical, identity
from .economics import Ledger
from .providers import credential

LIVE_URL = "wss://api.openai.com/v1/live/sessions"
SCHEMA = """
CREATE TABLE IF NOT EXISTS live_sessions(
 id TEXT PRIMARY KEY, owner TEXT NOT NULL, episode_id TEXT NOT NULL REFERENCES episodes(id),
 provider_id TEXT, state TEXT NOT NULL, seconds REAL NOT NULL DEFAULT 0,
 finalized INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS live_fragments(
 session_id TEXT NOT NULL REFERENCES live_sessions(id), event_id TEXT NOT NULL,
 role TEXT NOT NULL, start_ms INTEGER NOT NULL, end_ms INTEGER NOT NULL, text TEXT NOT NULL,
 PRIMARY KEY(session_id,event_id));
CREATE TABLE IF NOT EXISTS live_delegations(
 session_id TEXT NOT NULL REFERENCES live_sessions(id), id TEXT NOT NULL,
 offset_ms INTEGER NOT NULL, state TEXT NOT NULL, task_id TEXT, revision INTEGER,
 PRIMARY KEY(session_id,id));
CREATE TABLE IF NOT EXISTS live_deliveries(
 id TEXT PRIMARY KEY, session_id TEXT NOT NULL REFERENCES live_sessions(id),
 delegation_id TEXT, task_id TEXT, revision INTEGER, content TEXT NOT NULL,
 kind TEXT NOT NULL, state TEXT NOT NULL, created REAL NOT NULL);
"""


@dataclass(frozen=True)
class LiveConfig:
    model: str = "gpt-live-1"
    voice: str = "marin"
    sample_rate: int = 24000
    idle_seconds: float = 60
    startup_seconds: float = 30
    close_seconds: float = 15
    backend_seconds: float = 120
    max_session_seconds: float = 900
    max_delegations: int = 8
    account: str = "owner"
    provenance: str = "live_service"

    def __post_init__(self):
        if (
            self.sample_rate not in {16000, 24000}
            or self.model != "gpt-live-1"
            or not self.voice
            or not 5 <= self.idle_seconds <= 600
            or not 1 <= self.startup_seconds <= 60
            or not 1 <= self.close_seconds <= 60
            or not 1 <= self.backend_seconds <= 300
            or not 10 <= self.max_session_seconds <= 3600
            or not 1 <= self.max_delegations <= 16
            or self.provenance not in {"fixture", "live_service"}
        ):
            raise Rejected("invalid Live configuration")


@dataclass(frozen=True)
class DelegationContext:
    session_id: str
    delegation_id: str
    offset_ms: int
    fragments: tuple[dict, ...]
    task_id: str | None = None
    revision: int | None = None
    # Transcript fragments are evidence, not completed turns or authorization.
    complete_turn: bool = False


@dataclass(frozen=True)
class ReleasedUpdate:
    """Application-selected conversation text, never raw private model output."""

    text: str
    task_id: str | None = None
    revision: int | None = None
    kind: str = "result"

    def __post_init__(self):
        # UTF-8 byte count conservatively bounds token count below API's 500 limit.
        if (
            not isinstance(self.text, str)
            or not 0 < len(self.text.encode()) <= 480
            or self.kind not in {"result", "progress", "clarification"}
            or (self.task_id is None) != (self.revision is None)
        ):
            raise Rejected("invalid released Live update")


class LiveWebSocket:
    """Fixed trusted endpoint, bounded socket buffers, no reconnect/retry effects."""

    def __init__(self, *, api_key=None, connector=None):
        self.api_key, self.connector, self.socket = api_key, connector, None

    async def connect(self):
        connector = self.connector
        if connector is None:
            from websockets.asyncio.client import connect

            connector = connect
        self.socket = await connector(
            LIVE_URL,
            additional_headers={
                "Authorization": "Bearer "
                + (self.api_key or credential("OPENAI_API_KEY"))
            },
            max_size=262144,
            max_queue=16,
            open_timeout=30,
            close_timeout=5,
            ping_interval=20,
            ping_timeout=20,
            proxy=None,
        )

    async def send(self, event):
        async with asyncio.timeout(5):
            await self.socket.send(canonical(event))

    async def receive(self):
        raw = await self.socket.recv()
        if not isinstance(raw, str) or len(raw) > 262144:
            raise Rejected("invalid Live event envelope")
        event = json.loads(raw)
        if not isinstance(event, dict) or not isinstance(event.get("type"), str):
            raise Rejected("invalid Live event")
        return event

    async def close(self):
        if self.socket is not None:
            await self.socket.close()


class DuplexAudio:
    """Continuous PCM16 device I/O with bounded callbacks and qualified AEC.

    Queue/device frames are software observations, not proof of acoustic audibility.
    A hardware/OS echo-cancelled input must be commissioned before full duplex.
    """

    def __init__(
        self,
        *,
        sample_rate=24000,
        input_device=None,
        output_device=None,
        aec_qualified=False,
        stream_factory=None,
    ):
        if sample_rate not in {16000, 24000}:
            raise Rejected("Live requires 16 or 24 kHz PCM")
        self.sample_rate = sample_rate
        self.input_device, self.output_device = input_device, output_device
        self.aec_qualified, self.stream_factory = aec_qualified, stream_factory
        self.input, self.output = deque(), deque()
        self.input_bytes = self.output_bytes = 0
        self.limit = sample_rate * 2 * 2
        self.lock = threading.Lock()
        self.played_frames = self.generated_frames = 0
        self.last_activity = time.monotonic()
        self.failure = None
        self.stream = None
        self.output_enabled = True

    def _callback(self, indata, outdata, frames, timing, status):
        captured = bytes(indata)
        rendered = bytearray(frames * 2)
        with self.lock:
            if status or self.input_bytes + len(captured) > self.limit:
                self.failure = "audio_overflow_or_device_status"
            else:
                self.input.append(captured)
                self.input_bytes += len(captured)
            # A local amplitude indicator is only activity, not a speech detector.
            if any(
                abs(int.from_bytes(captured[i : i + 2], "little", signed=True)) > 600
                for i in range(0, len(captured), 2)
            ):
                self.last_activity = time.monotonic()
            written = 0
            while self.output and written < len(rendered):
                chunk = self.output.popleft()
                count = min(len(chunk), len(rendered) - written)
                rendered[written : written + count] = chunk[:count]
                if count < len(chunk):
                    self.output.appendleft(chunk[count:])
                self.output_bytes -= count
                written += count
            self.played_frames += written // 2
            if written:
                self.last_activity = time.monotonic()
        outdata[:] = rendered

    def start(self):
        if not self.aec_qualified:
            raise Rejected("full-duplex Live requires qualified echo-cancelled input")
        factory = self.stream_factory
        if factory is None:
            import sounddevice

            factory = sounddevice.RawStream
        self.stream = factory(
            samplerate=self.sample_rate,
            channels=1,
            dtype="int16",
            device=(self.input_device, self.output_device),
            blocksize=self.sample_rate // 50,
            callback=self._callback,
        )
        self.stream.start()

    async def read(self):
        while True:
            self.check()
            with self.lock:
                if self.input:
                    chunk = self.input.popleft()
                    self.input_bytes -= len(chunk)
                    return chunk
            await asyncio.sleep(0.005)

    def enqueue(self, chunk):
        if not chunk or len(chunk) % 2 or len(chunk) > self.limit:
            raise Rejected("invalid Live PCM output")
        with self.lock:
            self.generated_frames += len(chunk) // 2
            if not self.output_enabled:
                return
            if self.output_bytes + len(chunk) > self.limit:
                raise Rejected("Live playback queue exceeded")
            self.output.append(chunk)
            self.output_bytes += len(chunk)

    def interrupt(self):
        with self.lock:
            self.output.clear()
            self.output_bytes = 0
            self.output_enabled = False

    def resume(self):
        self.output_enabled = True

    def check(self):
        if self.failure:
            raise Rejected("Live audio device failed")

    def close(self):
        self.interrupt()
        if self.stream:
            self.stream.abort()
            self.stream.close()
            self.stream = None


class LiveBridge:
    def __init__(
        self,
        store,
        owner,
        episode_id,
        backend: Callable[[DelegationContext], Awaitable[ReleasedUpdate]],
        transport,
        audio,
        *,
        config=None,
        current_revision=None,
    ):
        self.store, self.owner, self.episode_id = store, owner, episode_id
        self.backend, self.transport, self.audio = backend, transport, audio
        self.config = config or LiveConfig()
        if audio.sample_rate != self.config.sample_rate:
            raise Rejected("capture/playback and Live sample rates must match")
        self.current_revision = current_revision
        store.db.executescript(SCHEMA)
        self.session_id = identity()
        self.provider_id = None
        self.started, self.finalized = asyncio.Event(), asyncio.Event()
        self.closing = False
        self.running = False
        self.work = asyncio.Queue(maxsize=self.config.max_delegations)
        self.last_activity = time.monotonic()
        self.created_mono = self.last_activity
        previous = store.db.execute(
            "SELECT d.task_id,d.revision FROM live_delegations d JOIN live_sessions s ON s.id=d.session_id "
            "WHERE s.owner=? AND s.episode_id=? AND d.task_id IS NOT NULL "
            "ORDER BY s.created DESC,d.offset_ms DESC LIMIT 1",
            (owner, episode_id),
        ).fetchone()
        self.binding = tuple(previous) if previous else (None, None)
        self.seconds = 0.0
        self._worker = None
        self._send_lock = asyncio.Lock()

    def _permission(self):
        self.store.require_episode(self.episode_id, self.owner)
        self.store.permission(self.owner, "cloud.voice", "openai")
        self.store.permission(self.owner, "speech.play", "local")

    async def _send(self, event):
        async with self._send_lock:
            await self.transport.send(event)

    def resume_history(self):
        """Restore only user fragments. Output transcript has no audibility proof."""
        rows = self.store.db.execute(
            "SELECT f.text FROM live_fragments f JOIN live_sessions s ON s.id=f.session_id "
            "WHERE s.owner=? AND s.episode_id=? AND f.role='user' "
            "ORDER BY s.created DESC,f.end_ms DESC LIMIT 32",
            (self.owner, self.episode_id),
        ).fetchall()
        selected, size = [], 0
        for row in rows:
            if size + len(row[0].encode()) > 8000:
                break
            size += len(row[0].encode())
            selected.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": row[0]}],
                }
            )
        return list(reversed(selected))

    async def run(self, *, wake):
        if wake not in {"local", "authenticated_remote"} or self.running:
            raise Rejected("explicit local/authenticated wake required")
        self._permission()
        self.running = True
        self.store.db.execute(
            "INSERT INTO live_sessions VALUES(?,?,?,?,?,?,?,?)",
            (
                self.session_id,
                self.owner,
                self.episode_id,
                None,
                "connecting",
                0,
                0,
                self.store.clock.utc(),
            ),
        )
        jobs = []
        try:
            await self.transport.connect()
            await self._send(
                {
                    "type": "session.start",
                    "event_id": identity(),
                    "session": {
                        "model": self.config.model,
                        "store": False,
                        "audio": {
                            "format": {
                                "type": "audio/pcm",
                                "rate": self.config.sample_rate,
                            },
                            "output": {"voice": self.config.voice},
                        },
                        "delegation": {
                            "type": "client",
                            "acknowledgement_delay_ms": 800,
                        },
                        "instructions": (
                            "You are Orbo's conversational voice. Delegate physical tasks, memory, "
                            "observations, corrections, pause and cancellation to the application. "
                            "Do not claim admission or completion before application confirmation. "
                            "Preserve uncertainty and inspected-view limits in application results. "
                            "An interrupted reply does not cancel a robot task. Ask for clarification "
                            "when the request or reference is incomplete."
                        ),
                        "input": self.resume_history(),
                    },
                }
            )
            async with asyncio.timeout(self.config.startup_seconds):
                while not self.started.is_set():
                    await self.handle(await self.transport.receive())
            self.audio.start()
            self._worker = asyncio.create_task(self._delegate())
            jobs = [
                asyncio.create_task(self._capture()),
                asyncio.create_task(self._supervise()),
            ]
            while not self.finalized.is_set():
                # A failed supervisor/capture must tear down the socket promptly.
                receiver = asyncio.create_task(self.transport.receive())
                done, _ = await asyncio.wait(
                    [receiver, *jobs], return_when=asyncio.FIRST_COMPLETED
                )
                if receiver in done:
                    await self.handle(receiver.result())
                else:
                    receiver.cancel()
                    await asyncio.gather(receiver, return_exceptions=True)
                    for task in done:
                        task.result()
                    break
        finally:
            self.closing = True
            for job in jobs:
                job.cancel()
            await asyncio.gather(*jobs, return_exceptions=True)
            # Closing voice never cancels the delegated application job. A bounded
            # backend worker may finish and leave its result durably pending.
            if self._worker and self._worker.done():
                await asyncio.gather(self._worker, return_exceptions=True)
            self.audio.close()
            await self.transport.close()
            state = "closed" if self.finalized.is_set() else "unconfirmed"
            self.store.db.execute(
                "UPDATE live_sessions SET state=? WHERE id=?", (state, self.session_id)
            )
            self.store.event(
                self.episode_id,
                "live.session_closed",
                {
                    "session_id": self.session_id,
                    "finalized": self.finalized.is_set(),
                    "generated_frames": self.audio.generated_frames,
                    "played_frames": self.audio.played_frames,
                    "evidence": "software_playback_only",
                    "spoken_result_audibility": "unknown",
                },
            )
            if self.provider_id:
                Ledger(self.store).observe(
                    "openai",
                    self.config.account,
                    self.provider_id,
                    "usage",
                    {
                        "model": self.config.model,
                        "seconds": self.seconds,
                        "finalized": self.finalized.is_set(),
                        "amount": None,
                        "session_id": self.session_id,
                    },
                    provenance=self.config.provenance,
                )
            self.running = False
        return {
            "session_id": self.session_id,
            "finalized": self.finalized.is_set(),
            "seconds": self.seconds,
        }

    async def _capture(self):
        while not self.closing:
            chunk = await self.audio.read()
            self._permission()
            await self._send(
                {
                    "type": "session.input_audio.append",
                    "audio": base64.b64encode(chunk).decode("ascii"),
                }
            )
        # A completed job tells run to tear down; wait for the receiver instead.
        await self.finalized.wait()

    async def _supervise(self):
        while not self.closing:
            self._permission()
            self.audio.check()
            latest = max(self.last_activity, self.audio.last_activity)
            if (
                time.monotonic() - self.created_mono >= self.config.max_session_seconds
                or (
                    time.monotonic() - latest >= self.config.idle_seconds
                    and not self.audio.output_bytes
                )
            ):
                await self.close()
                return
            await asyncio.sleep(0.05)
        await self.finalized.wait()

    async def wait_backend(self):
        """Join admitted application work before closing Store; does not replay work."""
        if self._worker:
            await self._worker

    async def close(self):
        if self.closing:
            return
        self.closing = True
        if self.started.is_set():
            await self._send({"type": "session.close", "event_id": identity()})
            try:
                await asyncio.wait_for(self.finalized.wait(), self.config.close_seconds)
            except TimeoutError:
                pass

    async def handle(self, event):
        kind = event.get("type")
        if kind == "error":
            # Provider error bodies may include caller data; never journal them.
            self.store.event(
                self.episode_id, "live.error", {"session_id": self.session_id}
            )
            raise Rejected("Live provider rejected a command")
        if kind == "session.started":
            if self.started.is_set():
                raise Rejected("duplicate session start")
            self.provider_id = event["session"]["id"]
            self.store.db.execute(
                "UPDATE live_sessions SET provider_id=?,state='active' WHERE id=?",
                (self.provider_id, self.session_id),
            )
            self.started.set()
            return
        if kind == "session.closed":
            if event.get("session", {}).get("id") != self.provider_id:
                raise Rejected("Live close session mismatch")
            self._usage(event)
            self.store.db.execute(
                "UPDATE live_sessions SET finalized=1,state='closed' WHERE id=?",
                (self.session_id,),
            )
            self.finalized.set()
            return
        if not self.started.is_set():
            if kind == "info":
                return
            raise Rejected("Live event before startup")
        if kind == "session.usage.updated":
            self._usage(event)
        elif kind in {
            "session.input_transcript.delta",
            "session.output_transcript.delta",
        }:
            role = "user" if kind == "session.input_transcript.delta" else "assistant"
            text, start, end = (
                event.get("delta"),
                event.get("start_ms"),
                event.get("end_ms"),
            )
            if (
                not isinstance(text, str)
                or len(text.encode()) > 8000
                or type(start) is not int
                or type(end) is not int
                or not 0 <= start <= end
            ):
                raise Rejected("invalid Live transcript")
            self.store.db.execute(
                "INSERT OR IGNORE INTO live_fragments VALUES(?,?,?,?,?,?)",
                (self.session_id, event["event_id"], role, start, end, text),
            )
            self.last_activity = time.monotonic()
            # Keep disk transcript context bounded per session as well as queries.
            self.store.db.execute(
                "DELETE FROM live_fragments WHERE session_id=? AND event_id NOT IN "
                "(SELECT event_id FROM live_fragments WHERE session_id=? ORDER BY end_ms DESC LIMIT 256)",
                (self.session_id, self.session_id),
            )
        elif kind == "session.output_audio.delta":
            self._permission()
            try:
                chunk = base64.b64decode(event["delta"], validate=True)
            except (ValueError, TypeError) as exc:
                raise Rejected("invalid Live audio encoding") from exc
            self.audio.enqueue(chunk)
        elif kind == "session.delegation.created":
            if self.closing:
                return
            item, offset = event.get("delegation", {}), event.get("offset_ms")
            if (
                item.get("target") != "client"
                or not isinstance(item.get("id"), str)
                or type(offset) is not int
                or offset < 0
            ):
                raise Rejected("unsupported Live delegation")
            previous = self.store.db.execute(
                "SELECT state FROM live_delegations WHERE session_id=? AND id=?",
                (self.session_id, item["id"]),
            ).fetchone()
            if previous:
                return
            if self.work.full():
                raise Rejected("Live delegation queue exceeded")
            self.store.db.execute(
                "INSERT INTO live_delegations VALUES(?,?,?,'admitted',NULL,NULL)",
                (self.session_id, item["id"], offset),
            )
            await self._send(
                {
                    "type": "session.thinking.append",
                    "event_id": identity(),
                    "delegation_id": item["id"],
                    "content": "",
                }
            )
            self.work.put_nowait((item["id"], offset))
        elif kind in {"session.commentary.appended", "session.thinking.appended"}:
            self.store.db.execute(
                "UPDATE live_deliveries SET state='context_accepted' "
                "WHERE id=? AND session_id=? AND state='submitted'",
                (event.get("client_event_id"), self.session_id),
            )

    def _usage(self, event):
        seconds = event.get("usage", {}).get("seconds")
        if (
            type(seconds) not in {float, int}
            or not math.isfinite(seconds)
            or seconds < self.seconds
        ):
            raise Rejected("invalid cumulative Live usage")
        self.seconds = float(seconds)
        self.store.db.execute(
            "UPDATE live_sessions SET seconds=? WHERE id=?",
            (self.seconds, self.session_id),
        )

    async def _delegate(self):
        while not self.closing or not self.work.empty():
            try:
                delegation_id, offset = await asyncio.wait_for(self.work.get(), 0.2)
            except TimeoutError:
                continue
            rows = self.store.db.execute(
                "SELECT f.session_id,f.role,f.start_ms,f.end_ms,f.text FROM live_fragments f "
                "JOIN live_sessions s ON s.id=f.session_id WHERE s.owner=? AND s.episode_id=? "
                "AND (s.id!=? OR f.start_ms<=?) ORDER BY s.created DESC,f.start_ms DESC LIMIT 32",
                (self.owner, self.episode_id, self.session_id, offset),
            ).fetchall()
            fragments, size = [], 0
            for row in rows:
                if size + len(row["text"].encode()) > 16000:
                    break
                fragments.append(
                    {**dict(row), "heard": None if row["role"] == "assistant" else True}
                )
                size += len(row["text"].encode())
            context = DelegationContext(
                self.session_id,
                delegation_id,
                offset,
                tuple(reversed(fragments)),
                *self.binding,
            )
            self.store.db.execute(
                "UPDATE live_delegations SET state='working' WHERE session_id=? AND id=?",
                (self.session_id, delegation_id),
            )
            backend_start = time.monotonic()
            try:
                async with asyncio.timeout(self.config.backend_seconds):
                    self.store.permission(self.owner, "cloud.reasoning", "near")
                    update = await self.backend(context)
                if not isinstance(update, ReleasedUpdate):
                    raise Rejected("backend must return selected release text")
                await self.publish(update, delegation_id=delegation_id)
                self.store.event(
                    self.episode_id,
                    "live.delegation_timing",
                    {
                        "session_id": self.session_id,
                        "delegation_id": delegation_id,
                        "backend_start_mono": backend_start,
                        "release_mono": time.monotonic(),
                        "update_kind": update.kind,
                        "spoken_result_latency": "unmeasured",
                    },
                )
                self.binding = (update.task_id, update.revision)
                self.store.db.execute(
                    "UPDATE live_delegations SET state='resolved',task_id=?,revision=? "
                    "WHERE session_id=? AND id=?",
                    (*self.binding, self.session_id, delegation_id),
                )
            except Exception:
                self.store.db.execute(
                    "UPDATE live_delegations SET state='unknown' WHERE session_id=? AND id=?",
                    (self.session_id, delegation_id),
                )
                self.store.event(
                    self.episode_id,
                    "live.delegation_unknown",
                    {"session_id": self.session_id, "delegation_id": delegation_id},
                )
            finally:
                self.work.task_done()

    async def publish(self, update, *, delegation_id=None):
        if not isinstance(update, ReleasedUpdate):
            raise Rejected("application release required")
        if update.task_id is not None:
            if (
                self.current_revision is None
                or self.current_revision(update.task_id) != update.revision
            ):
                raise Rejected("stale or unqualified task result")
        delivery = identity()
        self.store.db.execute(
            "INSERT INTO live_deliveries VALUES(?,?,?,?,?,?,?,?,?)",
            (
                delivery,
                self.session_id,
                delegation_id,
                update.task_id,
                update.revision,
                update.text,
                update.kind,
                "pending",
                self.store.clock.utc(),
            ),
        )
        if self.closing or not self.started.is_set():
            return delivery
        self._permission()
        self.audio.resume()
        kind = (
            "session.thinking.append"
            if update.kind == "progress"
            else "session.commentary.append"
        )
        async with self._send_lock:
            # Recheck after waiting for in-flight audio sends or a correction.
            self._permission()
            if self.closing:
                return delivery
            if (
                update.task_id is not None
                and self.current_revision(update.task_id) != update.revision
            ):
                self.store.db.execute(
                    "UPDATE live_deliveries SET state='stale' WHERE id=?", (delivery,)
                )
                raise Rejected("stale task result before transmission")
            # Persist before send; failure remains uncertain, never auto-replayed.
            self.store.db.execute(
                "UPDATE live_deliveries SET state='submitted' WHERE id=?", (delivery,)
            )
            await self.transport.send(
                {
                    "type": kind,
                    "event_id": delivery,
                    "delegation_id": delegation_id,
                    "content": update.text,
                }
            )
        self.last_activity = time.monotonic()
        return delivery

    def interrupt_speech(self):
        self.audio.interrupt()
        self.store.db.execute(
            "UPDATE live_deliveries SET state='interrupted' WHERE session_id=? "
            "AND state IN ('submitted','context_accepted')",
            (self.session_id,),
        )
        self.store.event(
            self.episode_id,
            "live.speech_interrupted",
            {
                "session_id": self.session_id,
                "played_frames": self.audio.played_frames,
                "task_cancelled": False,
            },
        )

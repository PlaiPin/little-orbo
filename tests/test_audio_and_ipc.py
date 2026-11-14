import asyncio
from types import SimpleNamespace as NS

import pytest

from orbo_runtime.audio import Playback
from orbo_runtime.cli import LocalService
from orbo_runtime.contracts import Rejected


class Output:
    def __init__(self, callback, **kwargs):
        self.callback, self.aborted, self.closed = callback, False, False
        self.task = None
        self.frames = bytearray()

    def start(self):
        async def tick():
            while True:
                data = bytearray(480 * 2)
                self.callback(data, 480, None, False)
                self.frames.extend(data)
                await asyncio.sleep(0.002)

        self.task = asyncio.create_task(tick())

    def abort(self):
        self.aborted = True
        self.task.cancel()

    def stop(self):
        pass

    def close(self):
        self.closed = True
        self.task.cancel()


async def test_playback_drains_pcm_and_labels_software_evidence():
    output = []

    def factory(**kwargs):
        output.append(Output(**kwargs))
        return output[-1]

    playback = Playback(stream_factory=factory)

    async def chunks():
        yield b"\x01\x02" * 1000
        yield b"\x03\x04" * 1000

    result = await playback.play(chunks())
    assert result["frames"] == 2000
    assert result["evidence"] == "software_playback"
    assert output[0].closed and not playback.queue and not playback.active


async def test_interrupt_clears_queued_audio_and_refuses_obsolete_chunk():
    playback = Playback(stream_factory=Output)
    pending = asyncio.Event()

    async def chunks():
        yield b"\x00\x01" * 24000
        pending.set()
        await asyncio.sleep(0.03)
        yield b"\x00\x02" * 10

    work = asyncio.create_task(playback.play(chunks()))
    await pending.wait()
    stream = playback.stream
    playback.interrupt()
    with pytest.raises(Rejected, match="obsolete"):
        await work
    assert stream.aborted and stream.closed
    assert not playback.queue and playback.queued_bytes == 0


async def test_stop_during_capture_never_transcribes(tmp_path, monkeypatch):
    service = LocalService(
        {
            "runtime": {
                "profile": "simulated",
                "owner": "test",
                "database": str(tmp_path / "state.db"),
            },
            "voice": {},
        }
    )
    started = asyncio.Event()
    submitted = []

    async def record(self, stop_event, **kwargs):
        started.set()
        await stop_event.wait()
        return b"fake-wav"

    from orbo_runtime.audio import Capture

    monkeypatch.setattr(Capture, "record", record)

    class Voice:
        generation = 0
        playback = NS()

        async def interrupt(self):
            self.generation += 1

        async def utterance(self, *args):
            submitted.append(args)

    service.voice = Voice()
    episode = service.store.episode("test", provenance="synthetic")
    service.store.grant(
        "test", "audio.capture", "local", True, service.store.clock.utc() + 30
    )
    work = asyncio.create_task(
        service.call({"method": "voice.capture", "args": {"episode_id": episode}})
    )
    await started.wait()
    await service.call({"method": "stop"})
    with pytest.raises((asyncio.CancelledError, Rejected)):
        await work
    assert not submitted
    assert service.capture_task is None
    service.store.close()


async def test_text_reasoning_without_voice_still_requires_grant(tmp_path, monkeypatch):
    from orbo_runtime import providers
    from orbo_runtime.providers import VerifiedReply

    replies = []

    class Reasoner:
        closed = False

        def __init__(self, *, model):
            assert model == "z-ai/glm-5.3-flash"

        async def complete(self, messages, tools):
            replies.append(messages)
            return VerifiedReply(
                {"content": "Text-only fixture reply"}, {"fixture": True}, {}
            )

        async def close(self):
            self.closed = True

    def unexpected_audio(**kwargs):
        pytest.fail("Text-only reasoning initialized a voice provider")

    monkeypatch.setattr(providers, "NearReasoner", Reasoner)
    monkeypatch.setattr(providers, "FishSynthesizer", unexpected_audio)
    monkeypatch.setattr(providers, "NearTranscriber", unexpected_audio)
    service = LocalService(
        {
            "runtime": {
                "profile": "simulated",
                "owner": "test",
                "database": str(tmp_path / "state.db"),
            },
            "near": {"enabled": True, "model": "z-ai/glm-5.3-flash"},
            "voice": {"enabled": False},
        }
    )
    try:
        assert service.voice is None
        episode = service.store.episode("test", provenance="synthetic")
        request = {
            "method": "conversation.text",
            "args": {"episode_id": episode, "text": "Hello"},
        }
        with pytest.raises(Rejected):
            await service.call(request)
        assert not replies
        service.store.grant(
            "test", "cloud.reasoning", "near", True, service.store.clock.utc() + 30
        )
        assert await service.call(request) == {
            "text": "Text-only fixture reply",
            "played": False,
        }
        kinds = [
            r[0]
            for r in service.store.db.execute(
                "SELECT kind FROM events WHERE episode_id=?", (episode,)
            )
        ]
        assert "reasoning.verified" in kinds and "turn.completed" in kinds
        assert len(replies) == 1
    finally:
        await service.close()
    assert service.reasoner.closed


def test_voice_requires_reasoning_before_opening_database(tmp_path):
    database = tmp_path / "state.db"
    with pytest.raises(Rejected, match="voice requires near.enabled"):
        LocalService(
            {
                "runtime": {"owner": "test", "database": str(database)},
                "near": {"enabled": False},
                "voice": {"enabled": True},
            }
        )
    assert not database.exists()

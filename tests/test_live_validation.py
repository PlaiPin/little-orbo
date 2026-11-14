import asyncio
import json
import os
from pathlib import Path
import stat
import wave
import io

import pytest

from orbo_runtime import live_validation as live
from orbo_runtime.providers import VerifiedReply


@pytest.fixture
def configured(tmp_path):
    directory = live.initialize(tmp_path.resolve() / "validation")
    (directory / "credentials.env").write_text(
        "".join(
            name + "=" + json.dumps("synthetic-private-" + name) + "\n"
            for name in live.FIELDS
        )
    )
    return directory


def test_templates_are_private_idempotent_and_never_overwrite(configured):
    before = {p.name: p.read_bytes() for p in configured.iterdir() if p.is_file()}
    live.initialize(configured)
    assert {
        p.name: p.read_bytes() for p in configured.iterdir() if p.is_file()
    } == before
    assert stat.S_IMODE(configured.stat().st_mode) == 0o700
    assert all(
        stat.S_IMODE(p.stat().st_mode) == 0o600
        for p in configured.iterdir()
        if p.is_file()
    )


def test_preflight_and_run_without_execute_never_create_clients(
    configured, monkeypatch, capsys
):
    def forbidden(**kwargs):
        raise AssertionError("must not initialize network client")

    monkeypatch.setattr(live, "NearReasoner", forbidden)
    # FishSynthesizer supplies static model validation, so block only construction.
    monkeypatch.setattr(live.FishSynthesizer, "__init__", forbidden)
    assert live.main(["check", "--directory", str(configured)]) == 0
    assert live.main(["run", "--directory", str(configured)]) == 0
    assert not list((configured / "reports").iterdir())
    output = capsys.readouterr().out
    assert "executed" in output and "synthetic-private" not in output


def test_empty_files_block_run_even_when_environment_has_keys(
    tmp_path, monkeypatch, capsys
):
    directory = live.initialize(tmp_path.resolve() / "empty")
    monkeypatch.setenv("NEARAI_API_KEY", "ambient-secret")
    assert live.main(["run", "--execute", "--directory", str(directory)]) == 2
    assert "ambient-secret" not in capsys.readouterr().out
    assert not list((directory / "reports").iterdir())


def test_permissions_symlinks_and_hardlinks_are_rejected(configured, tmp_path):
    path = configured / "credentials.env"
    path.chmod(0o644)
    with pytest.raises(live.SetupError):
        live.load_setup(configured, "both", False)
    path.chmod(0o600)
    other = tmp_path.resolve() / "key-link"
    os.link(path, other)
    with pytest.raises(live.SetupError):
        live.load_setup(configured, "both", False)
    other.unlink()
    path.unlink()
    path.symlink_to(configured / "config.json")
    with pytest.raises(OSError):
        live.load_setup(configured, "both", False)


class Providers:
    def __init__(self, *, failure=None, block=False, oversized=False, mismatch=False):
        self.calls, self.closed, self.args = [], [], []
        self.failure, self.block, self.oversized, self.mismatch = (
            failure,
            block,
            oversized,
            mismatch,
        )
        self.entered = asyncio.Event()
        self.stream_closed = False
        parent = self

        class Near:
            def __init__(self, **kwargs):
                parent.args.append(kwargs)

            async def complete(self, messages, tools):
                parent.calls.append("near")
                assert not tools
                parent.entered.set()
                if parent.block:
                    await asyncio.Event().wait()
                if parent.failure:
                    raise RuntimeError(parent.failure)
                return VerifiedReply(
                    {"content": "ORBO_LIVE_OK"},
                    {
                        "e2ee": True,
                        "signature_kind": "provider_tee",
                        "response_id": "fixture",
                    },
                    {
                        "total_tokens": 8,
                        "untrusted_field": "synthetic-private-NEARAI_API_KEY",
                    },
                )

            async def close(self):
                parent.closed.append("near")

        class Fish:
            def __init__(self, **kwargs):
                parent.args.append(kwargs)
                self.last_balance = {"credit": "2.00"}

            async def synthesize(self, text):
                parent.calls.append("fish")
                assert text == live.PHRASE
                try:
                    yield b"\0\0" * (
                        live.MAX_AUDIO_BYTES if parent.oversized else 16000
                    )
                finally:
                    parent.stream_closed = True

            async def balance(self):
                parent.calls.append("balance")
                return {"credit": "1.99"}

            async def close(self):
                parent.closed.append("fish")

        class Transcriber:
            def __init__(self, **kwargs):
                parent.args.append(kwargs)

            async def transcribe(self, data):
                parent.calls.append("transcribe")
                with wave.open(io.BytesIO(data)) as wav:
                    assert wav.getframerate() == 16000 and wav.getnchannels() == 1
                return ("wrong phrase" if parent.mismatch else live.PHRASE), {}

            async def close(self):
                parent.closed.append("transcribe")

        self.factories = Near, Fish, Transcriber


async def run(configured, providers, *, transcribe=False):
    directory, config, credentials, missing = live.load_setup(
        configured, "both", transcribe
    )
    assert not missing
    return await live.run_checks(
        directory,
        config,
        credentials,
        "both",
        transcribe,
        factories=providers.factories,
    )


async def test_exact_bounded_batch_keeps_secrets_and_content_out_of_report(configured):
    providers = Providers()
    path, status = await run(configured, providers, transcribe=True)
    assert status == "passed"
    assert providers.calls == ["near", "fish", "balance", "transcribe"]
    assert providers.closed == ["near", "fish", "transcribe"]
    text = path.read_text()
    assert "synthetic-private" not in text and live.PHRASE not in text
    report = json.loads(text)
    assert report["provenance"] == "fixture"
    assert report["checks"][0]["verified_response"]
    assert report["checks"][1]["played"] is False
    assert report["checks"][2]["expected_phrase_match"] is True
    assert all("api_key" in args for args in providers.args)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(configured.rglob("*.wav"))


async def test_failure_stops_batch_without_retry_and_suppresses_exception_contents(
    configured,
):
    providers = Providers(failure="synthetic-private-NEARAI_API_KEY bearer secrets")
    path, status = await run(configured, providers)
    assert status == "failed"
    assert providers.calls == ["near"] and providers.closed == ["near"]
    assert "bearer secrets" not in path.read_text()
    assert "synthetic-private" not in path.read_text()
    assert json.loads(path.read_text())["checks"][0]["outcome"] == "unknown"


async def test_cancel_is_durable_and_second_batch_is_locked_out(configured):
    providers = Providers(block=True)
    task = asyncio.create_task(run(configured, providers))
    await providers.entered.wait()
    pending = json.loads(next((configured / "reports").glob("*.json")).read_text())
    assert pending["checks"][0]["status"] == "in_progress"
    with pytest.raises(live.SetupError, match="Another"):
        await run(configured, Providers())
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    saved = json.loads(next((configured / "reports").glob("*.json")).read_text())
    assert saved["status"] == "cancelled"
    assert providers.closed == ["near"]
    assert len(list((configured / "reports").glob("*.json"))) == 1


async def test_transcription_is_opt_in_and_oversized_audio_never_uploads(configured):
    providers = Providers()
    await run(configured, providers)
    assert "transcribe" not in providers.calls
    oversized = Providers(oversized=True)
    _, status = await run(configured, oversized, transcribe=True)
    assert status == "failed" and "transcribe" not in oversized.calls
    assert oversized.closed == ["near", "fish"]
    assert oversized.stream_closed


async def test_mismatched_transcript_is_not_a_pass(configured):
    providers = Providers(mismatch=True)
    path, status = await run(configured, providers, transcribe=True)
    assert status == "failed"
    assert json.loads(path.read_text())["checks"][-1]["expected_phrase_match"] is False


def test_wrong_config_does_not_echo_accidentally_pasted_key(configured, capsys):
    (configured / "config.json").write_text("synthetic-private-NEARAI_API_KEY")
    assert live.main(["check", "--directory", str(configured)]) == 2
    assert "synthetic-private" not in capsys.readouterr().out


def test_explicit_reasoner_key_does_not_use_ambient_credentials(monkeypatch):
    import nearai_inference_sdk
    from orbo_runtime.providers import NearReasoner

    captured = []

    def constructor(key, **kwargs):
        captured.append((key, kwargs))
        return object()

    monkeypatch.setattr(nearai_inference_sdk, "InferenceClient", constructor)
    monkeypatch.setenv("NEARAI_API_KEY", "ambient-key-must-not-win")
    NearReasoner(api_key="private-file-key")
    assert captured == [
        ("private-file-key", {"e2ee": True, "attestation_cache_time_to_live_ms": 0})
    ]


def test_default_directory_is_project_local():
    assert (
        live.DEFAULT_DIRECTORY
        == Path(__file__).resolve().parents[1] / ".live-validation"
    )


def test_env_values_are_literal_and_never_executed(configured, tmp_path):
    marker = tmp_path / "must-not-exist"
    value = "$(touch " + str(marker) + ")"
    (configured / "credentials.env").write_text(
        "NEARAI_API_KEY=" + json.dumps(value) + "\n"
    )
    assert live.read_credentials(configured)["NEARAI_API_KEY"] == value
    assert not marker.exists()
    (configured / "credentials.env").write_text(
        "NEARAI_API_KEY=one\nNEARAI_API_KEY=two\n"
    )
    with pytest.raises(live.SetupError):
        live.read_credentials(configured)

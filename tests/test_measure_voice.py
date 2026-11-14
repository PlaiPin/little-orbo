"""The real-device runner retains incomplete-run evidence; this is a fixture."""

import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


async def test_measurement_retains_failure_and_failed_finalization(
    tmp_path, monkeypatch
):
    path = Path(__file__).parents[1] / "tools/measure-voice.py"
    spec = importlib.util.spec_from_file_location("measure_voice", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(
        module,
        "inventory",
        lambda: {"machine": "aarch64", "device_model": "Raspberry Pi 5 FIXTURE"},
    )
    monkeypatch.setattr(module, "load", lambda: {"fixture": True})

    async def request(socket, method, args):
        if method == "episode.start":
            return {"ok": True, "result": {"episode_id": "fixture"}}
        if method == "voice.capture":
            raise TimeoutError("fixture")
        return {"ok": False, "error": "fixture unavailable"}

    monkeypatch.setattr(module, "request", request)
    output = tmp_path / "report.json"
    with pytest.raises(SystemExit, match="incomplete"):
        await module.main(
            SimpleNamespace(
                socket="unused",
                turns=30,
                capture_seconds=1,
                ros_load="fixture",
                output=output,
            )
        )
    report = json.loads(output.read_text())
    assert report["requested_turns"] == 30 and report["unattempted_count"] == 29
    assert report["attempts"][0]["ok"] is False
    assert report["finalization"]["episode.end"]["ok"] is False
    assert report["p95_seconds"] is None and report["completed_count"] == 0
    assert output.stat().st_mode & 0o777 == 0o600

#!/usr/bin/env python3
"""Run actual Pi microphone turns; store timings and load, never audio/transcript."""

import argparse
import asyncio
import json
import platform
from pathlib import Path
import time

from orbo_runtime.cli import request


def inventory():
    model_path = Path("/proc/device-tree/model")
    model = model_path.read_text().strip("\0\n") if model_path.exists() else "unknown"
    return {
        "machine": platform.machine(),
        "platform": platform.platform(),
        "device_model": model,
    }


def load():
    data = {"mono": time.monotonic()}
    for name, path in [
        ("loadavg", "/proc/loadavg"),
        ("thermal_millidegrees", "/sys/class/thermal/thermal_zone0/temp"),
    ]:
        try:
            data[name] = Path(path).read_text().strip()
        except OSError:
            data[name] = None
    return data


async def main(args):
    device = inventory()
    if device["machine"] != "aarch64" or "Raspberry Pi 5" not in device["device_model"]:
        raise SystemExit(
            "This acceptance runner requires the actual Raspberry Pi 5; fixtures use unit tests."
        )
    if args.turns < 30 or not 1 <= args.capture_seconds <= 30:
        raise SystemExit("At least 30 turns, capture 1–30 seconds")
    start = await request(args.socket, "episode.start", {})
    if not start["ok"]:
        raise SystemExit(start["error"])
    episode = start["result"]["episode_id"]
    records = []
    finalization = {}
    interrupted = None
    try:
        for index in range(args.turns):
            print(
                f"Turn {index + 1}/{args.turns}: speak during the next {args.capture_seconds} seconds.",
                flush=True,
            )
            sample = {"index": index, "load_before": load()}
            records.append(sample)
            try:
                response = await request(
                    args.socket,
                    "voice.capture",
                    {"episode_id": episode, "max_seconds": args.capture_seconds},
                )
            except Exception as error:
                sample.update(
                    ok=False, error_class=type(error).__name__, load_after=load()
                )
                raise
            sample["ok"] = response["ok"]
            if not response["ok"]:
                sample["error_class"] = response.get("error")
            sample["load_after"] = load()
    except Exception as error:
        interrupted = type(error).__name__
    finally:
        for method, params in [
            ("stop", {}),
            (
                "episode.end",
                {
                    "episode_id": episode,
                    "outcome": "completed"
                    if len(records) == args.turns and all(r["ok"] for r in records)
                    else "unknown",
                },
            ),
        ]:
            try:
                response = await request(args.socket, method, params)
                finalization[method] = {"ok": response["ok"]}
                if not response["ok"]:
                    finalization[method]["error"] = response.get("error")
            except Exception as error:
                finalization[method] = {
                    "ok": False,
                    "error_class": type(error).__name__,
                }
    cursor, timings = 0, []
    while True:
        try:
            reply = await request(args.socket, "journal", {"after": cursor})
        except Exception as error:
            finalization["journal"] = {"ok": False, "error_class": type(error).__name__}
            break
        if not reply["ok"]:
            finalization["journal"] = {"ok": False}
            break
        rows = reply["result"]
        if not rows:
            break
        cursor = rows[-1]["seq"]
        for row in rows:
            if row["episode_id"] == episode and row["kind"] in {
                "voice.completed",
                "voice.failed",
            }:
                payload = json.loads(row["payload"])
                timing = payload.get("timing", {})
                latency = timing.get("playback_first", float("nan")) - timing.get(
                    "utterance_end", float("nan")
                )
                timings.append(
                    {
                        "kind": row["kind"],
                        "boot": row["boot"],
                        "timing": timing,
                        "latency_seconds": latency if latency == latency else None,
                    }
                )
    values = sorted(
        t["latency_seconds"]
        for t in timings
        if t["kind"] == "voice.completed" and t["latency_seconds"] is not None
    )

    def percentile(p):
        return (
            values[max(0, min(len(values) - 1, int((len(values) - 1) * p)))]
            if values
            else None
        )

    report = {
        "schema": "orbo.voice-qualification.v1",
        "device": device,
        "episode_id": episode,
        "ros_load_description": args.ros_load,
        "ros_load_is_operator_declared": True,
        "attempts": records,
        "requested_turns": args.turns,
        "unattempted_count": args.turns - len(records),
        "interrupted_error_class": interrupted,
        "finalization": finalization,
        "timings": timings,
        "completed_count": len(values),
        "p50_seconds": percentile(0.5),
        "p95_seconds": percentile(0.95),
        "missing_timing_count": max(0, len(records) - len(timings)),
        "measurement": "utterance-end to first PortAudio callback frames; external acoustic latency not measured",
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    # Create privately from the first byte, including on interrupted runs.
    import os

    with os.fdopen(
        os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "w"
    ) as output:
        json.dump(report, output, indent=2, allow_nan=False)
    args.output.chmod(0o600)
    print(f"Report: {args.output}; {len(values)}/{args.turns} completed turns")
    if (
        interrupted
        or len(values) < args.turns
        or any(not value["ok"] for value in finalization.values())
    ):
        raise SystemExit("Qualification incomplete; failures retained in report")


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--socket", default="/run/orbo/runtime.sock")
    p.add_argument("--turns", type=int, default=30)
    p.add_argument("--capture-seconds", type=float, default=5)
    p.add_argument(
        "--ros-load",
        required=True,
        help="Actual active nodes, topics/rates, motion state and scenario",
    )
    p.add_argument("--output", type=Path, required=True)
    asyncio.run(main(p.parse_args()))

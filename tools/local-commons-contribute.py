#!/usr/bin/env python3
"""Synthetic runtime -> native contributor/issuer -> ingest -> consumer test."""

import argparse
import asyncio
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

import httpx

from orbo_runtime.commons import Commons, ContributorIPC
from orbo_runtime.consumer import EvidenceConsumer
from orbo_runtime.contracts import ManualClock, Rejected
from orbo_runtime.storage import Store
from orbo_runtime.traces import Exports, trajectory

SPEC = importlib.util.spec_from_file_location(
    "issuer", Path(__file__).with_name("local-commons-issuer.py")
)
issuer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(issuer)
local = issuer.local


async def run(directory):
    config = local.read_config(directory)
    local.health(directory)
    state = directory / "contributor"
    enrollment = json.loads((state / "contributor.json").read_text())
    if enrollment["tenant_id"] != config["tenant"] or enrollment[
        "issuer_url"
    ] != local.origin(config):
        raise RuntimeError("local enrollment does not match the selected instance")
    if (state / "daemon.sock").exists():
        raise RuntimeError(
            "contributor daemon already running; do not overwrite its settings"
        )
    run_dir = directory / ("contribution-" + str(time.time_ns()))
    run_dir.mkdir(mode=0o700)
    staging = run_dir / "staging"
    staging.mkdir(mode=0o700)
    original = (state / "daemon-settings.json").read_bytes()
    settings = json.loads(original)
    settings.update(
        {
            "trajectory_source": {"mode": "watch", "path": str(staging)},
            "poll_interval_secs": 1,
            "quiescence_secs": 1,
            "approval_hold_secs": 1,
        }
    )
    local.write_private(state / "daemon-settings.json", settings)
    clock = ManualClock(now=time.time())
    store = Store(run_dir / "runtime.sqlite3", clock)
    owner, recipient = "synthetic-local-owner", "local-commons"
    episode = store.episode(owner, provenance="synthetic")
    store.event(
        episode,
        "turn.started",
        {"text": "Please run the synthetic reminder acknowledgment test."},
    )
    store.event(
        episode, "attention.awaiting_ack", {"generation": 1, "reminder_id": "synthetic"}
    )
    clock.advance(1.25)
    store.event(
        episode, "attention.acknowledged", {"generation": 1, "reminder_id": "synthetic"}
    )
    store.event(
        episode, "turn.completed", {"text": "The synthetic reminder was acknowledged."}
    )
    store.end_episode(episode, owner, "completed")
    exports = Exports(store)
    export = exports.prepare(owner, episode, "attention_timing", recipient)
    digest = export["sha256"]
    for scope in ("contribute", "commons.review"):
        store.grant(owner, scope, recipient, True, clock.utc() + 600)
    exports.approve(owner, export["export_id"], digest, expires=clock.utc() + 600)
    content = trajectory(export["package"])
    local.write_private(staging / "synthetic-task.jsonl", content)
    local.write_private(run_dir / "approved-export.json", export)
    report = {
        "schema": "orbo.local-contribution-report.v1",
        "build_commit": config["build_commit"],
        "origin": local.origin(config),
        "provenance": "synthetic",
        "source_digest": digest,
        "checks": {"runtime_export_and_digest_approval": "passed"},
        "not_tested": [
            "NEAR privacy classifier",
            "production scoring or reward payments",
            "physical robot outcomes",
            "image delivery",
        ],
    }
    log = (run_dir / "daemon.log").open("ab")
    binary = Path(config["release"]) / "bin/trace-commons-contributor"
    process = subprocess.Popen(
        [str(binary), "--config-dir", str(state), "daemon", "run"],
        cwd=directory,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUST_LOG": "warn",
            "SSL_CERT_FILE": str(directory / "localhost.crt"),
            "TRACE_COMMONS_ALLOWED_HOSTS": "127.0.0.1",
        },
        stdout=log,
        stderr=subprocess.STDOUT,
        umask=0o077,
    )
    ipc = ContributorIPC(state / "daemon.sock")
    commons = Commons(store, ipc)
    try:
        for _ in range(100):
            if process.poll() is not None:
                raise RuntimeError("daemon exited; inspect run log")
            if ipc.path.exists():
                break
            await asyncio.sleep(0.1)
        setup = await ipc.call("get_setup_status", {})
        if not setup["complete"]:
            await ipc.call(
                "complete_setup",
                {"expected_scope": setup["scope"], "scopes": ["debugging_evaluation"]},
            )
        for _ in range(60):
            pending = await ipc.call("list_pending", {})
            rows = [
                row
                for row in pending["pending"]
                if row["session_hash"]
                == "sha256:" + hashlib.sha256(content).hexdigest()
            ]
            if rows:
                break
            await asyncio.sleep(1)
        else:
            local.write_private(
                run_dir / "daemon-status.json", await ipc.call("status", {})
            )
            raise RuntimeError("synthetic trajectory was not queued")
        entry = rows[0]["entry_id"]
        preview = await commons.review(owner, export["export_id"], entry)
        local.write_private(run_dir / "reviewed-events.json", preview["body"].encode())
        report["checks"]["native_parser_redactor_and_exact_preview"] = "passed"
        approved = await commons.submit_reviewed(
            owner,
            export["export_id"],
            entry,
            preview["envelope_digest"],
            preview["body_digest"],
        )
        assert approved["state"] == "approved", approved
        report["checks"]["exact_reviewed_daemon_approval"] = "passed"
        for _ in range(90):
            status = await commons.status(owner, export["export_id"], entry)
            if status["submission_ids"]:
                break
            await asyncio.sleep(1)
        else:
            local.write_private(
                run_dir / "daemon-status.json", await ipc.call("status", {})
            )
            local.write_private(run_dir / "entry-status.json", status)
            raise RuntimeError("approved trace has not uploaded")
        submission = status["submission_ids"][0]
        report["submission_id"] = submission
        local.write_private(run_dir / "upload-status.json", status)
        report["checks"]["native_device_signed_claim_and_upload"] = "passed"
        tokens = json.loads((directory / "credentials.json").read_text())
        async with httpx.AsyncClient(
            base_url=local.origin(config),
            verify=local.tls_context(directory),
            timeout=20,
            trust_env=False,
        ) as client:
            response = await client.post(
                f"/v1/review/{submission}/decision",
                headers={"Authorization": "Bearer " + tokens["reviewer"]},
                json={
                    "decision": "approve",
                    "reason": "Synthetic local end-to-end test; no commercial reward",
                    "credit_points_pending": 0.0,
                },
            )
            response.raise_for_status()
            assert response.json()["status"] == "accepted"
            local.write_private(run_dir / "review-receipt.json", response.json())
        report["checks"]["server_review"] = "accepted_zero_credit"
        consumer = EvidenceConsumer(
            local.origin(config),
            token=tokens["export_worker"],
            client=httpx.AsyncClient(
                verify=local.tls_context(directory), timeout=20, trust_env=False
            ),
        )
        try:
            result = await consumer.retrieve(
                "attention_timing",
                [digest],
                privacy_risk="medium",
                submission_ids=[submission],
                limit=1,
            )
            assert (
                result["complete"]
                and result["tasks"][digest]["package"] == export["package"]
            )
            assert result["tasks"][digest]["analysis"]["ack_latency_seconds"] == [1.25]
            local.write_private(run_dir / "consumer-report.json", result)
            report["checks"]["consumer_exact_digest_and_analysis"] = (
                "passed; acknowledgment 1.25 seconds"
            )
            try:
                await consumer.retrieve(
                    "attention_timing",
                    ["0" * 64],
                    privacy_risk="medium",
                    submission_ids=[submission],
                    limit=1,
                )
            except Rejected:
                report["checks"]["wrong_digest"] = "rejected"
            else:
                raise RuntimeError("consumer accepted an incorrect digest")
        finally:
            await consumer.close()
        report["status"] = "passed"
        return {"report": str(run_dir / "report.json"), **report}
    except BaseException as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        local.write_private(run_dir / "report.json", report)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        log.close()
        local.write_private(state / "daemon-settings.json", original)
        store.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=local.DEFAULT)
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run(args.directory.resolve())), indent=2))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""Bounded local smoke of real packaged Commons processes using synthetic state.

No enrollment or trace uploads. Tests route/auth and owner-private daemon IPC;
accepted evidence and production privacy/scoring remain live E2E checks.
"""

import argparse
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import uuid

import httpx

from orbo_runtime.commons import ContributorIPC
from orbo_runtime.commons_deployment import REQUIRED_METHODS

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "packager", ROOT / "tools/package-commons.py"
)
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


async def smoke(release):
    manifest = packager.verify_release(release)
    processes = []
    with tempfile.TemporaryDirectory(prefix="orbo-cm-", dir="/tmp") as tmp:
        directory = Path(tmp)
        config = directory / "contributor"
        config.mkdir(mode=0o700)
        settings = (
            ROOT / "deploy/trace-commons/daemon-settings.json.example"
        ).read_bytes()
        (config / "daemon-settings.json").write_bytes(settings)
        (config / "daemon-settings.json").chmod(0o600)
        # Fresh process environment intentionally carries no provider/account keys.
        env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "RUST_LOG": "error"}
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
        token = secrets.token_hex(32)
        server_env = {
            **env,
            "TRACE_COMMONS_BIND": f"127.0.0.1:{port}",
            "TRACE_COMMONS_DATA_DIR": str(directory / "server"),
            "TRACE_COMMONS_TENANT_TOKENS": "orbo-smoke:export_worker:" + token,
        }
        try:
            with (directory / "server.log").open("wb") as log:
                server = subprocess.Popen(
                    [str(release / "bin/trace-commons-ingest")],
                    env=server_env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(server)
            with (directory / "daemon.log").open("wb") as log:
                daemon = subprocess.Popen(
                    [
                        str(release / "bin/trace-commons-contributor"),
                        "--config-dir",
                        str(config),
                        "daemon",
                        "run",
                        "--dry-run",
                    ],
                    env=env,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                )
                processes.append(daemon)
            origin = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 45
            async with httpx.AsyncClient(
                timeout=5, trust_env=False, follow_redirects=False
            ) as client:
                while True:
                    if any(p.poll() is not None for p in processes):
                        raise RuntimeError("a Commons process exited during startup")
                    try:
                        health = (await client.get(origin + "/health")).json()
                        if (config / "daemon.sock").exists():
                            break
                    except (httpx.HTTPError, ValueError):
                        pass
                    if time.monotonic() >= deadline:
                        raise TimeoutError("local Commons startup timed out")
                    await asyncio.sleep(0.1)
                if health.get("build_commit") != manifest["build_commit"]:
                    raise RuntimeError("unexpected server build")
                path = origin + "/v1/datasets/task-evidence"
                denied = await client.get(path)
                if denied.status_code not in (401, 403):
                    raise RuntimeError("unauthenticated custom export was not denied")
                selected = str(uuid.uuid4())
                response = await client.get(
                    path,
                    headers={"Authorization": "Bearer " + token},
                    params={
                        "purpose": "orbo.embodied_process.v1",
                        "status": "accepted",
                        "privacy_risk": "low",
                        "consent_scope": "debugging-evaluation",
                        "limit": 1,
                        "submission_ids": selected,
                    },
                )
                if (
                    response.status_code != 200
                    or response.headers.get("x-orbo-task-selection") != selected
                    or response.json().get("item_count") != 0
                ):
                    raise RuntimeError("authenticated exact-selection route failed")
            ipc = ContributorIPC(config / "daemon.sock")
            hello = await ipc.call("hello", {})
            status = await ipc.call("status", {})
            if not REQUIRED_METHODS <= set(hello.get("methods", [])):
                raise RuntimeError("daemon lacks required patched IPC methods")
            if status.get("logged_in") is not False:
                raise RuntimeError("isolated daemon unexpectedly enrolled")
            return {
                "schema": "orbo.commons-process-smoke.v1",
                "build_commit": manifest["build_commit"],
                "server_health": "passed",
                "unauthenticated_export": "denied",
                "exact_selection_route": "passed_empty_dataset",
                "patched_daemon_ipc": "passed",
                "enrollment": "not_run",
                "uploads": "not_run",
                "accepted_delivery": "not_run",
            }
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            for process in reversed(processes):
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--release", type=Path, required=True)
    args = parser.parse_args()
    try:
        report = asyncio.run(
            asyncio.wait_for(smoke(args.release.resolve()), timeout=90)
        )
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "stage": "local_process_smoke",
                }
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()

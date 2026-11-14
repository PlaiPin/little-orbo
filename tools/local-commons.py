#!/usr/bin/env python3
"""Isolated loopback Commons development instance and synthetic HTTP round trip.

Uses the verified Orbo binaries, real ingest/review/export routes and consumer.
No provider keys, private traces, production enrollment or commercial scoring.
"""

import argparse
import asyncio
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from orbo_runtime.consumer import EvidenceConsumer
from orbo_runtime.contracts import canonical
from orbo_runtime.traces import reconstruct_commons

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "artifacts/local-commons"
SPEC = importlib.util.spec_from_file_location(
    "commons_packager", ROOT / "tools/package-commons.py"
)
packager = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(packager)


def write_private(path, value):
    data = (
        value
        if isinstance(value, bytes)
        else (json.dumps(value, indent=2) + "\n").encode()
    )
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
    path.chmod(0o600)


def read_config(directory):
    config = json.loads((directory / "config.json").read_text())
    if config["schema"] != "orbo.local-commons.v1":
        raise RuntimeError("unexpected local configuration")
    return config


def origin(config):
    return f"https://127.0.0.1:{config['https_port']}"


def tls_context(directory):
    return ssl.create_default_context(cafile=str(directory / "localhost.crt"))


def initialize(directory, release, port, backend):
    if directory.exists():
        raise RuntimeError(
            "state directory already exists; reuse start/test or select a fresh --directory"
        )
    if len(os.fsencode(directory / "contributor/daemon.sock")) >= 104:
        raise RuntimeError(
            "state path is too long for the contributor Unix socket; select a shorter --directory"
        )
    manifest = packager.verify_release(release)
    if port == backend:
        raise RuntimeError("HTTPS and backend ports must differ")
    for value in (port, backend):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", value))
    directory.mkdir(parents=True, mode=0o700)
    directory.chmod(0o700)
    # Upstream uses dotenvy, which otherwise walks up to the project's .env.
    write_private(
        directory / ".env",
        b"# Synthetic-only instance: do not load ancestor provider credentials.\n",
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "Orbo local Commons test")]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            False,
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
        .sign(key, hashes.SHA256())
    )
    server_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    server_subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
    server_cert = (
        x509.CertificateBuilder()
        .subject_name(server_subject)
        .issuer_name(subject)
        .public_key(server_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
                    x509.DNSName("localhost"),
                ]
            ),
            False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
        .sign(key, hashes.SHA256())
    )
    write_private(
        directory / "localhost.key",
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    write_private(
        directory / "localhost.crt", cert.public_bytes(serialization.Encoding.PEM)
    )
    write_private(
        directory / "server.crt", server_cert.public_bytes(serialization.Encoding.PEM)
    )
    write_private(
        directory / "credentials.json",
        {
            role: secrets.token_hex(32)
            for role in ("contributor", "reviewer", "export_worker", "foreign_export")
        },
    )
    write_private(
        directory / "config.json",
        {
            "schema": "orbo.local-commons.v1",
            "release": str(release),
            "build_commit": manifest["build_commit"],
            "https_port": port,
            "backend_port": backend,
            "tenant": "orbo-local-synthetic",
            "data_kind": "synthetic_only",
        },
    )
    return {
        "initialized": str(directory),
        "origin": f"https://127.0.0.1:{port}",
        "external_credentials_required": [],
        "certificate_trust": "process-local only",
    }


def server_environment(directory, config):
    tokens = json.loads((directory / "credentials.json").read_text())
    entries = [
        f"{config['tenant']}:{role}:{token}"
        for role, token in tokens.items()
        if role != "foreign_export"
    ]
    entries.append("orbo-other-tenant:export_worker:" + tokens["foreign_export"])
    # Deliberately exclude the project .env and ambient provider credentials.
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RUST_LOG": "warn",
        "TRACE_COMMONS_BIND": f"127.0.0.1:{config['backend_port']}",
        "TRACE_COMMONS_DATA_DIR": str(directory / "server"),
        "TRACE_COMMONS_TENANT_TOKENS": ",".join(entries),
        "TRACE_COMMONS_NOVELTY_UTILITY_CREDIT_POINTS_DELTA": "0",
    }
    if config.get("issuer"):
        env.update(
            {
                "TRACE_COMMONS_SIGNED_TOKEN_EDDSA_KEYSET_FILE": str(
                    directory / "issuer/keyset.json"
                ),
                "TRACE_COMMONS_SIGNED_TOKEN_ISSUER": origin(config),
                "TRACE_COMMONS_SIGNED_TOKEN_AUDIENCE": "orbo-local-upload",
            }
        )
    return env


def serve(directory):
    os.umask(0o077)
    config = read_config(directory)
    release = Path(config["release"])
    packager.verify_release(release)
    issuer_process = None
    issuer_log = None
    if config.get("issuer"):
        issuer_dir = directory / "issuer"
        issuer_env = json.loads((issuer_dir / "environment.json").read_text())
        binary = issuer_dir / "trace-commons-upload-claim-issuer"
        if (
            hashlib.sha256(binary.read_bytes()).hexdigest()
            != config["issuer"]["binary_sha256"]
        ):
            raise RuntimeError("issuer binary changed")
        issuer_log = (issuer_dir / "issuer.log").open("ab")
        issuer_process = subprocess.Popen(
            [str(binary)],
            cwd=issuer_dir,
            env=issuer_env,
            stdout=issuer_log,
            stderr=subprocess.STDOUT,
        )
    backend_log = (directory / "ingest.log").open("ab")
    process = subprocess.Popen(
        [str(release / "bin/trace-commons-ingest")],
        cwd=directory,
        env=server_environment(directory, config),
        stdout=backend_log,
        stderr=subprocess.STDOUT,
    )

    class Proxy(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def forward(self):
            if (
                not self.path.startswith("/")
                or self.path.startswith("//")
                or self.headers.get("Transfer-Encoding")
            ):
                self.send_error(400)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 <= length <= 2_000_000:
                    self.send_error(413)
                    return
                body = self.rfile.read(length)
                headers = {
                    name: value
                    for name, value in self.headers.items()
                    if name.lower()
                    in {
                        "authorization",
                        "content-type",
                        "accept",
                        "x-trace-device-key-id",
                        "x-trace-device-signature",
                    }
                }
                issuer_route = self.path.split("?", 1)[0] in {
                    "/v1/enroll",
                    "/v1/onboard",
                    "/v1/trace-upload-claim",
                    "/.well-known/trace-commons-ed25519-keyset.json",
                }
                port = (
                    config["issuer"]["port"]
                    if issuer_route and config.get("issuer")
                    else config["backend_port"]
                )
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                try:
                    connection.request(
                        self.command, self.path, body=body, headers=headers
                    )
                    response = connection.getresponse()
                    result = response.read(16_777_217)
                    if len(result) > 16_777_216:
                        self.send_error(502)
                        return
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in {
                            "connection",
                            "transfer-encoding",
                            "content-length",
                        }:
                            self.send_header(name, value)
                    self.send_header("Content-Length", str(len(result)))
                    self.end_headers()
                    self.wfile.write(result)
                finally:
                    connection.close()
            except (ValueError, OSError, http.client.HTTPException):
                self.send_error(502)

        do_GET = do_POST = do_DELETE = forward

    httpd = None
    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", config["https_port"]), Proxy)
        httpd.timeout = 0.5
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(directory / "server.crt", directory / "localhost.key")
        httpd.socket = context.wrap_socket(httpd.socket, server_side=True)
        stopping = threading.Event()
        signal.signal(signal.SIGTERM, lambda *_: stopping.set())
        signal.signal(signal.SIGINT, lambda *_: stopping.set())
        while not stopping.is_set():
            if process.poll() is not None:
                raise RuntimeError("ingest exited; inspect private ingest.log")
            if issuer_process and issuer_process.poll() is not None:
                raise RuntimeError("issuer exited; inspect private issuer/issuer.log")
            httpd.handle_request()
    finally:
        if httpd:
            httpd.server_close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        backend_log.close()
        if issuer_process and issuer_process.poll() is None:
            issuer_process.terminate()
            try:
                issuer_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                issuer_process.kill()
                issuer_process.wait(timeout=5)
        if issuer_log:
            issuer_log.close()


def health(directory):
    config = read_config(directory)
    with httpx.Client(
        verify=tls_context(directory), trust_env=False, timeout=2
    ) as client:
        response = client.get(origin(config) + "/health")
        response.raise_for_status()
        result = response.json()
        if result.get("build_commit") != config["build_commit"]:
            raise RuntimeError("unexpected server build")
        return result


def start(directory):
    config = read_config(directory)
    try:
        result = health(directory)
        return {"origin": origin(config), "health": result, "already_running": True}
    except httpx.TransportError:
        pass
    if config.get("issuer"):
        name = config["issuer"]["postgres_container"]
        label = subprocess.check_output(
            [
                "docker",
                "inspect",
                "--format",
                '{{index .Config.Labels "orbo.local-commons"}}',
                name,
            ],
            text=True,
        ).strip()
        if label != "true":
            raise RuntimeError("refusing to start an unrelated PostgreSQL container")
        subprocess.run(["docker", "start", name], check=True, stdout=subprocess.DEVNULL)
        for _ in range(60):
            ready = subprocess.run(
                [
                    "docker",
                    "exec",
                    name,
                    "pg_isready",
                    "-U",
                    "orbo",
                    "-d",
                    "orbo_commons",
                ],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            if ready.returncode == 0:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError("local PostgreSQL is not ready")
    # Refuse an unrelated listener; do not terminate or replace it.
    for port in (config["https_port"], config["backend_port"]):
        with socket.socket() as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(("127.0.0.1", port))
    with (directory / "supervisor.log").open("ab") as log:
        process = subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--directory",
                str(directory),
                "serve",
            ],
            cwd=directory,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            umask=0o077,
        )
    write_private(directory / "process.json", {"pid": process.pid})
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("local instance exited; inspect private logs")
        try:
            current_health = health(directory)
            if config.get("issuer"):
                with httpx.Client(timeout=2, trust_env=False) as client:
                    ready = client.get(
                        f"http://127.0.0.1:{config['issuer']['port']}/health"
                    )
                    ready.raise_for_status()
            return {
                "origin": origin(config),
                "health": current_health,
                "pid": process.pid,
            }
        except (httpx.HTTPError, ValueError):
            time.sleep(0.2)
    stop(directory)
    raise RuntimeError("startup timed out")


def stop(directory):
    path = directory / "process.json"
    if not path.exists():
        return {"stopped": False, "reason": "no managed process"}
    pid = json.loads(path.read_text())["pid"]
    cmd = subprocess.run(
        ["ps", "-p", str(pid), "-o", "command="], capture_output=True, text=True
    ).stdout
    if (
        str(Path(__file__).resolve()) not in cmd
        or str(directory) not in cmd
        or "serve" not in cmd
    ):
        return {
            "stopped": False,
            "reason": "managed process absent; no unrelated process touched",
        }
    os.kill(pid, signal.SIGTERM)
    for _ in range(60):
        time.sleep(0.2)
        status = subprocess.run(
            ["ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True
        ).stdout.strip()
        if not status or status.startswith("Z"):
            path.unlink(missing_ok=True)
            return {"stopped": True}
    raise RuntimeError("managed process did not stop")


def stop_database(directory):
    config = read_config(directory)
    if not config.get("issuer"):
        return
    name = config["issuer"]["postgres_container"]
    label = subprocess.check_output(
        [
            "docker",
            "inspect",
            "--format",
            '{{index .Config.Labels "orbo.local-commons"}}',
            name,
        ],
        text=True,
    ).strip()
    if label != "true":
        raise RuntimeError("refusing to stop an unrelated PostgreSQL container")
    subprocess.run(["docker", "stop", name], check=True, stdout=subprocess.DEVNULL)


def synthetic_envelope():
    fixture = (
        ROOT
        / ".sdk/commons-orbo/crates/trace-commons-ingest/fixtures/orbo-prepared-events.json"
    )
    events = json.loads(fixture.read_text())
    # Preserve the prepared bytes. This fixture is explicitly synthetic and
    # was produced by the real contributor parser/redactor regression.
    package = reconstruct_commons(events)
    if package["episode"]["provenance"] != "synthetic":
        raise RuntimeError("only the checked-in synthetic fixture is supported")
    digest = hashlib.sha256(canonical(package).encode()).hexdigest()
    submission = str(uuid.uuid4())
    redaction = "ironclaw-deterministic-secret-path-v3"
    envelope = {
        "schema_version": "ironclaw.trace_contribution.v1",
        "trace_id": str(uuid.uuid4()),
        "submission_id": submission,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "ironclaw": {"version": "orbo-local-synthetic", "channel": "cli"},
        "consent": {
            "policy_version": "2026-04-24",
            "scopes": ["debugging_evaluation"],
            "message_text_included": True,
            "tool_payloads_included": False,
            "revocable": True,
        },
        "contributor": {
            "pseudonymous_contributor_id": "sha256:orbo-local-synthetic",
            "revocation_handle": str(uuid.uuid4()),
        },
        "privacy": {
            "redaction_pipeline_version": redaction,
            "residual_pii_risk": "medium",
            "redaction_hash": "sha256:"
            + hashlib.sha256(
                (json.dumps(events, separators=(",", ":")) + "{}").encode()
            ).hexdigest(),
            "warnings": [
                "Synthetic local integration fixture; no semantic privacy classifier"
            ],
        },
        "events": events,
        "outcome": {"user_feedback": "none", "task_success": "unknown"},
        "replay": {"replayable": False},
        "value": {"submission_score": 0.0, "credit_points_pending": 0.0},
        "trace_card": {
            "consent_scope": "debugging_evaluation",
            "redaction_pipeline_version": redaction,
            "source_channel": "orbo",
            "allowed_uses": ["evaluation"],
            "retention_policy": "private_corpus_revocable",
            "revocation_handle": submission,
        },
    }
    return envelope, digest, package


async def test_flow(directory):
    config = read_config(directory)
    health(directory)
    tokens = json.loads((directory / "credentials.json").read_text())
    report_path = directory / "report.json"
    if report_path.exists():
        raise RuntimeError(
            "test already recorded; use a fresh --directory for a new isolated corpus"
        )
    envelope, digest, package = synthetic_envelope()
    submission = envelope["submission_id"]
    write_private(directory / "submitted-envelope.json", envelope)
    report = {
        "schema": "orbo.local-commons-report.v1",
        "origin": origin(config),
        "build_commit": config["build_commit"],
        "submission_id": submission,
        "source_digest": digest,
        "provenance": "synthetic",
        "checks": {},
        "not_tested": [
            "contributor enrollment and daemon approval/upload",
            "NEAR privacy classifier",
            "production scoring and rewards",
            "physical robot or image delivery",
        ],
    }
    checks = report["checks"]
    async with httpx.AsyncClient(
        base_url=origin(config),
        verify=tls_context(directory),
        timeout=30,
        trust_env=False,
    ) as client:

        async def request(method, path, role=None, **kwargs):
            headers = {"Authorization": "Bearer " + tokens[role]} if role else {}
            return await client.request(method, path, headers=headers, **kwargs)

        response = await request("POST", "/v1/traces", json=envelope)
        assert response.status_code in (401, 403), response.status_code
        checks["unauthenticated_submission"] = "denied"
        response = await request("POST", "/v1/traces", "contributor", json=envelope)
        response.raise_for_status()
        receipt = response.json()
        write_private(directory / "submission-receipt.json", receipt)
        assert receipt["status"] == "quarantined", receipt
        checks["submission"] = "quarantined_for_review"
        params = {
            "purpose": "orbo.attention_timing.v1",
            "status": "accepted",
            "privacy_risk": "medium",
            "consent_scope": "debugging-evaluation",
            "limit": 1,
            "submission_ids": submission,
        }
        path = "/v1/datasets/task-evidence"
        for role in (None, "contributor"):
            response = await request("GET", path, role, params=params)
            assert response.status_code in (401, 403), response.status_code
        checks["unauthorized_retrieval"] = "denied"
        response = await request("GET", path, "export_worker", params=params)
        response.raise_for_status()
        assert response.json()["item_count"] == 0
        checks["unreviewed_evidence"] = "withheld"
        response = await request(
            "POST",
            f"/v1/review/{submission}/decision",
            "reviewer",
            json={
                "decision": "approve",
                "reason": "Owner-authorized synthetic local preservation test; zero reward",
                "credit_points_pending": 0.0,
            },
        )
        response.raise_for_status()
        assert response.json()["status"] == "accepted", response.json()
        checks["review"] = "accepted_zero_credit"
        response = await request("POST", "/v1/traces", "contributor", json=envelope)
        response.raise_for_status()
        assert response.json()["status"] == "accepted"
        checks["same_submission_retry"] = "idempotent"
        response = await request("GET", path, "foreign_export", params=params)
        response.raise_for_status()
        assert response.json()["item_count"] == 0
        checks["cross_tenant_retrieval"] = "withheld"
        consumer = EvidenceConsumer(
            origin(config),
            token=tokens["export_worker"],
            client=httpx.AsyncClient(
                verify=tls_context(directory), trust_env=False, timeout=30
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
            assert result["complete"] and result["tasks"][digest]["package"] == package
            assert result["tasks"][digest]["analysis"]["ack_latency_seconds"] == [1.25]
            write_private(directory / "consumer-report.json", result)
            checks["exact_digest_reconstruction"] = "passed"
            checks["consumer_analysis"] = (
                "1.25_second_acknowledgment; physical_success_not_established"
            )
            # Restart the actual ingest and TLS processes, preserving their store.
            await asyncio.to_thread(stop, directory)
            await asyncio.to_thread(start, directory)
            restarted = await consumer.retrieve(
                "attention_timing",
                [digest],
                privacy_risk="medium",
                submission_ids=[submission],
                limit=1,
            )
            assert restarted["complete"]
            checks["restart_persistence"] = "passed"
            response = await request(
                "POST", f"/v1/traces/{submission}/revoke", "contributor"
            )
            response.raise_for_status()
            revoked = await consumer.retrieve(
                "attention_timing",
                [digest],
                privacy_risk="medium",
                submission_ids=[submission],
                limit=1,
            )
            assert not revoked["complete"] and not revoked["tasks"]
            checks["revoked_evidence"] = "withheld"
        finally:
            await consumer.close()
    report["status"] = "passed"
    write_private(report_path, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=DEFAULT)
    sub = parser.add_subparsers(dest="command", required=True)
    init = sub.add_parser("init")
    init.add_argument(
        "--release", type=Path, default=ROOT / "artifacts/commons-current-macos-arm64"
    )
    init.add_argument("--port", type=int, default=19443)
    init.add_argument("--backend-port", type=int, default=13907)
    shutdown = sub.add_parser("stop")
    shutdown.add_argument("--database", action="store_true")
    for command in ("start", "serve", "status", "test"):
        sub.add_parser(command)
    args = parser.parse_args()
    directory = args.directory.resolve()
    if args.command == "init":
        result = initialize(
            directory, args.release.resolve(), args.port, args.backend_port
        )
    elif args.command == "serve":
        serve(directory)
        return
    elif args.command == "start":
        result = start(directory)
    elif args.command == "stop":
        result = stop(directory)
        if args.database:
            stop_database(directory)
            result["database_stopped"] = True
    elif args.command == "status":
        result = health(directory)
    else:
        result = asyncio.run(test_flow(directory))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

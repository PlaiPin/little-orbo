#!/usr/bin/env python3
"""Provision a loopback PostgreSQL/issuer for the synthetic Commons instance."""

import argparse
import base64
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from datetime import datetime, timezone

import httpx
from cryptography.hazmat.primitives import serialization

SPEC = importlib.util.spec_from_file_location(
    "local_commons", Path(__file__).with_name("local-commons.py")
)
local = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(local)


def initialize(directory, pg_port=15432, issuer_port=13917, admin_port=13918):
    config = local.read_config(directory)
    root = directory / "issuer"
    if root.exists():
        raise RuntimeError("issuer configuration already exists")
    binary = (
        local.ROOT / ".sdk/commons-orbo/target/debug/trace-commons-upload-claim-issuer"
    )
    version = subprocess.check_output([str(binary), "--version"], text=True)
    if config["build_commit"] not in version:
        raise RuntimeError(
            "build the issuer with the same stamped revision as the package"
        )
    root.mkdir(mode=0o700)
    shutil.copy2(binary, root / binary.name)
    keys = {}
    for name in ("signing", "instance"):
        # ring's grant signer needs the public-key-bearing PKCS8 v2 format
        # emitted by the upstream operator tool, rather than OpenSSL's v1.
        generated = subprocess.check_output(
            [str(binary), "--generate-keypair"],
            text=True,
            cwd=root,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        )
        private = (
            re.search(
                r"-----BEGIN PRIVATE KEY-----.*?-----END PRIVATE KEY-----",
                generated,
                re.S,
            )[0]
            + "\n"
        )
        public = (
            re.search(
                r"-----BEGIN PUBLIC KEY-----.*?-----END PUBLIC KEY-----",
                generated,
                re.S,
            )[0]
            + "\n"
        )
        keys[name] = serialization.load_pem_public_key(public.encode())
        local.write_private(root / (name + "-private.pem"), private.encode())
        local.write_private(root / (name + "-public.pem"), public.encode())
    local.write_private(
        root / "keyset.json",
        {
            "keys": [
                {
                    "kid": "orbo-local-signing",
                    "public_key_pem": (root / "signing-public.pem").read_text(),
                }
            ]
        },
    )
    local.write_private(
        root / "allowlist.json",
        {
            "version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "policy_label": "orbo-local-synthetic",
            "entries": [
                {
                    "kind": "instance",
                    "instance_id": "orbo-local",
                    "instance_public_key": base64.b64encode(
                        keys["instance"].public_bytes(
                            serialization.Encoding.Raw, serialization.PublicFormat.Raw
                        )
                    ).decode(),
                    "max_enrollments": 10,
                    "rate_per_min": 30,
                    "policy_template": {
                        "policy_version": "orbo-local-synthetic-v1",
                        "allowed_consent_scopes": ["debugging_evaluation"],
                        "allowed_uses": ["debugging", "evaluation"],
                    },
                }
            ],
        },
    )
    password = secrets.token_hex(32)
    local.write_private(
        root / "postgres.env",
        (
            "POSTGRES_USER=orbo\nPOSTGRES_DB=orbo_commons\nPOSTGRES_PASSWORD="
            + password
            + "\n"
        ).encode(),
    )
    name = "orbo-commons-pg-" + hashlib.sha256(str(directory).encode()).hexdigest()[:10]
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "--label",
            "orbo.local-commons=true",
            "--env-file",
            str(root / "postgres.env"),
            "-p",
            f"127.0.0.1:{pg_port}:5432",
            "--mount",
            f"type=volume,source={name},target=/var/lib/postgresql/data",
            "postgres:15",
        ],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    for _ in range(60):
        ready = subprocess.run(
            ["docker", "exec", name, "pg_isready", "-U", "orbo", "-d", "orbo_commons"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if ready.returncode == 0:
            break
        time.sleep(0.5)
    else:
        raise RuntimeError("local PostgreSQL not ready")
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "RUST_LOG": "warn",
        "DATABASE_URL": f"postgresql://orbo:{password}@127.0.0.1:{pg_port}/orbo_commons",
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_BIND": f"127.0.0.1:{issuer_port}",
        "TRACE_COMMONS_ISSUER_ADMIN_BIND": f"127.0.0.1:{admin_port}",
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_SHUTDOWN_GRACE_SECONDS": "2",
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_SIGNING_PRIVATE_KEY_FILE": str(
            root / "signing-private.pem"
        ),
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_SIGNING_PUBLIC_KEY_FILE": str(
            root / "signing-public.pem"
        ),
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_WORKLOAD_PUBLIC_KEY_FILE": str(
            root / "instance-public.pem"
        ),
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_SIGNING_KID": "orbo-local-signing",
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_ISSUER": local.origin(config),
        "TRACE_COMMONS_UPLOAD_CLAIM_ISSUER_AUDIENCE": "orbo-local-upload",
        "TRACE_COMMONS_ONBOARDING_DEVICE_KEY_REGISTRY_ENABLED": "true",
        "TRACE_COMMONS_ONBOARDING_INGEST_URL": local.origin(config) + "/v1/traces",
        "TRACE_COMMONS_ALLOWLIST_SOURCE": "file:" + str(root / "allowlist.json"),
    }
    local.write_private(root / "environment.json", env)
    local.stop(directory)
    config["issuer"] = {
        "port": issuer_port,
        "postgres_container": name,
        "postgres_port": pg_port,
        "binary_sha256": hashlib.sha256((root / binary.name).read_bytes()).hexdigest(),
    }
    local.write_private(directory / "config.json", config)
    local.start(directory)
    with httpx.Client(timeout=2, trust_env=False) as client:
        for _ in range(90):
            try:
                response = client.get(f"http://127.0.0.1:{issuer_port}/health")
                if response.status_code == 200:
                    return {
                        "issuer": "running",
                        "postgres": "running",
                        "external_credentials_required": [],
                    }
            except httpx.TransportError:
                pass
            time.sleep(0.5)
    raise RuntimeError("issuer not ready; inspect private issuer.log")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, default=local.DEFAULT)
    parser.add_argument("--pg-port", type=int, default=15432)
    parser.add_argument("--issuer-port", type=int, default=13917)
    parser.add_argument("--admin-port", type=int, default=13918)
    parser.add_argument(
        "command", choices=("init", "enroll"), default="init", nargs="?"
    )
    args = parser.parse_args()
    if args.command == "init":
        result = initialize(
            args.directory.resolve(), args.pg_port, args.issuer_port, args.admin_port
        )
    else:
        result = enroll(args.directory.resolve())
    print(json.dumps(result, indent=2))


def contributor_command(directory, args, timeout=30):
    config = local.read_config(directory)
    binary = Path(config["release"]) / "bin/trace-commons-contributor"
    result = subprocess.run(
        [str(binary), "--config-dir", str(directory / "contributor"), *args],
        cwd=directory,
        env={
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "RUST_LOG": "warn",
            "SSL_CERT_FILE": str(directory / "localhost.crt"),
            "TRACE_COMMONS_ALLOWED_HOSTS": "127.0.0.1",
        },
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode:
        local.write_private(directory / "contributor-error.log", result.stderr.encode())
        raise RuntimeError(
            "contributor command failed; inspect private contributor-error.log"
        )
    return result.stdout


def enroll(directory):
    config = local.read_config(directory)
    if not config.get("issuer"):
        raise RuntimeError("initialize the local issuer first")
    state = directory / "contributor"
    state.mkdir(exist_ok=True, mode=0o700)
    if (state / "contributor.json").exists():
        raise RuntimeError(
            "contributor is already enrolled; existing enrollment preserved"
        )
    settings = state / "daemon-settings.json"
    if not settings.exists():
        local.write_private(
            settings,
            (
                local.ROOT / "deploy/trace-commons/daemon-settings.json.example"
            ).read_bytes(),
        )
    contributor_command(directory, ["login", "--key-id"])
    grant = contributor_command(
        directory,
        [
            "mint-grant",
            "--instance-key-pem",
            str(directory / "issuer/instance-private.pem"),
            "--instance-id",
            "orbo-local",
            "--user-subject",
            "synthetic-local-owner",
            "--audience",
            "orbo-local-upload",
            "--issuer-url",
            local.origin(config),
        ],
    ).strip()
    contributor_command(
        directory,
        [
            "login",
            "--grant",
            grant,
            "--allowed-hosts",
            "127.0.0.1",
            "--scopes",
            "debugging_evaluation",
        ],
    )
    enrollment = json.loads((state / "contributor.json").read_text())
    local.stop(directory)
    config["tenant"] = enrollment["tenant_id"]
    local.write_private(directory / "config.json", config)
    local.start(directory)
    return {
        "enrollment": "passed",
        "tenant": config["tenant"],
        "issuer": local.origin(config),
        "grant": "locally signed; no third-party account",
        "external_credentials_required": [],
    }


if __name__ == "__main__":
    main()

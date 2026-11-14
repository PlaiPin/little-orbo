#!/usr/bin/env python3
"""Bounded live NEAR privacy checks and a synthetic local Commons round trip.

The recording proxy forwards classifier response bodies unchanged. Only the
synthetic contributor uses it; no authentication headers are written to disk.
"""

import argparse
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import secrets
import threading
import time

import httpx

from orbo_runtime.credentials import credential

SPEC = importlib.util.spec_from_file_location(
    "local_contribution", Path(__file__).with_name("local-commons-contribute.py")
)
flow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(flow)
local = flow.local
BASE = "https://cloud-api.near.ai/v1/privacy/"
MODEL = "openai/privacy-filter"
CASES = [
    {
        "id": "contact",
        "input": "Synthetic test fixture. My name is Maya Chen. Email me at maya.chen@example.com or call +1 (202) 555-0147. My home address is 742 Cedar Lane, Springfield, IL 62704.",
    },
    {
        "id": "control",
        "input": "The blue robot inspected a red cube on a wooden table. The acknowledgment took 1.25 seconds.",
    },
    {
        "id": "unicode",
        "input": "Synthetic test fixture: 🤖 Café. My name is José Alvarez and my email is jose.alvarez@example.org.",
    },
]
SENSITIVE = [
    "Maya Chen",
    "maya.chen@example.com",
    "+1 (202) 555-0147",
    "742 Cedar Lane",
    "José Alvarez",
    "jose.alvarez@example.org",
]


def safe_response(response, key):
    # Never persist the credential, even if a provider error unexpectedly echoes it.
    text = response.text.replace(key, "[CREDENTIAL REMOVED]")
    try:
        return json.loads(text)
    except ValueError:
        return {"non_json_body": text[:2000]}


def direct_checks(root, key, reuse):
    if not reuse:
        local.write_private(root / "synthetic-inputs.json", CASES)
        with httpx.Client(
            timeout=45, trust_env=False, follow_redirects=False
        ) as client:
            for endpoint in ("classify", "redact"):
                started = time.monotonic()
                response = client.post(
                    BASE + endpoint,
                    headers={"Authorization": "Bearer " + key},
                    json={"model": MODEL, "input": [case["input"] for case in CASES]},
                )
                local.write_private(
                    root / (endpoint + "-response.json"),
                    {
                        "endpoint": endpoint,
                        "http_status": response.status_code,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "request_id": response.headers.get("x-request-id"),
                        "response": safe_response(response, key),
                    },
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        "privacy endpoint refused request; inspect sanitized response"
                    )
    assert json.loads((root / "synthetic-inputs.json").read_text()) == CASES
    classified = json.loads((root / "classify-response.json").read_text())
    redacted = json.loads((root / "redact-response.json").read_text())
    assert classified["http_status"] == redacted["http_status"] == 200
    rows = classified["response"]["data"]
    assert len(rows) == len(CASES)
    spans = []
    for case, row in zip(CASES, rows, strict=True):
        assert row["index"] == len(spans)
        for span in row["spans"]:
            assert 0 <= span["start"] < span["end"] <= len(case["input"])
            assert case["input"][span["start"] : span["end"]] == span["text"]
        spans.append(row["spans"])
    assert {s["category"] for s in spans[0]} >= {
        "private_person",
        "private_email",
        "private_phone",
        "private_address",
    }
    assert not spans[1]
    assert {s["category"] for s in spans[2]} >= {"private_person", "private_email"}
    assert redacted["response"]["data"][1]["redacted"] == CASES[1]["input"]
    unicode_result = redacted["response"]["data"][2]["redacted"]
    unicode_structure = (
        unicode_result.startswith("Synthetic test fixture: 🤖 Café. My name is")
        and " and my email is" in unicode_result
        and "varez" not in unicode_result
        and "e.org" not in unicode_result
    )
    return {
        "classify_http_200": True,
        "all_span_offsets_match_original_codepoints": True,
        "contact_categories": sorted({s["category"] for s in spans[0]}),
        "clean_control_unchanged": True,
        "gateway_unicode_redacted": unicode_result,
        "gateway_unicode_structure_preserved": unicode_structure,
        "direct_responses_reused": reuse,
    }


class Capture:
    def __init__(self, key, root):
        self.key, self.root = key, root
        self.token = secrets.token_urlsafe(32)
        self.calls = []
        self.lock = threading.Lock()
        self.count, self.bytes = 0, 0

    def __enter__(self):
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                size = int(self.headers.get("Content-Length", "0"))
                if (
                    self.path != "/v1/privacy/classify"
                    or not 0 < size <= 65536
                    or self.headers.get("Authorization") != "Bearer " + capture.token
                ):
                    self.send_error(403)
                    return
                body = self.rfile.read(size)
                with capture.lock:
                    capture.count += 1
                    capture.bytes += len(body)
                    number = capture.count
                    allowed = number <= 80 and capture.bytes <= 262144
                if not allowed:
                    self.send_error(429, "synthetic test request budget exhausted")
                    return
                request = json.loads(body)
                if request.get("model") != MODEL:
                    self.send_error(403)
                    return
                started = time.monotonic()
                try:
                    with httpx.Client(
                        timeout=40, trust_env=False, follow_redirects=False
                    ) as client:
                        response = client.post(
                            BASE + "classify",
                            content=body,
                            headers={
                                "Authorization": "Bearer " + capture.key,
                                "Content-Type": "application/json",
                            },
                        )
                    record = {
                        "call": number,
                        "request": request,
                        "http_status": response.status_code,
                        "elapsed_seconds": round(time.monotonic() - started, 3),
                        "request_id": response.headers.get("x-request-id"),
                        "response": safe_response(response, capture.key),
                    }
                    local.write_private(
                        capture.root / f"native-classify-{number:03}.json", record
                    )
                    with capture.lock:
                        capture.calls.append(record)
                    self.send_response(response.status_code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(response.content)))
                    self.end_headers()
                    self.wfile.write(response.content)
                except (httpx.HTTPError, OSError):
                    self.send_error(502, "classifier transport failed")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def environment(self):
        return {
            "TRACE_PRIVACY_FILTER_BACKEND": "near-ai",
            "TRACE_NEAR_AI_PRIVACY_API_KEY": self.token,
            "TRACE_NEAR_AI_PRIVACY_BASE_URL": f"http://127.0.0.1:{self.server.server_port}/v1",
            "TRACE_NEAR_AI_PRIVACY_MODEL": MODEL,
            "TRACE_NEAR_AI_PRIVACY_TIMEOUT_MS": "45000",
            "TRACE_NEAR_AI_PRIVACY_MAX_BATCH_INPUTS": "1",
            "TRACE_NEAR_AI_PRIVACY_MAX_REQUESTS_PER_MINUTE": "60",
        }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--allow-cloud", action="store_true")
    parser.add_argument("--directory", type=Path, default=local.DEFAULT)
    parser.add_argument("--reuse-direct", type=Path)
    args = parser.parse_args()
    if not args.allow_cloud:
        parser.error(
            "--allow-cloud is required for billed synthetic classifier requests"
        )
    os.umask(0o077)
    root = (local.ROOT / "artifacts/privacy-e2e" / str(time.time_ns())).resolve()
    root.mkdir(parents=True, mode=0o700)
    if args.reuse_direct:
        for name in (
            "synthetic-inputs.json",
            "classify-response.json",
            "redact-response.json",
        ):
            local.write_private(root / name, (args.reuse_direct / name).read_bytes())
    key = credential("TRACE_NEAR_AI_PRIVACY_API_KEY")
    report = {
        "schema": "orbo.privacy-e2e.v1",
        "provenance": "synthetic",
        "artifacts": str(root),
    }
    try:
        report["direct_checks"] = direct_checks(root, key, bool(args.reuse_direct))
        with Capture(key, root) as capture:
            fixture = {
                "request": CASES[0]["input"],
                "response": CASES[2]["input"],
                "sensitive": SENSITIVE,
                "environment": capture.environment(),
            }
            try:
                report["contribution"] = asyncio.run(
                    flow.run(
                        args.directory.resolve(),
                        privacy=fixture,
                    )
                )
            finally:
                report["native_classifier_calls"] = len(capture.calls)
                report["native_classifier_statuses"] = sorted(
                    {c["http_status"] for c in capture.calls}
                )
                report["native_request_bytes"] = capture.bytes
                report["native_input_tokens_reported"] = sum(
                    row.get("usage", {}).get("input_tokens", 0)
                    for call in capture.calls
                    for row in call["response"].get("data", [])
                )
                if "run_directory" in fixture:
                    path = Path(fixture["run_directory"]) / "report.json"
                    if path.is_file():
                        report["contribution"] = {
                            "report": str(path),
                            **json.loads(path.read_text()),
                        }
            assert capture.calls, "native contributor did not call the classifier"
        if not report["direct_checks"]["gateway_unicode_structure_preserved"]:
            raise RuntimeError("gateway Unicode redaction check failed")
        report["status"] = "passed"
    except Exception as error:
        report["status"] = "failed"
        report["error_type"] = type(error).__name__
        raise
    finally:
        local.write_private(root / "report.json", report)
        print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

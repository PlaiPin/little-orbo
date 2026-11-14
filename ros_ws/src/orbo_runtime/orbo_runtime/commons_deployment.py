"""Read-only deployed Commons identity, daemon capability and delivery checks.

Health is deliberately separate from admission, delivery and rewards. An open
server can be healthy while its production scoring pipeline is unavailable.
"""

import asyncio
import json
from pathlib import Path
import re

import httpx

from .commons import ContributorIPC
from .consumer import EvidenceConsumer
from .contracts import Rejected

REQUIRED_METHODS = {
    "preview_body",
    "approve_reviewed",
    "orbo_entry_status",
    "orbo_revoke_reviewed",
}


def release_identity(manifest):
    if manifest.get("schema") != "orbo.commons-release.v1":
        raise Rejected("Commons release manifest required")
    revision, digest = (
        manifest.get("upstream_revision", ""),
        manifest.get("patch_sha256", ""),
    )
    if not re.fullmatch("[0-9a-f]{40}", revision) or not re.fullmatch(
        "[0-9a-f]{64}", digest
    ):
        raise Rejected("invalid source identity")
    expected = revision[:12] + "-orbo-" + digest[:16]
    if manifest.get("build_commit") != expected:
        raise Rejected("inconsistent release identity")
    return expected


def checked_origin(origin):
    url = httpx.URL(origin)
    if (
        url.scheme != "https"
        or not url.host
        or url.username
        or url.password
        or url.query
        or url.fragment
        or url.path not in ("", "/")
    ):
        raise Rejected("explicit HTTPS Commons origin required")
    return str(url).rstrip("/")


async def bounded_json(client, url):
    data = bytearray()
    async with client.stream("GET", url) as response:
        if response.status_code != 200:
            raise Rejected("Commons health unavailable")
        async for chunk in response.aiter_bytes():
            if len(data) + len(chunk) > 65_536:
                raise Rejected("Commons health response too large")
            data.extend(chunk)
    value = json.loads(data)
    if not isinstance(value, dict):
        raise Rejected("invalid Commons health response")
    return value


async def check_deployment(
    origin,
    manifest,
    *,
    socket=None,
    submission_id=None,
    digest=None,
    purpose="embodied_process",
    privacy_risk="low",
    token=None,
    client=None,
    ipc=None,
):
    """No enrollment, approval, uploads, ledger mutation or token contents in output."""
    origin = checked_origin(origin)
    expected = release_identity(manifest)
    if bool(submission_id) != bool(digest):
        raise Rejected(
            "submission identity and reviewed digest must be supplied together"
        )
    owned = client is None
    client = client or httpx.AsyncClient(
        timeout=30, follow_redirects=False, trust_env=False
    )
    try:
        async with asyncio.timeout(120):
            health = await bounded_json(client, origin + "/health")
            if health.get("status") != "ok" or health.get("build_commit") != expected:
                raise Rejected("deployed Commons build does not match release")
            if health.get("near_ai_staging") is not False:
                raise Rejected("production-channel Commons build required")
            source = await bounded_json(client, origin + "/v1/source")
            if (
                source.get("build_commit") != expected
                or source.get("license") != "AGPL-3.0-or-later"
                or source.get("source_url") != origin + "/orbo-commons-source.tar.gz"
            ):
                raise Rejected("published patched-source offer required")
            report = {
                "schema": "orbo.commons-deployment-check.v1",
                "build_commit": expected,
                "health": "passed",
                "daemon": "not_checked",
                "delivery": "not_checked",
                "production_pipeline": "not_qualified",
                "rewards": "not_checked",
            }
            if socket or ipc:
                ipc = ipc or ContributorIPC(Path(socket))
                hello = await ipc.call("hello", {})
                if not REQUIRED_METHODS <= set(hello.get("methods", [])):
                    raise Rejected("contributor lacks required Orbo methods")
                status = await ipc.call("status", {})
                if status.get("logged_in") is not True:
                    raise Rejected("contributor enrollment required")
                report["daemon"] = "enrolled_compatible"
                report["setup_complete"] = status.get("setup_complete") is True
                report["paused"] = status.get("paused") is True
                report["terms_hold"] = status.get("terms_hold") is True
            if submission_id:
                consumer = EvidenceConsumer(origin, client=client, token=token)
                result = await consumer.retrieve(
                    purpose,
                    [digest],
                    privacy_risk=privacy_risk,
                    submission_ids=[submission_id],
                    limit=1,
                )
                if not result["complete"]:
                    raise Rejected(
                        "selected accepted evidence not available or incomplete"
                    )
                report["delivery"] = "exact_reviewed_digest_verified"
                report["response_sha256"] = result["response_sha256"]
            return report
    finally:
        if owned:
            await client.aclose()

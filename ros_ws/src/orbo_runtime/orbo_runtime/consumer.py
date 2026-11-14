"""Authorized bounded evidence retrieval from the patched Commons export surface."""

import hashlib
import json
import uuid

import httpx

from .contracts import Rejected, canonical
from .providers import check_response, credential
from .traces import PURPOSES, analyze, reconstruct_commons


class EvidenceConsumer:
    def __init__(self, base_url, *, client=None, token=None):
        url = httpx.URL(base_url)
        if (
            url.scheme != "https"
            or not url.host
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise Rejected("explicit HTTPS Commons origin required")
        self.base = str(url).rstrip("/")
        self.client = client or httpx.AsyncClient(
            timeout=60, follow_redirects=False, trust_env=False
        )
        self.token = token or credential("COMMONS_EXPORT_TOKEN")

    async def retrieve(
        self, purpose, expected_digests, *, privacy_risk, submission_ids, limit=10
    ):
        if (
            purpose not in PURPOSES
            or privacy_risk not in {"low", "medium"}
            or type(limit) is not int
            or not 1 <= limit <= 10
        ):
            raise Rejected("bounded explicit consumer profile required")
        if (
            not expected_digests
            or len(expected_digests) > 10
            or any(
                not isinstance(s, str)
                or len(s) != 64
                or any(c not in "0123456789abcdef" for c in s)
                for s in expected_digests
            )
        ):
            raise Rejected("reviewed source export digests required")
        try:
            targets = [str(uuid.UUID(value)) for value in submission_ids]
        except (ValueError, TypeError, AttributeError):
            raise Rejected("explicit submission UUIDs required") from None
        if (
            len(targets) != len(expected_digests)
            or len(set(targets)) != len(targets)
            or len(set(expected_digests)) != len(expected_digests)
            or len(targets) > limit
        ):
            raise Rejected(
                "one unique submission per expected digest within limit required"
            )
        expected = dict(zip(targets, expected_digests))
        body = bytearray()
        async with self.client.stream(
            "GET",
            self.base + "/v1/datasets/task-evidence",
            headers={"Authorization": "Bearer " + self.token},
            params={
                "purpose": "orbo." + purpose + ".v1",
                "status": "accepted",
                "privacy_risk": privacy_risk,
                "consent_scope": "debugging-evaluation",
                "limit": limit,
                "submission_ids": ",".join(targets),
            },
        ) as response:
            check_response(response, "commons-consumer")
            if response.headers.get("x-orbo-task-selection") != ",".join(targets):
                raise Rejected("server has not acknowledged exact submission selection")
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 16_777_216:
                    raise Rejected("consumer response byte limit exceeded")
                body.extend(chunk)
        delivered = json.loads(body)
        uuid.UUID(delivered["export_id"])
        if (
            not isinstance(delivered.get("manifest"), dict)
            or not isinstance(delivered.get("items"), list)
            or len(delivered["items"]) != delivered.get("item_count")
            or len(delivered["items"]) > limit
        ):
            raise Rejected("invalid or incomplete consumer export")
        found, rejected = {}, []
        for item in delivered["items"]:
            submission = str(uuid.UUID(item["submission_id"]))
            if submission not in expected:
                raise Rejected(
                    "delivered submission does not match requested identity and digest"
                )
            if item.get(
                "evidence_schema_version"
            ) != "orbo.task-evidence.v1" or not isinstance(item.get("events"), list):
                raise Rejected("server lacks qualified full-event delivery")
            try:
                package = reconstruct_commons(item["events"])
            except (Rejected, KeyError, TypeError, ValueError):
                rejected.append(
                    {
                        "submission_id": submission,
                        "reason": "required_evidence_unavailable",
                    }
                )
                continue
            sha = hashlib.sha256(canonical(package).encode()).hexdigest()
            if (
                submission not in expected
                or sha != expected[submission]
                or package["purpose"] != purpose
            ):
                raise Rejected(
                    "delivered submission does not match requested identity and digest"
                )
            if sha in found:
                raise Rejected("duplicate task evidence in delivery")
            found[sha] = {
                "submission_id": submission,
                "package": package,
                "analysis": analyze(package),
            }
        return {
            "schema": "orbo.consumer-report.v1",
            "server_export_id": delivered["export_id"],
            "manifest": delivered["manifest"],
            "response_sha256": hashlib.sha256(body).hexdigest(),
            "tasks": found,
            "missing_source_digests": sorted(set(expected_digests) - found.keys()),
            "unusable_items": rejected,
            "complete": set(found) == set(expected_digests),
            "limits": [
                "Server admission and delivery do not prove physical outcomes.",
                "Previously delivered copies cannot be recalled by future access revocation.",
            ],
        }

    async def close(self):
        await self.client.aclose()

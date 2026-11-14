"""Separate provider usage, contribution stages and credit evidence; no purchases."""

from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import uuid

import httpx

from .contracts import Rejected, canonical, digest
from .providers import check_response, credential


KINDS = {
    "usage",
    "contribution_submitted",
    "contribution_admitted",
    "contribution_delivered",
    "award",
    "credit_received",
    "credit_applied",
    "owner_topup",
    "adjustment",
    "accounting_snapshot",
}


def amount(value):
    if not isinstance(value, str):
        raise Rejected("monetary values must be decimal strings")
    try:
        parsed = Decimal(value)
    except InvalidOperation:
        raise Rejected("invalid monetary amount") from None
    if not parsed.is_finite():
        raise Rejected("nonfinite monetary amount")
    return parsed


class Ledger:
    def __init__(self, store):
        self.store = store

    def observe(self, provider, account, external_id, kind, body, *, provenance):
        if (
            provider not in {"near", "commons", "openai"}
            or kind not in KINDS
            or provenance not in {"live_service", "fixture", "owner_assertion"}
        ):
            raise Rejected("invalid record classification")
        if not account or not external_id or len(external_id) > 256:
            raise Rejected("account and stable external record identity required")
        if body.get("amount") is not None:
            value = amount(body["amount"])
            if kind != "adjustment" and value < 0:
                raise Rejected("negative ordinary monetary observation")
            if body.get("instrument") not in {
                "USD",
                "nano_USD",
                "near_restricted_credit",
            }:
                raise Rejected("explicit instrument required")
            if (
                provider == "openai"
                and body["instrument"] == "near_restricted_credit"
            ):
                raise Rejected("NEAR restricted credit cannot fund OpenAI")
        if kind == "credit_applied" and not body.get("usage_reference"):
            raise Rejected("applied credit requires actual usage correlation")
        if kind == "credit_received" and not body.get("receipt_reference"):
            raise Rejected("received credit requires a receipt reference")
        record = {"provenance": provenance, "data": body}
        sha = digest(record)
        with self.store.transaction() as db:
            previous = db.execute(
                "SELECT digest FROM records WHERE provider=? AND account=? AND external_id=? AND kind=?",
                (provider, account, external_id, kind),
            ).fetchone()
            if previous:
                if previous[0] != sha:
                    raise Rejected(
                        "record changed: retain and reconcile an explicit adjustment"
                    )
                return False
            db.execute(
                "INSERT INTO records VALUES(?,?,?,?,?,?)",
                (provider, account, external_id, kind, sha, canonical(record)),
            )
            return True

    def summary(self):
        groups = {}
        for row in self.store.db.execute(
            "SELECT * FROM records ORDER BY provider,account,external_id,kind"
        ):
            record = json.loads(row["body"])
            data = record["data"]
            key = (
                row["provider"],
                row["account"],
                row["kind"],
                record["provenance"],
                data.get("instrument"),
            )
            group = groups.setdefault(
                key,
                {
                    "provider": key[0],
                    "account": key[1],
                    "kind": key[2],
                    "provenance": key[3],
                    "instrument": key[4],
                    "records": 0,
                    "known_amount": Decimal(0),
                    "unknown_amount_count": 0,
                },
            )
            group["records"] += 1
            if row["kind"] == "accounting_snapshot":
                group["aggregation"] = "snapshot_not_additive"
                # Balance samples are not expenses; timestamps may be absent.
                stamp = data.get("observed_utc")
                if type(stamp) in {float, int} and stamp > group.get(
                    "latest_observed_utc", float("-inf")
                ):
                    group["latest_observed_utc"] = stamp
                    group["latest_balance"] = data.get("amount")
            elif data.get("amount") is None:
                group["unknown_amount_count"] += 1
            else:
                group["known_amount"] += amount(data["amount"])
        for group in groups.values():
            group["known_amount"] = (
                None
                if group.get("aggregation") == "snapshot_not_additive"
                else str(group["known_amount"])
            )
        return {
            "groups": list(groups.values()),
            "openai_funding": "separate_owner_account",
            "automatic_purchases": False,
            "cost_coverage": "not_established",
            "award_is_received_credit": False,
            "received_is_applied_credit": False,
        }


class NearUsage:
    """Fixed-window reporting on the platform host with a separate reporting token."""

    def __init__(self, organization, ledger, *, client=None, token=None):
        self.organization = str(uuid.UUID(organization))
        self.ledger = ledger
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        )
        self.token = token or credential("NEARAI_REPORTING_TOKEN")

    async def sync(self, start, end):
        a, b = (
            datetime.fromisoformat(start.replace("Z", "+00:00")),
            datetime.fromisoformat(end.replace("Z", "+00:00")),
        )
        if (
            a.tzinfo is None
            or b.tzinfo is None
            or not 0 <= (b - a).total_seconds() <= 366 * 86400
        ):
            raise Rejected("bounded timezone-aware reporting window required")
        params = {"start_time": start, "end_time": end, "source": "all", "limit": 1000}
        seen = set()
        inserted = 0
        for _ in range(100):
            response = await self.client.get(
                f"https://cloud-platform.near.ai/v1/organizations/{self.organization}/usage/export",
                params=params,
                headers={"Authorization": "Bearer " + self.token},
            )
            check_response(response, "near-usage")
            payload = response.json()
            if not isinstance(payload.get("data"), list) or len(payload["data"]) > 1000:
                raise Rejected("invalid reporting page")
            for row in payload["data"]:
                cost = row.get("total_cost_nano_usd")
                if (
                    type(cost) is not int
                    or cost < 0
                    or not isinstance(row.get("id"), str)
                    or row.get("source") not in {"inference", "service"}
                ):
                    raise Rejected("invalid usage record")
                inserted += self.ledger.observe(
                    "near",
                    self.organization,
                    row["source"] + ":" + row["id"],
                    "usage",
                    {
                        "amount": str(cost),
                        "instrument": "nano_USD",
                        "provider_record": row,
                    },
                    provenance="live_service",
                )
            cursor = payload.get("next_cursor")
            if not cursor:
                return {
                    "inserted": inserted,
                    "window": {"start": start, "end": end},
                    "complete": True,
                }
            if not isinstance(cursor, str) or cursor in seen or len(cursor) > 4096:
                raise Rejected("invalid or repeated reporting cursor")
            seen.add(cursor)
            params["cursor"] = cursor
        raise Rejected("reporting page budget exceeded; narrow the fixed window")

    async def close(self):
        await self.client.aclose()


class CommonsAccounting:
    """Read authoritative contributor status/credit views; never initiate settlement.

    These upstream views expose score and payout labels, not recipient receipts
    or usage allocations. Store complete snapshots without inventing those facts.
    """

    def __init__(self, origin, account, ledger, *, client=None, token=None):
        from .consumer import EvidenceConsumer

        # Reuse its strict HTTPS-origin validation, with a distinct credential.
        transport = EvidenceConsumer(
            origin,
            client=client,
            token=token or credential("COMMONS_CONTRIBUTOR_TOKEN"),
        )
        self.base, self.client, self.token = (
            transport.base,
            transport.client,
            transport.token,
        )
        if not isinstance(account, str) or not 0 < len(account) <= 128:
            raise Rejected("configured contributor account label required")
        self.account, self.ledger = account, ledger

    async def read(self, path, submission_ids=None):
        body = bytearray()
        kwargs = {"headers": {"Authorization": "Bearer " + self.token}}
        if submission_ids is not None:
            kwargs["json"] = {"submission_ids": submission_ids}
        async with self.client.stream(
            "POST" if submission_ids is not None else "GET", self.base + path, **kwargs
        ) as response:
            check_response(response, "commons-accounting")
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > 4_194_304:
                    raise Rejected("accounting response byte limit exceeded")
                body.extend(chunk)
        # Preserve upstream decimal text instead of introducing binary float rounding.
        return json.loads(body, parse_float=str)

    async def sync(self, submission_ids):
        if not isinstance(submission_ids, list) or not 1 <= len(submission_ids) <= 100:
            raise Rejected("one to 100 explicit linked submission IDs required")
        ids = sorted({str(uuid.UUID(value)) for value in submission_ids})
        statuses = await self.read("/v1/contributors/me/submission-status", ids)
        if not isinstance(statuses, list) or len(statuses) > len(ids):
            raise Rejected("invalid submission status response")
        seen = set()
        for row in statuses:
            identity = str(uuid.UUID(row["submission_id"]))
            if identity not in ids or identity in seen:
                raise Rejected("unrequested or duplicate submission identity")
            seen.add(identity)
        credit = await self.read("/v1/contributors/me/credit")
        events = await self.read("/v1/contributors/me/credit-events")
        if (
            not isinstance(credit, dict)
            or not isinstance(credit.get("tenant_id"), str)
            or not isinstance(events, list)
        ):
            raise Rejected("invalid contributor credit view")
        selected = []
        for event in events:
            if event.get("submission_id") not in ids:
                continue
            uuid.UUID(event["event_id"])
            if event.get("tenant_id") != credit["tenant_id"]:
                raise Rejected("credit tenant mismatch")
            selected.append(event)
        snapshot = {
            "statuses": statuses,
            "account_credit": credit,
            "credit_events": selected,
            "requested_submission_ids": ids,
        }
        sha = digest(snapshot)
        inserted = (
            self.ledger.observe(
                "commons",
                self.account,
                sha,
                "accounting_snapshot",
                {"snapshot": snapshot, "observed_utc": self.ledger.store.clock.utc()},
                provenance="live_service",
            )
            if not self.ledger.store.db.execute(
                "SELECT 1 FROM records WHERE provider='commons' AND account=? AND external_id=? AND kind='accounting_snapshot'",
                (self.account, sha),
            ).fetchone()
            else False
        )
        return {
            "inserted": inserted,
            "snapshot_sha256": sha,
            **snapshot,
            "missing_submission_ids": sorted(set(ids) - seen),
            "received_credit": "not_established_by_this_view",
            "applied_credit": "not_established_by_this_view",
            "account_totals_are_task_awards": False,
        }

    async def close(self):
        await self.client.aclose()

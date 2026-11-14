"""Explicit, policy-bounded NEAR-origin 1Click swaps with durable deposit identity.

This module is intentionally not exposed as a voice/model tool. Review the stored
quote and pass its digest to execute. All retries reuse the same signed transfer.
"""

import argparse
import asyncio
from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import time
import uuid

import httpx

from .contracts import Rejected, canonical, digest
from .near_signer import NearSigner, account, b58decode, b58encode, request_json
from .storage import Store

API = "https://1click.chaindefuser.com"
# @defuse-protocol/one-click-sdk-typescript 0.1.26 src/quote-signature.ts.
MANAGER_KEY = "reYaWhvwu8Jzo3WUM3zhn6VrhuMEF4eADL17qtRVifc"
REQUEST_FIELDS = "dry swapType slippageTolerance originAsset depositType destinationAsset amount refundTo refundType recipient recipientType deadline".split()
OPTIONAL_REQUEST_FIELDS = "quoteWaitingTimeMs referral virtualChainRecipient virtualChainRefundRecipient customRecipientMsg".split()
QUOTE_FIELDS = "amountIn amountInFormatted amountInUsd minAmountIn amountOut amountOutFormatted amountOutUsd minAmountOut".split()
FULL_FIELDS = "depositAddress depositMemo deadline timeWhenInactive timeEstimate refundFee withdrawFee".split()
STATES = {
    "KNOWN_DEPOSIT_TX",
    "PENDING_DEPOSIT",
    "INCOMPLETE_DEPOSIT",
    "PROCESSING",
    "SUCCESS",
    "REFUNDED",
    "FAILED",
}


def units(value):
    if (
        not isinstance(value, str)
        or not value.isascii()
        or not value.isdigit()
        or value.startswith("0")
        or len(value) > 39
        or not 0 < int(value) < 2**128
    ):
        raise Rejected("amount must be a positive canonical u128 decimal string")
    return int(value)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if result.tzinfo is None:
            raise ValueError()
        return result.timestamp()
    except (AttributeError, TypeError, ValueError):
        raise Rejected("invalid timezone-aware deadline") from None


def _stable(value):
    # Supported signed fields contain strings, booleans and small integer
    # numbers. Match JS stable stringify for integral JSON numbers; reject
    # unfamiliar numeric representations rather than verify different bytes.
    if isinstance(value, dict):
        return {k: _stable(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_stable(v) for v in value]
    if type(value) in {float, int}:
        if not float(value).is_integer() or abs(value) > 2**53 - 1:
            raise Rejected("unsupported signed numeric field")
        return int(value)
    return value


def quote_hash(response):
    request, quote = response["quoteRequest"], response["quote"]
    signed = {key: request[key] for key in REQUEST_FIELDS if key in request}
    signed.update(
        {key: request[key] for key in OPTIONAL_REQUEST_FIELDS if request.get(key)}
    )
    signed.update({key: quote[key] for key in QUOTE_FIELDS if key in quote})
    if not request["dry"]:
        signed.update({key: quote[key] for key in FULL_FIELDS if quote.get(key)})
    signed["timestamp"] = response["timestamp"]
    encoded = json.dumps(
        _stable(signed),
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    ).encode()
    return b58encode(hashlib.sha256(encoded).digest())


def verify_quote(response, manager_key=MANAGER_KEY):
    from nacl.signing import VerifyKey
    from nacl.exceptions import BadSignatureError

    try:
        signature = response["signature"].removeprefix("ed25519:")
        VerifyKey(b58decode(manager_key)).verify(
            quote_hash(response).encode(), b58decode(signature)
        )
    except (KeyError, TypeError, ValueError, BadSignatureError):
        raise Rejected("1Click quote signature verification failed") from None


class Policy:
    """One approved route, recipient and cumulative input ceiling per journal."""

    def __init__(self, body):
        expected = {
            "sender",
            "origin_contract",
            "destination_asset",
            "recipient",
            "max_input",
            "max_total_input",
            "min_output",
            "slippage_bps",
            "deadline_seconds",
        }
        if not isinstance(body, dict) or set(body) != expected:
            raise Rejected("policy fields do not match the restricted swap policy")
        self.body = body.copy()
        for key in ("sender", "origin_contract"):
            account(body[key])
        for key in ("max_input", "max_total_input", "min_output"):
            units(body[key])
        if units(body["max_total_input"]) < units(body["max_input"]):
            raise Rejected("total input cap is below per-swap cap")
        for key in ("destination_asset", "recipient"):
            if (
                not isinstance(body[key], str)
                or not 1 <= len(body[key]) <= 256
                or not body[key].isascii()
                or any(c.isspace() for c in body[key])
            ):
                raise Rejected("explicit destination asset and recipient required")
        if (
            type(body["slippage_bps"]) is not int
            or not 0 <= body["slippage_bps"] <= 100
        ):
            raise Rejected("slippage must be between zero and 100 basis points")
        if (
            type(body["deadline_seconds"]) is not int
            or not 60 <= body["deadline_seconds"] <= 600
        ):
            raise Rejected("deadline must be between 60 and 600 seconds")

    def request(self, amount, dry, now):
        if units(amount) > units(self.body["max_input"]):
            raise Rejected("per-swap input cap exceeded")
        return {
            "dry": dry,
            "swapType": "EXACT_INPUT",
            "slippageTolerance": self.body["slippage_bps"],
            "originAsset": "nep141:" + self.body["origin_contract"],
            "depositType": "ORIGIN_CHAIN",
            "destinationAsset": self.body["destination_asset"],
            "amount": amount,
            "refundTo": self.body["sender"],
            "refundType": "ORIGIN_CHAIN",
            "recipient": self.body["recipient"],
            "recipientType": "DESTINATION_CHAIN",
            "deadline": datetime.fromtimestamp(
                now + self.body["deadline_seconds"], timezone.utc
            )
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
        }


class OneClick:
    def __init__(self, *, client=None, token=None, manager_key=MANAGER_KEY):
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        )
        self.owns_client = client is None
        self.token = token
        self.manager_key = manager_key

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def call(self, method, path, **kwargs):
        return await request_json(
            self.client,
            method,
            API + path,
            success_statuses=(200, 201) if path == "/v0/quote" else (200,),
            headers={"X-API-Key": self.token} if self.token else {},
            **kwargs,
        )

    async def quote(self, request):
        result = await self.call("POST", "/v0/quote", json=request)
        verify_quote(result, self.manager_key)
        if any(result["quoteRequest"].get(k) != v for k, v in request.items()):
            raise Rejected("quote changed requested route or limits")
        # Reject unsupported execution modifiers even if the server echoes them.
        # Platform fees may be added by 1Click. The signed output floor and
        # exact input bound their effect; these are not extra wallet transfers.
        for key in (
            "rebates",
            "customRecipientMsg",
            "virtualChainRecipient",
            "virtualChainRefundRecipient",
        ):
            if result["quoteRequest"].get(key):
                raise Rejected("unsupported quote execution modifier")
        return result

    async def tokens(self):
        result = await self.call("GET", "/v0/tokens")
        if not isinstance(result, list):
            raise Rejected("invalid token catalogue")
        return result

    async def status(self, quote):
        params = {"depositAddress": quote["quote"]["depositAddress"]}
        if quote["quote"].get("depositMemo"):
            params["depositMemo"] = quote["quote"]["depositMemo"]
        result = await self.call("GET", "/v0/status", params=params)
        verify_quote(result.get("quoteResponse", {}), self.manager_key)
        if quote_hash(result["quoteResponse"]) != quote_hash(quote):
            raise Rejected("status belongs to another quote")
        if result.get("status") not in STATES or not isinstance(
            result.get("swapDetails"), dict
        ):
            raise Rejected("invalid swap status")
        return result


class Swaps:
    def __init__(self, path, policy, api, *, now=time.time):
        self.path = str(path)
        self.store = Store(path)
        self.policy = policy
        self.api = api
        self.now = now
        self.store.db.execute(
            "CREATE TABLE IF NOT EXISTS intent_swaps(id TEXT PRIMARY KEY, body TEXT NOT NULL)"
        )

    @contextmanager
    def exclusive(self):
        # Lock spans RPC preparation and SQLite persistence, preventing competing
        # processes from preparing two transfers with the same key nonce.
        fd = os.open(self.path + ".lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Rejected("another Intents operation is running") from None
            yield
        finally:
            os.close(fd)

    def save(self, row):
        with self.store.transaction() as db:
            db.execute(
                "INSERT INTO intent_swaps VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body",
                (row["id"], canonical(row)),
            )

    def get(self, swap_id):
        row = self.store.db.execute(
            "SELECT body FROM intent_swaps WHERE id=?", (swap_id,)
        ).fetchone()
        if not row:
            raise Rejected("unknown swap")
        return json.loads(row[0])

    @staticmethod
    def public(row):
        return {key: value for key, value in row.items() if key != "prepared"}

    async def quote(self, amount, *, dry=True):
        request = self.policy.request(amount, dry, self.now())
        tokens = await self.api.tokens()
        origin = next(
            (t for t in tokens if t.get("assetId") == request["originAsset"]), None
        )
        destination = next(
            (t for t in tokens if t.get("assetId") == request["destinationAsset"]), None
        )
        if (
            not origin
            or origin.get("blockchain") != "near"
            or origin.get("contractAddress") != self.policy.body["origin_contract"]
            or not destination
        ):
            raise Rejected("route is not a supported NEAR-origin token route")
        result = await self.api.quote(request)
        q = result["quote"]
        if units(q["amountIn"]) != units(amount) or units(q["minAmountOut"]) < units(
            self.policy.body["min_output"]
        ):
            raise Rejected("quote violates amount or minimum output policy")
        if not dry:
            account(q.get("depositAddress"))
            if q.get("depositMemo"):
                raise Rejected("NEAR deposit memos are unsupported")
            expiry = min(
                timestamp(q["deadline"]),
                timestamp(q["timeWhenInactive"]),
                timestamp(request["deadline"]),
            )
            if expiry <= self.now() + 15:
                raise Rejected("quote expires too soon")
        else:
            expiry = timestamp(request["deadline"])
        row = {
            "id": str(uuid.uuid4()),
            "state": "dry" if dry else "quoted",
            "policy": self.policy.body,
            "quote": result,
            "expires": expiry,
            "created": self.now(),
        }
        row["approval_digest"] = digest(row)
        with self.exclusive():
            self.save(row)
        return self.public(row)

    def check(self, row, approval):
        if row["state"] != "quoted" or approval != row["approval_digest"]:
            raise Rejected("exact non-dry quote digest approval required")
        reviewed = {
            key: row[key]
            for key in ("id", "state", "policy", "quote", "expires", "created")
        }
        if digest(reviewed) != approval:
            raise Rejected("reviewed quote record changed")
        if row["policy"] != self.policy.body:
            raise Rejected("policy changed since quote review")
        if self.now() + 15 >= row["expires"]:
            raise Rejected(
                "quote expired or too close to expiration; reconcile existing deposits"
            )
        verify_quote(row["quote"], self.api.manager_key)

    def cancel(self, swap_id):
        """Withdraw a local unsent approval opportunity; never claim a refund."""
        with self.exclusive():
            row = self.get(swap_id)
            if row.get("prepared"):
                raise Rejected(
                    "signed deposit cannot be cancelled locally; reconcile settlement"
                )
            row["state"] = "cancelled"
            self.save(row)
            return self.public(row)

    async def execute(self, swap_id, approval, signer, *, allow_funded=False):
        if not allow_funded:
            raise Rejected("funded execution requires explicit opt-in")
        with self.exclusive():
            row = self.get(swap_id)
            if signer.sender != self.policy.body["sender"]:
                raise Rejected("signer does not match policy sender")
            self.check(row, approval)
            if row.get("swap_status") in {"SUCCESS", "REFUNDED", "FAILED"} or row.get(
                "deposit_state"
            ) in {"final", "failed"}:
                return self.public(row)
            if "prepared" not in row:
                existing = [
                    json.loads(item[0])
                    for item in self.store.db.execute("SELECT body FROM intent_swaps")
                ]
                if any(
                    item.get("prepared")
                    and item.get("deposit_state") not in {"final", "failed"}
                    for item in existing
                ):
                    raise Rejected(
                        "reconcile unresolved deposit before preparing another"
                    )
                spent = sum(
                    units(item["quote"]["quote"]["amountIn"])
                    for item in existing
                    if item.get("prepared")
                )
                if spent + units(row["quote"]["quote"]["amountIn"]) > units(
                    self.policy.body["max_total_input"]
                ):
                    raise Rejected("cumulative journal input cap exceeded")
                row["prepared"] = await signer.prepare(
                    self.policy.body["origin_contract"],
                    row["quote"]["quote"]["depositAddress"],
                    row["quote"]["quote"]["amountIn"],
                )
                row["deposit_tx_hash"] = row["prepared"]["tx_hash"]
                row["deposit_state"] = "prepared"
                # Commit before sending: interruption or timeout cannot cause a
                # second transaction to be generated for this reviewed quote.
                self.save(row)
            self.check(row, approval)
            row["deposit_state"] = "uncertain"
            self.save(row)
            outcome = await signer.broadcast(row["prepared"])
            self.record_deposit(row, outcome)
            return self.public(row)

    def record_deposit(self, row, outcome):
        if outcome.get("transaction", {}).get("hash") != row["deposit_tx_hash"]:
            raise Rejected("RPC returned another transaction")
        status = outcome.get("status", {})
        if outcome.get("final_execution_status") == "FINAL":
            if "SuccessValue" in status:
                row["deposit_state"] = "final"
            elif "Failure" in status:
                row["deposit_state"] = "failed"
            else:
                raise Rejected("unrecognized finalized transaction status")
        row["deposit_receipt"] = outcome
        self.save(row)

    async def reconcile(self, swap_id, signer=None):
        with self.exclusive():
            row = self.get(swap_id)
            if row["state"] == "dry":
                raise Rejected("dry quote has no settlement")
            if signer is not None and row.get("prepared"):
                if signer.sender != row["prepared"]["sender"]:
                    raise Rejected("signer does not match deposit")
                self.record_deposit(row, await signer.status(row["prepared"]))
            receipt = await self.api.status(row["quote"])
            row["swap_status"] = receipt["status"]
            row["settlement_receipt"] = receipt
            row["observed"] = self.now()
            self.save(row)
            return self.public(row)


async def run(args):
    policy = Policy(json.loads(Path(args.policy).read_text()))
    api = OneClick(token=os.environ.get("NEAR_INTENTS_API_KEY"))
    swaps = Swaps(args.database, policy, api)
    signer = None
    try:
        if args.command == "preflight":
            return {
                "policy_valid": True,
                "network": "mainnet",
                "origin": "nep141:" + policy.body["origin_contract"],
                "authenticated_api": bool(api.token),
                "network_contacted": False,
                "funded_execution": False,
            }
        if args.command == "show":
            return swaps.public(swaps.get(args.swap_id))
        if args.command == "cancel":
            return swaps.cancel(args.swap_id)
        if args.command == "quote":
            return await swaps.quote(args.amount, dry=not args.firm)
        if args.command in {"execute", "status"} and args.key_file:
            signer = NearSigner(policy.body["sender"], args.key_file)
        if args.command == "execute":
            if signer is None:
                raise Rejected("owner-only signer key file required")
            return await swaps.execute(
                args.swap_id, args.approve, signer, allow_funded=args.allow_funded
            )
        return await swaps.reconcile(args.swap_id, signer)
    finally:
        swaps.store.close()
        await api.close()
        if signer:
            await signer.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", required=True, help="owner-reviewed JSON policy")
    parser.add_argument(
        "--database",
        required=True,
        help="durable private SQLite journal; reuse it across runs",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("preflight")
    quote = commands.add_parser("quote")
    quote.add_argument("--amount", required=True)
    quote.add_argument(
        "--firm",
        action="store_true",
        help="obtain deposit address for review; does not transfer funds",
    )
    show = commands.add_parser("show")
    show.add_argument("swap_id")
    cancel = commands.add_parser("cancel")
    cancel.add_argument("swap_id")
    execute = commands.add_parser("execute")
    execute.add_argument("swap_id")
    execute.add_argument(
        "--approve", required=True, help="exact reviewed approval_digest"
    )
    execute.add_argument("--allow-funded", action="store_true")
    execute.add_argument("--key-file", required=True)
    status = commands.add_parser("status")
    status.add_argument("swap_id")
    status.add_argument(
        "--key-file", help="optional signer identity to also query deposit finality"
    )
    try:
        print(json.dumps(asyncio.run(run(parser.parse_args(argv))), indent=2))
        return 0
    except Rejected as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    except (OSError, KeyError, TypeError, ValueError):
        # Provider bodies and key material are never included in CLI failures.
        print(
            json.dumps(
                {
                    "error": "Intents operation rejected or unavailable; inspect configuration and reconcile any existing deposit before retrying"
                }
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

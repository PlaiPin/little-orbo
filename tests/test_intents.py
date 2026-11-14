"""Offline signed protocol fixtures; no provider calls or funded transactions."""

import base64
from copy import deepcopy
import hashlib
import json

import httpx
from nacl.signing import SigningKey, VerifyKey
import pytest

from orbo_runtime.contracts import Rejected
from orbo_runtime.intents import OneClick, Policy, Swaps, quote_hash, verify_quote
from orbo_runtime.near_signer import NearSigner, b58encode, encode_transfer

NOW = 1800000000
MANAGER = SigningKey(bytes(range(32)))
KEY = b58encode(bytes(MANAGER.verify_key))
POLICY = {
    "sender": "owner.near",
    "origin_contract": "wrap.near",
    "destination_asset": "nep141:usdt.tether-token.near",
    "recipient": "recipient.near",
    "max_input": "100",
    "max_total_input": "150",
    "min_output": "10",
    "slippage_bps": 50,
    "deadline_seconds": 300,
}
TOKENS = [
    {
        "assetId": "nep141:wrap.near",
        "blockchain": "near",
        "contractAddress": "wrap.near",
    },
    {
        "assetId": POLICY["destination_asset"],
        "blockchain": "near",
        "contractAddress": "usdt.tether-token.near",
    },
]


def signed(request):
    quote = {
        "amountIn": request["amount"],
        "amountOut": "12",
        "minAmountOut": "10",
        "amountInFormatted": "0.1",
        "amountInUsd": "0.1",
        "minAmountIn": request["amount"],
        "amountOutFormatted": "0.12",
        "amountOutUsd": "0.12",
    }
    if not request["dry"]:
        quote.update(
            depositAddress="deposit.near",
            deadline=request["deadline"],
            timeWhenInactive=request["deadline"],
            timeEstimate=30,
        )
    result = {
        "quoteRequest": request,
        "quote": quote,
        "timestamp": "2027-01-15T08:00:00Z",
    }
    result["signature"] = "ed25519:" + b58encode(
        MANAGER.sign(quote_hash(result).encode()).signature
    )
    return result


class Fixture:
    def __init__(self):
        self.quote = None
        self.calls = []
        self.status = "PROCESSING"
        self.mutate = None

    def handle(self, request):
        self.calls.append(request)
        if request.url.path == "/v0/tokens":
            return httpx.Response(200, json=TOKENS)
        if request.url.path == "/v0/quote":
            self.quote = signed(json.loads(request.content))
            if self.mutate:
                self.mutate(self.quote)
            return httpx.Response(200, json=self.quote)
        assert request.url.params["depositAddress"] == "deposit.near"
        return httpx.Response(
            200,
            json={
                "quoteResponse": self.quote,
                "status": self.status,
                "swapDetails": {
                    "refundedAmount": "100" if self.status == "REFUNDED" else "0"
                },
            },
        )


class Signer:
    sender = "owner.near"

    def __init__(self):
        self.prepares = 0
        self.broadcasts = []
        self.timeout = True

    async def prepare(self, contract, recipient, amount):
        assert (contract, recipient, amount) == ("wrap.near", "deposit.near", "100")
        self.prepares += 1
        return {
            "tx_hash": "fixture-hash",
            "signed_tx_base64": "fixture-signed-bytes",
            "sender": self.sender,
        }

    async def broadcast(self, prepared):
        self.broadcasts.append(prepared.copy())
        if self.timeout:
            raise Rejected("uncertain fixture timeout")
        return await self.status(prepared)

    async def status(self, prepared):
        return {
            "transaction": {"hash": prepared["tx_hash"]},
            "final_execution_status": "FINAL",
            "status": {"SuccessValue": ""},
        }


@pytest.fixture
def setup(tmp_path):
    fixture = Fixture()
    client = httpx.AsyncClient(transport=httpx.MockTransport(fixture.handle))
    api = OneClick(client=client, manager_key=KEY, token="fixture-only")
    swaps = Swaps(tmp_path / "swaps.db", Policy(POLICY), api, now=lambda: NOW)
    yield swaps, fixture
    swaps.store.close()


def test_policy_rejects_unbounded_or_noncanonical_values():
    for key, value in [
        ("max_input", "1.0"),
        ("max_input", "01"),
        ("max_input", "0"),
        ("slippage_bps", True),
        ("slippage_bps", 101),
        ("deadline_seconds", 601),
        ("recipient", ""),
    ]:
        with pytest.raises(Rejected):
            Policy({**POLICY, key: value})


def test_signature_covers_money_destination_and_deposit():
    result = signed(Policy(POLICY).request("100", False, NOW))
    verify_quote(result, KEY)
    for section, key, value in [
        ("quote", "depositAddress", "attacker.near"),
        ("quote", "minAmountOut", "1"),
        ("quoteRequest", "recipient", "attacker.near"),
    ]:
        changed = deepcopy(result)
        changed[section][key] = value
        with pytest.raises(Rejected, match="signature"):
            verify_quote(changed, KEY)


async def test_dry_quote_never_signs_and_firm_requires_digest_and_optin(setup):
    swaps, fixture = setup
    row = await swaps.quote("100")
    signer = Signer()
    with pytest.raises(Rejected):
        await swaps.execute(
            row["id"], row["approval_digest"], signer, allow_funded=True
        )
    row = await swaps.quote("100", dry=False)
    with pytest.raises(Rejected, match="opt-in"):
        await swaps.execute(row["id"], row["approval_digest"], signer)
    with pytest.raises(Rejected, match="digest"):
        await swaps.execute(row["id"], "wrong", signer, allow_funded=True)
    assert signer.prepares == 0
    assert all(r.headers["X-API-Key"] == "fixture-only" for r in fixture.calls)


async def test_uncertain_broadcast_survives_restart_and_reuses_exact_bytes(setup):
    swaps, fixture = setup
    row = await swaps.quote("100", dry=False)
    signer = Signer()
    with pytest.raises(Rejected, match="timeout"):
        await swaps.execute(
            row["id"], row["approval_digest"], signer, allow_funded=True
        )
    assert swaps.get(row["id"])["deposit_state"] == "uncertain"
    reopened = Swaps(swaps.path, swaps.policy, swaps.api, now=lambda: NOW)
    try:
        signer.timeout = False
        done = await reopened.execute(
            row["id"], row["approval_digest"], signer, allow_funded=True
        )
        assert done["deposit_state"] == "final"
        assert "prepared" not in done
        assert signer.prepares == 1
        assert signer.broadcasts[0] == signer.broadcasts[1]
        await reopened.execute(
            row["id"], row["approval_digest"], signer, allow_funded=True
        )
        assert len(signer.broadcasts) == 2
        fixture.status = "REFUNDED"
        done = await reopened.reconcile(row["id"], signer)
        assert done["swap_status"] == "REFUNDED"
        assert done["settlement_receipt"]["swapDetails"]["refundedAmount"] == "100"
    finally:
        reopened.store.close()


async def test_uncertain_deposit_blocks_new_nonce_and_expired_cannot_send(setup):
    swaps, _ = setup
    first = await swaps.quote("100", dry=False)
    second = await swaps.quote("100", dry=False)
    signer = Signer()
    with pytest.raises(Rejected):
        await swaps.execute(
            first["id"], first["approval_digest"], signer, allow_funded=True
        )
    with pytest.raises(Rejected, match="unresolved"):
        await swaps.execute(
            second["id"], second["approval_digest"], signer, allow_funded=True
        )
    swaps.now = lambda: NOW + 400
    with pytest.raises(Rejected, match="expired"):
        await swaps.execute(
            first["id"], first["approval_digest"], signer, allow_funded=True
        )
    assert signer.prepares == 1 and len(signer.broadcasts) == 1
    # Reconciliation remains available after deadline, without another transfer.
    result = await swaps.reconcile(first["id"], signer)
    assert result["deposit_state"] == "final"


async def test_total_budget_and_changed_policy_fail_before_signature(setup):
    swaps, _ = setup
    signer = Signer()
    signer.timeout = False
    row = await swaps.quote("100", dry=False)
    await swaps.execute(row["id"], row["approval_digest"], signer, allow_funded=True)
    second = await swaps.quote("100", dry=False)
    with pytest.raises(Rejected, match="cumulative"):
        await swaps.execute(
            second["id"], second["approval_digest"], signer, allow_funded=True
        )
    swaps.policy = Policy({**POLICY, "recipient": "different.near"})
    with pytest.raises(Rejected, match="policy changed"):
        await swaps.execute(
            second["id"], second["approval_digest"], signer, allow_funded=True
        )
    assert signer.prepares == 1


async def test_signed_quote_cannot_change_requested_route(setup):
    swaps, fixture = setup

    def mutate(result):
        result["quoteRequest"]["recipient"] = "different.near"
        result["signature"] = b58encode(
            MANAGER.sign(quote_hash(result).encode()).signature
        )

    fixture.mutate = mutate
    with pytest.raises(Rejected, match="route"):
        await swaps.quote("100", dry=False)


async def test_status_rejects_other_quote_and_rpc_hash(setup):
    swaps, fixture = setup
    row = await swaps.quote("100", dry=False)
    fixture.quote = signed(Policy(POLICY).request("99", False, NOW))
    with pytest.raises(Rejected, match="another quote"):
        await swaps.reconcile(row["id"])
    row["deposit_tx_hash"] = "expected"
    with pytest.raises(Rejected, match="another transaction"):
        swaps.record_deposit(row, {"transaction": {"hash": "other"}})


def keyfile(tmp_path):
    seed = SigningKey(bytes(range(32)))
    path = tmp_path / "key.json"
    path.write_text(
        json.dumps(
            {
                "account_id": "owner.near",
                "public_key": "ed25519:" + b58encode(bytes(seed.verify_key)),
                "private_key": "ed25519:"
                + b58encode(bytes(seed) + bytes(seed.verify_key)),
            }
        )
    )
    path.chmod(0o600)
    return path


async def test_real_signer_builds_verified_restricted_transfer_and_rpc(tmp_path):
    calls = []

    def handler(request):
        data = json.loads(request.content)
        calls.append(data)
        if data["method"] == "query":
            params = data["params"]
            if params["request_type"] == "view_access_key":
                result = {
                    "nonce": 41,
                    "block_hash": b58encode(bytes(range(32))),
                    "permission": "FullAccess",
                }
            else:
                value = (
                    {"total": "100"}
                    if params["method_name"] == "storage_balance_of"
                    else "1000"
                )
                result = {"result": list(json.dumps(value).encode())}
        else:
            result = {"final_execution_status": "FINAL"}
        return httpx.Response(200, json={"result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        signer = NearSigner("owner.near", keyfile(tmp_path), client=client)
        prepared = await signer.prepare("wrap.near", "deposit.near", "100")
        signed_tx = base64.b64decode(prepared["signed_tx_base64"])
        raw = signed_tx[:-65]
        VerifyKey(signer.public).verify(hashlib.sha256(raw).digest(), signed_tx[-64:])
        assert raw == encode_transfer(
            "owner.near",
            signer.public,
            42,
            "wrap.near",
            bytes(range(32)),
            "deposit.near",
            "100",
            30_000_000_000_000,
        )
        assert b"ft_transfer" in raw and b'"amount":"100"' in raw
        assert prepared["tx_hash"] == b58encode(hashlib.sha256(raw).digest())
        await signer.broadcast(prepared)
        await signer.status(prepared)
    assert calls[-2]["method"] == "send_tx" and calls[-1]["method"] == "tx"
    assert calls[-2]["params"]["signed_tx_base64"] == prepared["signed_tx_base64"]


def test_key_permissions_and_account_binding(tmp_path):
    path = keyfile(tmp_path)
    path.chmod(0o644)
    with pytest.raises(Rejected, match="owner-only"):
        NearSigner("owner.near", path)
    path.chmod(0o600)
    with pytest.raises(Rejected, match="account"):
        NearSigner("other.near", path)


def test_official_javascript_sdk_golden_vectors():
    # Generated independently with @near-js/transactions 2.5.1:
    # encodeTransaction(createTransaction('owner.near', PublicKey.from(KEY),
    # 'wrap.near', 42n, [functionCall('ft_transfer', Buffer.from(ARGS),
    # 30000000000000n, 1n)], Uint8Array.from({length:32}, (_,i)=>i))).
    expected = (
        "0a0000006f776e65722e6e6561720003a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
        "2a0000000000000009000000777261702e6e656172000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f"
        "01000000020b00000066745f7472616e736665722d0000007b2272656365697665725f6964223a226465706f7369742e6e656172222c"
        "22616d6f756e74223a22313030227d00e057eb481b000001000000000000000000000000000000"
    )
    assert (
        encode_transfer(
            "owner.near",
            bytes(MANAGER.verify_key),
            42,
            "wrap.near",
            bytes(range(32)),
            "deposit.near",
            "100",
            30_000_000_000_000,
        ).hex()
        == expected
    )
    # @defuse-protocol/one-click-sdk-typescript 0.1.26 quoteHash() on this
    # fixture, independent of Python's implementation and fixture signature.
    result = signed(Policy(POLICY).request("100", False, NOW))
    assert quote_hash(result) == "E4X3W8JstTYmvr6Q5d7r73QT2kjEQhBRCYvA9Eq8GP7r"


async def test_cancelled_quote_cannot_be_executed(setup):
    swaps, _ = setup
    row = await swaps.quote("100", dry=False)
    assert swaps.cancel(row["id"])["state"] == "cancelled"
    signer = Signer()
    with pytest.raises(Rejected):
        await swaps.execute(
            row["id"], row["approval_digest"], signer, allow_funded=True
        )
    assert signer.prepares == 0


async def test_oversized_and_redirected_provider_responses_rejected():
    for response in [
        httpx.Response(200, content=b"x" * 2_000_001),
        httpx.Response(302, headers={"location": "https://elsewhere.invalid"}),
    ]:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda request: response)
        ) as client:
            api = OneClick(client=client)
            with pytest.raises(Rejected):
                await api.tokens()


def test_restricted_serializer_checks_money_at_its_boundary():
    for amount in ["0", "-1", "01", "1.0", str(2**128)]:
        with pytest.raises(Rejected):
            encode_transfer(
                "owner.near",
                bytes(MANAGER.verify_key),
                42,
                "wrap.near",
                bytes(range(32)),
                "deposit.near",
                amount,
                30_000_000_000_000,
            )


async def test_live_observed_quote_response_normalization_and_platform_fees(setup):
    swaps, fixture = setup

    def normalized(result):
        # Actual API returns these defaults, including appFees outside the
        # signed subset. Net output is still signed and checked against policy.
        result["quoteRequest"]["appFees"] = [{"recipient": "platform.near", "fee": 20}]
        result["quoteRequest"]["depositMode"] = "SIMPLE"

    fixture.mutate = normalized
    original = fixture.handle

    def created(request):
        response = original(request)
        if request.url.path == "/v0/quote":
            return httpx.Response(201, json=response.json())
        return response

    swaps.api.client = httpx.AsyncClient(transport=httpx.MockTransport(created))
    result = await swaps.quote("100", dry=False)
    assert result["quote"]["quoteRequest"]["deadline"].endswith(".000Z")
    assert result["quote"]["quote"]["minAmountOut"] == "10"


async def test_firm_quote_implicit_account_and_service_extension_keep_owner_deadline(
    setup,
):
    swaps, fixture = setup

    def extended_service_deadline(result):
        # Observed on the public API: execution fields may advertise a longer
        # lifetime than the signed request. Local owner deadline still wins.
        result["quote"]["depositAddress"] = "d" * 64
        result["quote"]["deadline"] = "2027-01-18T08:00:00.000Z"
        result["quote"]["timeWhenInactive"] = "2027-01-18T08:00:00.000Z"
        result["signature"] = b58encode(
            MANAGER.sign(quote_hash(result).encode()).signature
        )

    fixture.mutate = extended_service_deadline
    row = await swaps.quote("100", dry=False)
    assert row["quote"]["quote"]["depositAddress"] == "d" * 64
    assert row["expires"] == NOW + POLICY["deadline_seconds"]
    swaps.now = lambda: NOW + 301
    signer = Signer()
    with pytest.raises(Rejected, match="expired"):
        await swaps.execute(
            row["id"], row["approval_digest"], signer, allow_funded=True
        )
    assert signer.prepares == 0

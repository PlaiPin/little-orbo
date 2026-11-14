"""Minimal NEAR mainnet NEP-141 signer; persisted bytes are safe to rebroadcast.

Wire layout follows @near-js/transactions 2.5.1's Transaction/FunctionCall schema.
Only a single ft_transfer action is supported. There is no general contract-call
interface, automatic storage deposit, key creation, or native token conversion.
"""

import base64
import hashlib
import json
import os
import re
import stat
import struct

import httpx

from .contracts import Rejected

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
ACCOUNT = re.compile(r"(?=.{2,64}\Z)[a-z0-9]+(?:[._-][a-z0-9]+)*\Z")


def account(value):
    if not isinstance(value, str) or not ACCOUNT.fullmatch(value):
        raise Rejected("invalid NEAR account")
    return value


def b58encode(value):
    number = int.from_bytes(value, "big")
    result = ""
    while number:
        number, digit = divmod(number, 58)
        result = ALPHABET[digit] + result
    return "1" * (len(value) - len(value.lstrip(b"\0"))) + result


def b58decode(value):
    if not isinstance(value, str) or len(value) > 128:
        raise Rejected("invalid base58 value")
    number = 0
    for char in value:
        if char not in ALPHABET:
            raise Rejected("invalid base58 value")
        number = number * 58 + ALPHABET.index(char)
    return b"\0" * (len(value) - len(value.lstrip("1"))) + number.to_bytes(
        (number.bit_length() + 7) // 8, "big"
    )


async def request_json(client, method, url, *, success_statuses=(200,), **kwargs):
    """Bound bytes while reading, disable redirects, and omit remote error text."""
    try:
        async with client.stream(
            method, url, follow_redirects=False, **kwargs
        ) as response:
            if response.status_code not in success_statuses:
                raise Rejected(
                    f"provider request failed with HTTP {response.status_code}"
                )
            data = bytearray()
            async for chunk in response.aiter_bytes():
                if len(data) + len(chunk) > 2_000_000:
                    raise Rejected("provider response exceeds byte limit")
                data.extend(chunk)
            return json.loads(data)
    except (httpx.HTTPError, ValueError) as exc:
        if isinstance(exc, Rejected):
            raise
        raise Rejected("provider transport or response failed") from None


def _bytes(value):
    if isinstance(value, str):
        value = value.encode()
    return struct.pack("<I", len(value)) + value


def encode_transfer(
    sender, public_key, nonce, contract, block_hash, recipient, amount, gas
):
    """Serialize the restricted v0 transaction, not a signed transaction."""
    for value in (sender, contract, recipient):
        account(value)
    if len(public_key) != 32 or len(block_hash) != 32 or not 0 < nonce < 2**64:
        raise Rejected("invalid NEAR transaction inputs")
    if (
        not isinstance(amount, str)
        or not amount.isascii()
        or not amount.isdigit()
        or amount.startswith("0")
        or len(amount) > 39
        or not 0 < int(amount) < 2**128
    ):
        raise Rejected(
            "transfer amount must be a positive canonical u128 decimal string"
        )
    if type(gas) is not int or not 1 <= gas <= 30_000_000_000_000:
        raise Rejected("gas exceeds restricted transfer ceiling")
    args = json.dumps(
        {"receiver_id": recipient, "amount": amount}, separators=(",", ":")
    ).encode()
    action = (
        b"\x02"
        + _bytes("ft_transfer")
        + _bytes(args)
        + struct.pack("<Q", gas)
        + (1).to_bytes(16, "little")
    )
    return (
        _bytes(sender)
        + b"\0"
        + public_key
        + struct.pack("<Q", nonce)
        + _bytes(contract)
        + block_hash
        + struct.pack("<I", 1)
        + action
    )


class NearSigner:
    """Dedicated Ed25519 full-access key, scoped by the caller's approved policy.

    A full-access key is required by NEAR for attached deposits. Keep this key on
    a separately funded account and do not share it with another transaction
    sender. The service persists a signature before sending and serializes its
    own deposits; it cannot coordinate nonces with outside wallet processes.
    """

    def __init__(
        self, sender, key_file=None, *, values=None, client=None, gas=30_000_000_000_000
    ):
        from nacl.signing import SigningKey

        self.sender = account(sender)
        self.gas = gas
        if type(gas) is not int or not 1 <= gas <= 30_000_000_000_000:
            raise Rejected("gas exceeds restricted transfer ceiling")
        if key_file is not None and values is not None:
            raise Rejected("choose one signer credential source")
        if key_file is not None:
            from .credentials import external_path

            path = external_path(key_file)
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd) as source:
                info = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(info.st_mode)
                    or info.st_mode & 0o077
                    or info.st_uid != os.geteuid()
                    or info.st_size > 16_384
                ):
                    raise Rejected("signer key must be an owner-only regular file")
                contents = source.read(16_385)
                if len(contents) > 16_384:
                    raise Rejected("signer key file exceeds size limit")
                value = json.loads(contents)
        elif isinstance(values, dict):
            # Only construct this mapping from the shared external credential
            # resolver, or a deliberately injected signer in an offline test.
            value = values
        else:
            raise Rejected("external signer credentials required")
        if not isinstance(value, dict):
            raise Rejected("invalid signer credential mapping")
        if value.get("account_id") != sender:
            raise Rejected("key account does not match approved sender")
        secret = value.get("private_key", "")
        if not isinstance(secret, str) or not secret.startswith("ed25519:"):
            raise Rejected("only Ed25519 keys are supported")
        raw = b58decode(secret.split(":", 1)[1])
        if len(raw) not in {32, 64}:
            raise Rejected("invalid Ed25519 key")
        self.key = SigningKey(raw[:32])
        self.public = bytes(self.key.verify_key)
        if len(raw) == 64 and raw[32:] != self.public:
            raise Rejected("invalid Ed25519 public-key suffix")
        self.public_key = "ed25519:" + b58encode(self.public)
        if value.get("public_key") != self.public_key:
            raise Rejected("key public identity does not match")
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        )
        self.owns_client = client is None

    async def close(self):
        if self.owns_client:
            await self.client.aclose()

    async def rpc(self, method, params):
        data = await request_json(
            self.client,
            "POST",
            "https://rpc.mainnet.near.org",
            json={"jsonrpc": "2.0", "id": "orbo", "method": method, "params": params},
        )
        if not isinstance(data, dict) or "error" in data or "result" not in data:
            raise Rejected("NEAR RPC result unavailable; reconcile before retrying")
        return data["result"]

    async def _view(self, contract, method, args):
        result = await self.rpc(
            "query",
            {
                "request_type": "call_function",
                "finality": "final",
                "account_id": contract,
                "method_name": method,
                "args_base64": base64.b64encode(json.dumps(args).encode()).decode(),
            },
        )
        try:
            return json.loads(bytes(result["result"]))
        except (KeyError, ValueError, TypeError):
            raise Rejected("invalid token view response") from None

    async def prepare(self, contract, recipient, amount):
        account(contract)
        account(recipient)
        # An unregistered deposit account would make ft_transfer fail. Never add
        # an unapproved storage_deposit action to compensate.
        registered = await self._view(
            contract, "storage_balance_of", {"account_id": recipient}
        )
        if not registered or int(registered.get("total", "0")) <= 0:
            raise Rejected("deposit account lacks token storage registration")
        balance = await self._view(
            contract, "ft_balance_of", {"account_id": self.sender}
        )
        if int(balance) < int(amount):
            raise Rejected("insufficient source token balance")
        access = await self.rpc(
            "query",
            {
                "request_type": "view_access_key",
                "finality": "final",
                "account_id": self.sender,
                "public_key": self.public_key,
            },
        )
        if access.get("permission") != "FullAccess":
            raise Rejected("NEP-141 transfer requires a full-access key")
        raw = encode_transfer(
            self.sender,
            self.public,
            access["nonce"] + 1,
            contract,
            b58decode(access["block_hash"]),
            recipient,
            amount,
            self.gas,
        )
        tx_hash = hashlib.sha256(raw).digest()
        signed = raw + b"\0" + self.key.sign(tx_hash).signature
        return {
            "tx_hash": b58encode(tx_hash),
            "signed_tx_base64": base64.b64encode(signed).decode(),
            "sender": self.sender,
        }

    async def broadcast(self, prepared):
        return await self.rpc(
            "send_tx",
            {"signed_tx_base64": prepared["signed_tx_base64"], "wait_until": "FINAL"},
        )

    async def status(self, prepared):
        return await self.rpc(
            "tx",
            {
                "tx_hash": prepared["tx_hash"],
                "sender_account_id": prepared["sender"],
                "wait_until": "FINAL",
            },
        )

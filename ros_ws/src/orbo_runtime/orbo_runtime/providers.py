"""Real provider adapters. No retries of billed requests or silent model fallback."""

import asyncio
import dataclasses
import json
import re


from .contracts import Rejected, digest
from .credentials import credential


def parse_json_reply(content):
    """Decode JSON with at most one whole-message Markdown fence.

    Call only after response verification. Never extract JSON from surrounding
    prose; callers still enforce their complete schema and authorization checks.
    """
    text = content.strip()
    fenced = re.fullmatch(r"```(?:json)?[ \t]*\r?\n([\s\S]*?)\r?\n```", text)
    return json.loads(fenced.group(1) if fenced else text)


def serializable(value):
    if dataclasses.is_dataclass(value):
        return serializable(dataclasses.asdict(value))
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, bytes):
        return value.hex()
    if isinstance(value, dict):
        return {str(k): serializable(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [serializable(v) for v in value]
    return value


@dataclasses.dataclass(frozen=True)
class VerifiedReply:
    message: dict
    evidence: dict
    usage: dict


class NearReasoner:
    def __init__(
        self, *, model="z-ai/glm-5.3-flash", timeout=40, client=None, api_key=None
    ):
        if not model or timeout <= 0 or timeout > 120:
            raise Rejected("invalid reasoning configuration")
        # Provider provenance is independent of whether the robot is simulated.
        # An injected SDK client is test evidence until separately established.
        self.provenance = "live_service" if client is None else "fixture"
        if client is None:
            from nearai_inference_sdk import InferenceClient

            client = InferenceClient(
                api_key if api_key is not None else credential("NEARAI_API_KEY"),
                e2ee=True,
                attestation_cache_time_to_live_ms=0,
            )
        self.client, self.model, self.timeout = client, model, timeout

    async def complete(self, messages, tools):
        async with asyncio.timeout(self.timeout):
            deployment = await self.client.verify(self.model)
            if not deployment.models:
                raise Rejected("model attestation required")
            # Nonstreamed, bounded completion deliberately holds ALL text and tools.
            completion = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                max_completion_tokens=1024,
                stream=False,
            )
            verified = await self.client.verify_response(completion.id)
            if (
                verified.id != completion.id
                or verified.signature_kind != "provider_tee"
            ):
                raise Rejected("attested model response signature required")
            if completion.model != self.model:
                raise Rejected("response model mismatch")
            if len(completion.choices) != 1 or completion.choices[
                0
            ].finish_reason not in {"stop", "tool_calls"}:
                raise Rejected("incomplete response")
            message = completion.choices[0].message.model_dump(
                mode="json", exclude_none=True
            )
            if len(json.dumps(message)) > 32768:
                raise Rejected("response exceeds local limit")
            evidence = {
                "provider": "near",
                "provenance": self.provenance,
                "model": self.model,
                "e2ee": True,
                "signature_kind": verified.signature_kind,
                "response_id": completion.id,
                "verified_at": deployment.verified_at,
                "verification": serializable(verified),
                "message_digest": digest(message),
            }
            return VerifiedReply(
                message,
                evidence,
                completion.usage.model_dump(mode="json") if completion.usage else {},
            )

    async def close(self):
        await self.client.aclose()


class ProviderError(RuntimeError):
    def __init__(self, provider, status):
        # Deliberately exclude response bodies, URLs and credentials from errors.
        super().__init__(f"{provider} request failed ({status})")
        self.provider, self.status = provider, status


def check_response(response, provider):
    if response.status_code != 200:
        raise ProviderError(provider, response.status_code)

"""Real provider adapters. No retries of billed requests or silent model fallback."""

import asyncio
import dataclasses
from decimal import Decimal, InvalidOperation
import io
import json
import os
from pathlib import Path
import stat
import wave

import httpx

from .contracts import Rejected, digest


def credential(name):
    """Prefer systemd credentials, otherwise a deployment-injected environment value."""
    directory = os.environ.get("CREDENTIALS_DIRECTORY")
    if directory:
        path = Path(directory) / name
        if path.exists():
            if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
                raise Rejected("credential file must be owner-only")
            value = path.read_text().strip()
            if value:
                return value
    value = os.environ.get(name, "").strip()
    if not value:
        raise Rejected(f"missing credential: {name}")
    return value


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


class NearTranscriber:
    """TLS multipart transcription; not covered by verified-chat E2EE."""

    def __init__(self, *, model="openai/whisper-large-v3", client=None, api_key=None):
        self.model = model
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        )
        self.api_key = api_key or credential("NEARAI_API_KEY")

    async def transcribe(self, wav_bytes):
        if len(wav_bytes) > 2_000_000:
            raise Rejected("utterance too large")
        with wave.open(io.BytesIO(wav_bytes)) as audio:
            if (
                audio.getnchannels() != 1
                or audio.getsampwidth() != 2
                or audio.getframerate() != 16000
                or not 0 < audio.getnframes() <= 16000 * 30
            ):
                raise Rejected("expected bounded 16kHz mono PCM utterance")
        async with asyncio.timeout(35):
            response = await self.client.post(
                "https://cloud-api.near.ai/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + self.api_key},
                data={"model": self.model},
                files={"file": ("utterance.wav", wav_bytes, "audio/wav")},
            )
        check_response(response, "near-transcription")
        payload = response.json()
        text = payload.get("text")
        if not isinstance(text, str) or not 0 < len(text) <= 8000:
            raise Rejected("invalid transcript")
        return text, {
            "model": self.model,
            "protection": "https_only",
            "chat_e2ee": False,
            "request_id": response.headers.get("x-request-id"),
            "response_id": payload.get("id"),
        }

    async def close(self):
        await self.client.aclose()


class FishSynthesizer:
    MODELS = {"s1", "s2-pro", "s2.1-pro", "s2.1-pro-free", "drama-3"}

    def __init__(
        self,
        *,
        model,
        voice,
        sample_rate=24000,
        low_credit="1",
        client=None,
        api_key=None,
    ):
        if model not in self.MODELS or not isinstance(voice, str) or not voice.strip():
            raise Rejected("explicit supported Fish model and voice required")
        if sample_rate not in {8000, 16000, 24000, 32000, 44100}:
            raise Rejected("unsupported PCM sample rate")
        self.model, self.voice, self.sample_rate = model, voice, sample_rate
        self.low_credit = Decimal(low_credit)
        if not self.low_credit.is_finite() or self.low_credit < 0:
            raise Rejected("invalid balance threshold")
        self.client = client or httpx.AsyncClient(
            timeout=30, follow_redirects=False, trust_env=False
        )
        self.api_key = api_key or credential("FISH_API_KEY")
        self.last_balance = None

    async def balance(self):
        self.last_balance = None
        response = await self.client.get(
            "https://api.fish.audio/wallet/self/api-credit",
            headers={"Authorization": "Bearer " + self.api_key},
        )
        check_response(response, "fish-balance")
        payload = response.json()
        try:
            if not isinstance(payload["credit"], str) or not isinstance(
                payload["cumulative_top_up"], str
            ):
                raise ValueError()
            credit, topups = (
                Decimal(payload["credit"]),
                Decimal(payload["cumulative_top_up"]),
            )
            if not credit.is_finite() or not topups.is_finite() or topups < 0:
                raise ValueError()
        except (KeyError, ValueError, InvalidOperation):
            raise Rejected("invalid API credit response") from None
        self.last_balance = {
            "credit": str(credit),
            "cumulative_top_up": str(topups),
            "low": credit < self.low_credit,
            "account": payload.get("user_id"),
            "instrument": "fish_api_credit",
            "funding": "owner_prepaid",
        }
        return self.last_balance

    async def synthesize(self, text):
        if not isinstance(text, str) or not 0 < len(text) <= 4000:
            raise Rejected("speech text exceeds limit")
        # Unknown balance blocks new synthesis. No purchase or provider fallback.
        balance = await self.balance()
        if Decimal(balance["credit"]) <= 0:
            raise ProviderError("fish", 402)
        async with asyncio.timeout(60):
            async with self.client.stream(
                "POST",
                "https://api.fish.audio/v1/tts",
                headers={
                    "Authorization": "Bearer " + self.api_key,
                    "model": self.model,
                },
                json={
                    "text": text,
                    "reference_id": self.voice,
                    "format": "pcm",
                    "sample_rate": self.sample_rate,
                    "latency": "normal",
                },
            ) as response:
                check_response(response, "fish-tts")
                total = 0
                tail = b""
                async for chunk in response.aiter_bytes(4096):
                    total += len(chunk)
                    if total > self.sample_rate * 2 * 120:
                        raise Rejected("speech stream exceeds duration limit")
                    chunk = tail + chunk
                    length = len(chunk) // 2 * 2
                    if length:
                        yield chunk[:length]
                    tail = chunk[length:]
                if tail or total == 0:
                    raise Rejected("truncated or empty PCM stream")

    async def close(self):
        await self.client.aclose()

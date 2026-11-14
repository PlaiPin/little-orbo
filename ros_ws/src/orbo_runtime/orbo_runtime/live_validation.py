"""Explicit, bounded, hardware-free NEAR/Fish smoke validation. No Commons calls."""

import argparse
from contextlib import aclosing
import asyncio
from datetime import datetime, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid
import wave

from .contracts import Rejected, digest
from .credentials import initialize as initialize_credentials, read_values
from .providers import (
    FishSynthesizer,
    NearReasoner,
    NearTranscriber,
    ProviderError,
    VerifiedReply,
)

PROJECT_ROOT = next(
    (
        parent
        for parent in Path(__file__).resolve().parents
        if (parent / "pyproject.toml").is_file()
    ),
    Path.cwd(),
)
DEFAULT_DIRECTORY = PROJECT_ROOT / ".live-validation"
DEFAULT_CONFIG = {
    "version": 1,
    "near_model": "z-ai/glm-5.3-flash",
    "transcription_model": "openai/whisper-large-v3",
    "fish_model": "s2.1-pro",
}
FIELDS = ("NEARAI_API_KEY", "FISH_API_KEY", "FISH_VOICE_ID")
PHRASE = "Orbo validation. The blue circle is beside the green square."
SAMPLE_RATE = 16000
MAX_AUDIO_BYTES = SAMPLE_RATE * 2 * 10


class SetupError(Exception):
    """Only fixed messages from this module may be displayed."""


def private_directory(path, *, create=False):
    path = Path(os.path.abspath(path.expanduser()))
    for part in (path, *path.parents):
        if part.is_symlink():
            raise SetupError("Symlink directories are not allowed.")
    if create:
        previous = os.umask(0o077)
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        finally:
            os.umask(previous)
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
    ):
        raise SetupError("Validation directory must be owned by you with mode 0700.")
    return path


def private_open(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_uid != os.getuid()
        or info.st_mode & 0o077
        or info.st_nlink != 1
    ):
        os.close(fd)
        raise SetupError(
            "Validation files must be regular, owner-only files without links."
        )
    return fd


def read_private(path):
    fd = private_open(path, os.O_RDONLY)
    with os.fdopen(fd, "r", encoding="utf-8") as stream:
        value = stream.read(16385)
    if len(value) > 16384:
        raise SetupError("Validation file exceeds its size limit.")
    return value


def initialize(directory):
    directory = private_directory(directory, create=True)
    initialize_credentials()
    templates = {
        "config.json": json.dumps(DEFAULT_CONFIG, indent=2) + "\n",
    }
    for name, value in templates.items():
        try:
            fd = private_open(directory / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            # Preserve existing content; check its security without displaying it.
            read_private(directory / name)
            continue
        with os.fdopen(fd, "w") as stream:
            stream.write(value)
    private_directory(directory / "reports", create=True)
    return directory


def read_credentials():
    """Read the shared credential file; never consult ambient provider keys."""
    try:
        values = read_values()
    except Rejected:
        raise SetupError(
            "Shared credential file is unavailable or invalid."
        ) from None
    return {name: values[name] for name in FIELDS if name in values}


def load_setup(directory, service, transcribe):
    directory = private_directory(directory)
    config = json.loads(read_private(directory / "config.json"))
    if (
        not isinstance(config, dict)
        or set(config) != set(DEFAULT_CONFIG)
        or type(config["version"]) is not int
        or config["version"] != 1
        or any(
            not isinstance(config[k], str)
            or not re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", config[k])
            for k in ("near_model", "transcription_model", "fish_model")
        )
        or config["fish_model"] not in FishSynthesizer.MODELS
    ):
        raise SetupError("Invalid config.json; compare with the supplied template.")
    needed = []
    if service in {"near", "both"} or transcribe:
        needed.append("NEARAI_API_KEY")
    if service in {"fish", "both"}:
        needed.extend(("FISH_API_KEY", "FISH_VOICE_ID"))
    if transcribe and service == "near":
        raise SetupError("Transcription requires the Fish-generated test audio.")
    stored = read_credentials()
    credentials, missing = {}, []
    for name in needed:
        value = stored.get(name, "").strip()
        if not value or value.startswith(("REPLACE_", "YOUR_", "<")):
            missing.append(name)
        elif len(value) > 8192 or any(ord(c) < 33 or ord(c) > 126 for c in value):
            raise SetupError(
                "Credential and voice fields must contain one printable value each."
            )
        else:
            credentials[name] = value
    return directory, config, credentials, missing


def plan(service, transcribe):
    return {
        "near_inference_requests": int(service in {"near", "both"}),
        "fish_synthesis_requests": int(service in {"fish", "both"}),
        "near_transcription_requests": int(transcribe),
        "additional_calls": "Attestation/proof retrieval and Fish balance reads; no retries or purchases.",
        "audio_source": "Fixed synthetic phrase; no microphone or private files.",
        "transcription_disclosure": "Fish audio sent to NEAR over HTTPS; not chat E2EE."
        if transcribe
        else "disabled",
        "commons": "excluded",
        "monetary_cost": "Not estimated or hard-capped by this runner; requests can consume prepaid credit.",
    }


def failure_code(error):
    if isinstance(error, ProviderError):
        return {"kind": "provider_error", "http_status": error.status}
    if isinstance(error, (TimeoutError, asyncio.CancelledError)):
        return {"kind": "timeout_or_cancelled"}
    if isinstance(error, Rejected):
        return {"kind": "validation_rejected"}
    return {"kind": "provider_or_runtime_failure"}


def scrub(value, secrets):
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[redacted]")
        return value
    if isinstance(value, dict):
        return {k: scrub(v, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [scrub(v, secrets) for v in value]
    return value


def write_report(path, report, secrets):
    private_directory(path.parent)
    temporary = path.with_name("." + uuid.uuid4().hex + ".tmp")
    try:
        fd = private_open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        with os.fdopen(fd, "w") as stream:
            json.dump(scrub(report, secrets), stream, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        parent_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        temporary.unlink(missing_ok=True)


async def run_checks(
    directory, config, credentials, service, transcribe, *, factories=None
):
    fixture = factories is not None
    factories = factories or (NearReasoner, FishSynthesizer, NearTranscriber)
    reports = private_directory(directory / "reports", create=True)
    report_path = reports / (uuid.uuid4().hex + ".json")
    report = {
        "schema": "orbo.live-validation.v1",
        "provenance": "fixture" if fixture else "live_service",
        "started_utc": datetime.now(timezone.utc).isoformat(),
        "plan": plan(service, transcribe),
        "status": "running",
        "checks": [],
        "limits": [
            "Cloud smoke check only; no robot, Pi timing, acoustic playback or Commons acceptance.",
            "No per-request cost, earned credit, received credit or applied-usage proof.",
            "An interrupted request may already be billed; never automatically resumed.",
        ],
    }
    secrets = list(credentials.values())
    lock = private_open(directory / ".run.lock", os.O_RDWR | os.O_CREAT)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError("Another validation batch is running.") from None
        write_report(report_path, report, secrets)
        pcm = bytearray()
        stages = ["near_reasoning"] if service in {"near", "both"} else []
        stages += ["fish_speech"] if service in {"fish", "both"} else []
        stages += ["near_transcription"] if transcribe else []
        for stage in stages:
            entry = {"check": stage, "status": "in_progress", "outcome": "unknown"}
            report["checks"].append(entry)
            write_report(
                report_path, report, secrets
            )  # Before any possibly billed call.
            client = None
            start = time.monotonic()
            try:
                if stage == "near_reasoning":
                    client = factories[0](
                        model=config["near_model"],
                        api_key=credentials["NEARAI_API_KEY"],
                    )
                    reply = await client.complete(
                        [
                            {
                                "role": "user",
                                "content": "Reply with exactly ORBO_LIVE_OK. Do not call tools.",
                            }
                        ],
                        [],
                    )
                    if (
                        not isinstance(reply, VerifiedReply)
                        or reply.evidence.get("e2ee") is not True
                        or reply.evidence.get("signature_kind") != "provider_tee"
                        or reply.message.get("tool_calls")
                        or reply.message.get("content", "").strip() != "ORBO_LIVE_OK"
                    ):
                        raise Rejected("verified fixed response required")
                    entry.update(
                        {
                            "verified_response": True,
                            "model": config["near_model"],
                            "proof_sha256": digest(reply.evidence),
                            "response_sha256": digest(reply.message),
                            "usage": {
                                k: reply.usage[k]
                                for k in (
                                    "prompt_tokens",
                                    "completion_tokens",
                                    "total_tokens",
                                )
                                if type(reply.usage.get(k)) is int
                                and reply.usage[k] >= 0
                            },
                        }
                    )
                elif stage == "fish_speech":
                    client = factories[1](
                        model=config["fish_model"],
                        voice=credentials["FISH_VOICE_ID"],
                        sample_rate=SAMPLE_RATE,
                        api_key=credentials["FISH_API_KEY"],
                    )
                    async with aclosing(client.synthesize(PHRASE)) as stream:
                        async for chunk in stream:
                            if len(pcm) + len(chunk) > MAX_AUDIO_BYTES:
                                raise Rejected("test audio exceeds ten seconds")
                            pcm.extend(chunk)
                    if not pcm or len(pcm) % 2:
                        raise Rejected("complete PCM required")
                    entry.update(
                        {
                            "stream_complete": True,
                            "model": config["fish_model"],
                            "sample_rate": SAMPLE_RATE,
                            "channels": 1,
                            "sample_width_bytes": 2,
                            "audio_seconds": len(pcm) / (SAMPLE_RATE * 2),
                            "audio_sha256": hashlib.sha256(pcm).hexdigest(),
                            "opening_api_credit": client.last_balance["credit"],
                            "played": False,
                        }
                    )
                    closing = await client.balance()
                    entry["closing_api_credit"] = closing["credit"]
                    entry["cost_attribution"] = (
                        "Account snapshots only; concurrent usage may affect them."
                    )
                else:
                    wav = io.BytesIO()
                    with wave.open(wav, "wb") as audio:
                        audio.setnchannels(1)
                        audio.setsampwidth(2)
                        audio.setframerate(SAMPLE_RATE)
                        audio.writeframes(pcm)
                    client = factories[2](
                        model=config["transcription_model"],
                        api_key=credentials["NEARAI_API_KEY"],
                    )
                    transcript, _ = await client.transcribe(wav.getvalue())

                    def normalize(text):
                        return re.findall(r"[a-z0-9]+", text.lower())

                    match = normalize(transcript) == normalize(PHRASE)
                    entry.update(
                        {
                            "expected_phrase_match": match,
                            "protection": "https_only",
                            "chat_e2ee": False,
                            "transcript_sha256": digest(transcript),
                        }
                    )
                    if not match:
                        raise Rejected("fixed phrase was not recovered")
                entry.update(status="passed", outcome="observed")
            except (Exception, asyncio.CancelledError) as error:
                entry.update(status="failed", error=failure_code(error))
                report["status"] = (
                    "cancelled"
                    if isinstance(error, asyncio.CancelledError)
                    else "failed"
                )
                if isinstance(error, asyncio.CancelledError):
                    raise
                break
            finally:
                try:
                    if client is not None:
                        await client.close()
                except asyncio.CancelledError:
                    entry["cleanup"] = "incomplete"
                    report["status"] = "cancelled"
                    raise
                except Exception:
                    entry["cleanup"] = "incomplete"
                    report["status"] = "failed"
                finally:
                    entry["elapsed_seconds"] = round(time.monotonic() - start, 3)
                    write_report(report_path, report, secrets)
            if report["status"] != "running":
                break
        if report["status"] == "running":
            report["status"] = "passed"
        report["finished_utc"] = datetime.now(timezone.utc).isoformat()
        write_report(report_path, report, secrets)
        return report_path, report["status"]
    finally:
        os.close(lock)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "check", "run"))
    parser.add_argument("--directory", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--service", choices=("near", "fish", "both"), default="both")
    parser.add_argument(
        "--transcribe-fish-audio",
        action="store_true",
        help="Explicitly upload the fixed Fish test audio to NEAR transcription.",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        help="Authorize this one bounded live batch; may consume credit.",
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            path = initialize(args.directory)
            print(
                f"Created/preserved local config/reports at {path} and shared credential template. No network requests."
            )
            return 0
        directory, config, credentials, missing = load_setup(
            args.directory, args.service, args.transcribe_fish_audio
        )
        if missing:
            print(
                "Not ready. Fill these fields in the configured shared credential file: "
                + ", ".join(missing)
            )
            return 2
        if args.command == "check" or not args.execute:
            print(
                json.dumps(
                    {
                        "status": "ready",
                        "executed": False,
                        "plan": plan(args.service, args.transcribe_fish_audio),
                    },
                    indent=2,
                )
            )
            if args.command == "run":
                print(
                    "No network requests. Add --execute only when authorizing this batch."
                )
            return 0
        path, status = asyncio.run(
            run_checks(
                directory, config, credentials, args.service, args.transcribe_fish_audio
            )
        )
        print(f"Validation {status}. Private report: {path}")
        return 0 if status == "passed" else 1
    except SetupError as error:
        print(str(error))
        return 2
    except (OSError, ValueError, TypeError):
        print(
            "Setup or report I/O failed. Check private file paths, permissions and config.json; no values are displayed."
        )
        return 2
    except KeyboardInterrupt:
        print(
            "Interrupted. Inspect the private reports directory before starting another batch; a request may have been billed."
        )
        return 130
    except Exception:
        print(
            "Validation failed. Inspect the private report; provider exception details are suppressed."
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

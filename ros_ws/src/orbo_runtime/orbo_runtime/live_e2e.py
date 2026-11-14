"""Opt-in live integration checks; reports distinguish transport from physical proof.

All requests are bounded and have no automatic effectful retries. Provider text,
audio and image bytes remain in memory. Runtime journals retain their normal
owner-controlled task evidence. This runner never creates runtime grants.
"""

import argparse
import asyncio
import base64
import dataclasses
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import stat
import time
import uuid
import wave

from .contracts import Rejected
from .providers import NearReasoner, credential

STAGES = {"near", "live", "task", "commons"}


class StageFailure(Rejected):
    """Only runner-created, non-content evidence is safe to retain on failure."""

    def __init__(self, code, evidence):
        super().__init__(code)
        self.code, self.evidence = code, evidence


def load_config(path):
    path = Path(path)
    if path.stat().st_size > 65536:
        raise Rejected("configuration exceeds limit")
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or set(config) - {
        "stages",
        "near_model",
        "image",
        "audio",
        "live_seconds",
        "stage_seconds",
        "socket",
        "task",
        "commons",
        "live_backend",
    }:
        raise Rejected("invalid configuration fields")
    stages = config.get("stages", [])
    if (
        not isinstance(stages, list)
        or not stages
        or len(set(stages)) != len(stages)
        or not set(stages) <= STAGES
    ):
        raise Rejected("explicit unique stages required")
    for name, default, low, high in (
        ("live_seconds", 60, 10, 120),
        ("stage_seconds", 180, 30, 600),
    ):
        value = config.get(name, default)
        if type(value) not in {int, float} or not low <= value <= high:
            raise Rejected("invalid duration budget")
        config[name] = value
    if config["stage_seconds"] < config["live_seconds"] + 20 and "live" in stages:
        raise Rejected("stage budget must allow Live close and cleanup")
    config.setdefault("near_model", "z-ai/glm-5.3-flash")
    config.setdefault("live_backend", "near")
    if config["live_backend"] not in {"near", "runtime"}:
        raise Rejected("invalid Live backend")
    if (
        not isinstance(config["near_model"], str)
        or not 1 <= len(config["near_model"]) <= 128
    ):
        raise Rejected("invalid NEAR model")
    for name in ("image", "audio", "socket"):
        if name in config:
            if not isinstance(config[name], str) or not config[name]:
                raise Rejected("invalid input path")
            config[name] = str((path.parent / config[name]).resolve())
    return config


def read_image(path):
    """Only a bounded explicit PNG is sent; no directory or camera scanning."""
    path = Path(path)
    if not 33 <= path.stat().st_size <= 2_000_000:
        raise Rejected("PNG size outside limit")
    data = path.read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n" or data[12:16] != b"IHDR":
        raise Rejected("PNG image required")
    width, height = (
        int.from_bytes(data[16:20], "big"),
        int.from_bytes(data[20:24], "big"),
    )
    if not 16 <= width <= 1920 or not 16 <= height <= 1080:
        raise Rejected("PNG dimensions outside limit")
    return data


def read_audio(path):
    if Path(path).stat().st_size > 1_500_000:
        raise Rejected("audio exceeds limit")
    with wave.open(str(path), "rb") as source:
        if (
            source.getnchannels() != 1
            or source.getsampwidth() != 2
            or source.getframerate() != 24000
            or source.getcomptype() != "NONE"
            or not 2400 <= source.getnframes() <= 24000 * 20
        ):
            raise Rejected("audio must be 0.1–20 seconds mono PCM16 24kHz WAV")
        data = source.readframes(source.getnframes())
        if len(data) != source.getnframes() * 2:
            raise Rejected("truncated audio")
    return data


def preflight(config):
    """No connections, imports of optional providers, or credential values returned."""
    missing, checks = [], []
    for module in (
        {"nearai_inference_sdk"} if set(config["stages"]) & {"near", "live"} else set()
    ) | ({"websockets"} if "live" in config["stages"] else set()):
        if importlib.util.find_spec(module) is None:
            missing.append("dependency:" + module)
    keys = set()
    if "near" in config["stages"] or (
        "live" in config["stages"] and config["live_backend"] == "near"
    ):
        keys.add("NEARAI_API_KEY")
    if "live" in config["stages"]:
        keys.add("OPENAI_API_KEY")
    for key in sorted(keys):
        try:
            credential(key)
        except Rejected:
            missing.append("credential:" + key)
    for stage, name, reader in (
        ("near", "image", read_image),
        ("live", "audio", read_audio),
    ):
        if stage in config["stages"]:
            try:
                data = reader(config[name])
                checks.append(
                    {
                        "input": name,
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "bytes": len(data),
                    }
                )
            except (KeyError, OSError, ValueError, Rejected, wave.Error):
                missing.append("input:" + name)
    if set(config["stages"]) & {"task", "commons"} or (
        "live" in config["stages"] and config["live_backend"] == "runtime"
    ):
        try:
            mode = Path(config["socket"]).stat().st_mode
            if not stat.S_ISSOCK(mode) or mode & 0o077:
                raise ValueError()
        except (KeyError, OSError, ValueError):
            missing.append("runtime:private_socket")
    if "task" in config["stages"]:
        task = config.get("task", {})
        budget = task.get("budget", {}) if isinstance(task, dict) else {}
        if (
            not isinstance(task, dict)
            or not {"kind", "target", "places", "purpose", "allowed_actions", "budget"}
            <= task.keys()
            or not isinstance(task.get("allowed_actions"), list)
            or not isinstance(budget, dict)
            or type(budget.get("seconds")) not in {int, float}
            or not 1 <= budget["seconds"] <= config["stage_seconds"] - 10
            or type(budget.get("provider_calls")) is not int
            or not 1 <= budget["provider_calls"] <= 8
            or type(budget.get("observations")) is not int
            or not 1 <= budget["observations"] <= 8
        ):
            missing.append("task:bounded_request")
    if "commons" in config["stages"]:
        commons = config.get("commons", {})
        if (
            not isinstance(commons, dict)
            or not {"export_id", "entry_id", "source_digest", "privacy_risk"}
            <= commons.keys()
        ):
            missing.append("commons:reviewed_selection")
        elif (
            commons.get("submit", False)
            and not {"envelope_digest", "body_digest"} <= commons.keys()
        ):
            missing.append("commons:reviewed_envelope")
        else:
            try:
                uuid.UUID(commons["export_id"])
                if not isinstance(commons["entry_id"], str) or not commons["entry_id"]:
                    raise ValueError()
                if type(commons.get("submit", False)) is not bool or commons[
                    "privacy_risk"
                ] not in {"low", "medium"}:
                    raise ValueError()
                for name in ("source_digest", "envelope_digest", "body_digest"):
                    if name == "source_digest" or commons.get("submit", False):
                        value = commons[name]
                        if (
                            not isinstance(value, str)
                            or len(value) != 64
                            or any(c not in "0123456789abcdef" for c in value)
                        ):
                            raise ValueError()
            except (ValueError, TypeError, AttributeError, KeyError):
                missing.append("commons:invalid_reviewed_selection")
    return {
        "ready": not missing,
        "missing": missing,
        "inputs": checks,
        "network_contacted": False,
        "account_access": "not_checked",
        "runtime_grants": "not_checked",
    }


def verified_summary(reply):
    evidence = reply.evidence
    if (
        evidence.get("provider") != "near"
        or evidence.get("signature_kind") != "provider_tee"
        or evidence.get("e2ee") is not True
        or not evidence.get("response_id")
    ):
        raise Rejected("verified NEAR TEE response required")
    return {
        "response_id_sha256": hashlib.sha256(
            evidence["response_id"].encode()
        ).hexdigest(),
        "signature_kind": "provider_tee",
        "e2ee": True,
        "usage": {
            key: value
            for key, value in reply.usage.items()
            if key in {"prompt_tokens", "completion_tokens", "total_tokens"}
            and type(value) is int
            and value >= 0
        },
    }


async def near_stage(config, *, factory=NearReasoner):
    image = read_image(config["image"])
    reasoner = factory(model=config["near_model"], timeout=60)
    try:
        text = await reasoner.complete(
            [{"role": "user", "content": "Reply with exactly ORBO_OK."}], []
        )
        text_evidence = verified_summary(text)
        if text.message.get("content", "").strip() != "ORBO_OK":
            raise Rejected("text smoke response mismatch")
        visual = await reasoner.complete(
            [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": "Briefly describe only what is visible in this image. Do not infer hidden objects.",
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": "data:image/png;base64,"
                                + base64.b64encode(image).decode()
                            },
                        },
                    ],
                }
            ],
            [],
        )
        visual_evidence = verified_summary(visual)
        if (
            not isinstance(visual.message.get("content"), str)
            or not visual.message["content"].strip()
        ):
            raise Rejected("missing vision response")
        return {
            "text": text_evidence,
            "image": visual_evidence,
            "image_sha256": hashlib.sha256(image).hexdigest(),
            "visual_accuracy": "requires_scene_review",
        }
    finally:
        await reasoner.close()


class FileAudio:
    """Realtime-paced file input and discarded output; never claims audibility/AEC."""

    sample_rate = 24000
    output_bytes = 0
    played_frames = 0

    def __init__(self, pcm):
        self.pcm, self.offset = pcm, 0
        self.generated_frames = 0
        self.after_release_frames = 0
        self.released = False
        self.last_activity = time.monotonic()

    def start(self):
        pass

    async def read(self):
        await asyncio.sleep(0.02)
        chunk = self.pcm[self.offset : self.offset + 960]
        self.offset += len(chunk)
        return chunk.ljust(960, b"\0")

    def enqueue(self, chunk):
        if not chunk or len(chunk) % 2 or len(chunk) > 96000:
            raise Rejected("invalid PCM output")
        self.generated_frames += len(chunk) // 2
        if self.released:
            self.after_release_frames += len(chunk) // 2

    def check(self):
        pass

    def resume(self):
        pass

    def interrupt(self):
        pass

    def close(self):
        pass


async def live_stage(config, *, factory=NearReasoner, transport=None, bridge_type=None):
    from .live_voice import LiveBridge, LiveConfig, LiveWebSocket, ReleasedUpdate
    from .storage import Store

    audio = FileAudio(read_audio(config["audio"]))
    reasoner = (
        factory(model=config["near_model"], timeout=40)
        if config.get("live_backend", "near") == "near"
        else None
    )
    store = Store(":memory:")
    owner = "live-e2e"
    episode = store.episode(owner, provenance="recorded")
    for scope, target in (
        ("cloud.voice", "openai"),
        ("speech.play", "local"),
        ("cloud.reasoning", "near"),
    ):
        store.grant(
            owner, scope, target, True, store.clock.utc() + config["stage_seconds"] + 60
        )
    calls, proofs, tasks, delegation_ids = 0, [], [], []

    async def backend(context):
        nonlocal calls
        if calls >= 1:
            raise Rejected("one NEAR delegation per live smoke run")
        calls += 1
        delegation_ids.append(context.delegation_id)
        fragments = [
            item["text"] for item in context.fragments if item["role"] == "user"
        ]
        if not fragments:
            raise Rejected("delegation missing user transcript")
        if config.get("live_backend") == "runtime":
            selected = await runtime_call(
                config,
                "task.delegate",
                {"context": dataclasses.asdict(context)},
            )
            if not selected.get("task_id"):
                raise Rejected("Live task request did not admit a task")
            task = await runtime_call(
                config, "task.get", {"task_id": selected["task_id"]}
            )
            try:
                task_report, selected = await wait_task(config, task)
            except StageFailure as failure:
                tasks.append(failure.evidence)
                raise
            tasks.append(task_report)
            proofs.append(task_report["provider_evidence"])
            return ReleasedUpdate(selected["text"])
        reply = await reasoner.complete(
            [
                {
                    "role": "system",
                    "content": "This is a stationary integration test. Answer the user in one short sentence under 300 UTF-8 bytes. You cannot move a robot or call tools. State that limitation for action requests.",
                },
                {"role": "user", "content": " ".join(fragments)},
            ],
            [],
        )
        proofs.append(verified_summary(reply))
        text = reply.message.get("content")
        if not isinstance(text, str) or not 0 < len(text.encode()) <= 480:
            raise Rejected("unbounded backend release")
        return ReleasedUpdate(text)

    class ObservedBridge(bridge_type or LiveBridge):
        async def handle(self, event):
            await super().handle(event)
            if event.get("type") == "session.commentary.appended":
                accepted = store.db.execute(
                    "SELECT 1 FROM live_deliveries WHERE id=? AND session_id=? AND state='context_accepted'",
                    (event.get("client_event_id"), self.session_id),
                ).fetchone()
                if accepted:
                    audio.released = True

    bridge = ObservedBridge(
        store,
        owner,
        episode,
        backend,
        transport or LiveWebSocket(),
        audio,
        config=LiveConfig(
            max_session_seconds=config["live_seconds"],
            idle_seconds=config["live_seconds"],
            backend_seconds=min(config["live_seconds"], 120),
            max_delegations=1,
        ),
    )
    job = asyncio.create_task(bridge.run(wake="local"))
    try:
        result = await job
        await bridge.wait_backend()
        accepted = store.db.execute(
            "SELECT COUNT(*) FROM live_deliveries WHERE session_id=? AND state='context_accepted'",
            (bridge.session_id,),
        ).fetchone()[0]
        resolved = store.db.execute(
            "SELECT COUNT(*) FROM live_delegations WHERE session_id=? AND state='resolved'",
            (bridge.session_id,),
        ).fetchone()[0]
        if (
            not result["finalized"]
            or not accepted
            or not resolved
            or not audio.after_release_frames
            or len(proofs) != 1
        ):
            raise Rejected(
                "Live requires finalized usage, verified delegation, accepted context and subsequent output audio"
            )
        return {
            "seconds": result["seconds"],
            "finalized": True,
            "verified_delegations": len(proofs),
            "near": proofs,
            "tasks": tasks,
            "context_accepted": accepted,
            "generated_frames": audio.generated_frames,
            "frames_after_context_acceptance": audio.after_release_frames,
            "audibility": "not_tested",
            "acoustic_echo": "not_tested",
            "spoken_semantics": "requires_listener_review",
        }
    except BaseException as error:
        raise StageFailure(
            "live_exchange_failed_or_timed_out",
            {
                "session_id": bridge.session_id,
                "delegation_ids": delegation_ids,
                "tasks": tasks,
                "generated_frames": audio.generated_frames,
                "frames_after_context_acceptance": audio.after_release_frames,
            },
        ) from error
    finally:
        if not job.done():
            job.cancel()
        await asyncio.gather(job, return_exceptions=True)
        # Bridge keeps admitted backend work alive across voice close; join it
        # before disposing the in-memory journal, bounded by its own timeout.
        await bridge.wait_backend()
        if reasoner:
            await reasoner.close()
        store.close()


async def runtime_call(config, method, args):
    from .cli import request

    reply = await request(config["socket"], method, args)
    if not isinstance(reply, dict) or reply.get("ok") is not True:
        raise Rejected("runtime rejected " + method)
    return reply["result"]


async def task_stage(config, run_id, *, call=runtime_call):
    task = await call(
        config, "task.submit", {"key": "live-e2e:" + run_id, "request": config["task"]}
    )
    report, _ = await wait_task(config, task, call=call)
    return report


async def wait_task(config, task, *, call=runtime_call):
    task_id = task["id"]
    try:
        while task["state"] in {"accepted", "working"}:
            await asyncio.sleep(0.5)
            task = await call(config, "task.get", {"task_id": task_id})
        if task["state"] != "completed":
            raise Rejected("task did not complete")
        result = await call(config, "task.result", {"task_id": task_id})
        if not result:
            raise Rejected("task result not released")
        proof = await call(config, "task.evidence", {"task_id": task_id})
        if proof.get("live_service") is not True or not proof.get(
            "verified_response_count"
        ):
            raise Rejected("task lacks live verified NEAR evidence")
        return {
            "task_id": task_id,
            "episode_id": task["episode_id"],
            "state": task["state"],
            "revision": task["revision"],
            "result_released": True,
            "provider_evidence": proof,
            "provenance": task["request"]["provenance"],
            "physical_success": "requires_scene_review",
        }, result
    except BaseException as error:
        # A timeout never turns into another task submission. Request cancellation
        # for the known task; durable runtime state remains authoritative.
        cancellation = "unconfirmed"
        try:
            async with asyncio.timeout(10):
                cancelled = await call(config, "task.cancel", {"task_id": task_id})
                if (
                    isinstance(cancelled, dict)
                    and cancelled.get("state") == "cancelled"
                ):
                    cancellation = "runtime_recorded_cancelled"
        except Exception:
            pass
        raise StageFailure(
            "task_failed_or_timed_out",
            {
                "task_id": task_id,
                "episode_id": task["episode_id"],
                "cancellation": cancellation,
            },
        ) from error


async def commons_stage(config, *, call=runtime_call):
    selection = config["commons"]
    args = {key: selection[key] for key in ("export_id", "entry_id")}
    if selection.get("submit", False):
        await call(
            config,
            "commons.submit_reviewed",
            {
                **args,
                **{key: selection[key] for key in ("envelope_digest", "body_digest")},
            },
        )
    while True:
        status = await call(config, "commons.status", args)
        if status["local_state"] in {"revoked", "refused", "expired", "superseded"}:
            raise Rejected("Commons submission unavailable")
        submissions = status["submission_ids"]
        if submissions:
            if len(submissions) != 1:
                raise Rejected("one exact Commons submission required")
            break
        await asyncio.sleep(1)
    report = await call(
        config,
        "consumer.retrieve",
        {
            "purpose": "embodied_process",
            "source_digests": [selection["source_digest"]],
            "submission_ids": submissions,
            "privacy_risk": selection["privacy_risk"],
            "limit": 1,
        },
    )
    if report.get("complete") is not True:
        raise Rejected("Commons did not deliver reconstructable reviewed evidence")
    return {
        "complete": True,
        "submission_ids": submissions,
        "response_sha256": report["response_sha256"],
        "source_digest": selection["source_digest"],
        "accounting": "not_established",
    }


def authorize(config, flags):
    if set(config["stages"]) & {"near", "live", "task"} and not flags.allow_cloud:
        raise Rejected(
            "--allow-cloud required for billed inference and selected data transfer"
        )
    if "task" in config["stages"]:
        if not flags.allow_task:
            raise Rejected("--allow-task required for runtime capture/task effects")
        if "navigate" in config["task"]["allowed_actions"] and not flags.allow_motion:
            raise Rejected("--allow-motion required for navigation")
    if (
        "live" in config["stages"]
        and config.get("live_backend") == "runtime"
        and not (flags.allow_task and flags.allow_motion)
    ):
        raise Rejected(
            "runtime voice delegation requires --allow-task and --allow-motion; runtime grants remain authoritative"
        )
    if "commons" in config["stages"] and not flags.allow_commons:
        raise Rejected("--allow-commons required for authenticated evidence retrieval")
    if (
        "commons" in config["stages"]
        and config["commons"].get("submit", False)
        and not flags.allow_export
    ):
        raise Rejected("--allow-export required for the exact reviewed envelope")


def save_report(path, report):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Never follow an existing output symlink or replace a previous run report.
    temporary = path.with_name(path.name + ".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(report, stream, indent=2)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


async def run(config, report, path):
    for stage in config["stages"]:
        item = {"stage": stage, "state": "running"}
        report["results"].append(item)
        save_report(path, report)
        started = time.monotonic()
        try:
            async with asyncio.timeout(config["stage_seconds"]):
                if stage == "task":
                    evidence = await task_stage(config, report["run_id"])
                else:
                    evidence = await {
                        "near": near_stage,
                        "live": live_stage,
                        "commons": commons_stage,
                    }[stage](config)
            item.update(state="passed", evidence=evidence)
        except Exception as exc:
            # Errors may contain credentials, URLs, transcripts or provider bodies.
            item.update(
                state="failed",
                error_class=type(exc).__name__,
                outcome="inspect_private_runtime_state_before_retry",
            )
            if isinstance(exc, StageFailure):
                item.update(error_code=exc.code, evidence=exc.evidence)
        finally:
            item["elapsed_seconds"] = round(time.monotonic() - started, 3)
            save_report(path, report)
        if item["state"] != "passed":
            return False
    return True


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["preflight", "run"])
    parser.add_argument("--config", required=True)
    parser.add_argument("--report-directory", default=".live-validation/e2e")
    for flag in ("cloud", "task", "motion", "commons", "export"):
        parser.add_argument("--allow-" + flag, action="store_true")
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        readiness = preflight(config)
        if args.command == "preflight":
            print(json.dumps(readiness, indent=2))
            return 0 if readiness["ready"] else 2
        if not readiness["ready"]:
            print(json.dumps(readiness, indent=2))
            return 2
        authorize(config, args)
        run_id = str(uuid.uuid4())
        report = {
            "schema": "orbo.live-e2e.v1",
            "run_id": run_id,
            "preflight": readiness,
            "results": [],
            "limits": [
                "No acoustic audibility or physical scene truth inferred.",
                "No reward or applied-credit inference.",
                "Provider request counts and duration bounded; monetary charge depends on provider pricing.",
            ],
        }
        path = Path(args.report_directory) / (run_id + ".json")
        passed = asyncio.run(run(config, report, path))
        print(json.dumps({"passed": passed, "report": str(path.resolve())}))
        return 0 if passed else 1
    except (Exception, KeyboardInterrupt) as exc:
        failure = {"passed": False, "error_class": type(exc).__name__}
        if isinstance(exc, Rejected):
            failure["setup_error"] = str(exc)
        print(json.dumps(failure))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

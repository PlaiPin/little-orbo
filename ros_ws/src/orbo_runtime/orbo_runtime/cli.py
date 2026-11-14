"""Owner-only local IPC and reproducible development scenario."""

import argparse
import asyncio
import contextlib
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import signal
import socket
import sys
import tomllib

from .broker import Broker, FakeRobot
from .contracts import ManualClock, Rejected, canonical, finite, identity
from .coordinator import Coordinator
from .storage import Store
from .traces import Exports, analyze


class UnconfiguredReasoner:
    async def complete(self, messages, tools):
        raise Rejected("provider configuration required")


async def demo(database):
    clock = ManualClock()
    store = Store(database, clock)
    try:
        robot = FakeRobot()
        broker = Broker(store, robot)
        coordinator = Coordinator(store, broker, UnconfiguredReasoner())
        store.grant(
            "development-owner",
            "robot.command",
            robot.robot_id,
            True,
            clock.utc() + 3600,
        )
        episode = store.episode("development-owner", provenance="synthetic")
        saved = coordinator.reminders.create(
            "development-owner", episode, identity(), "Stretch", clock.utc() + 10
        )
        clock.advance(10)
        attention = await coordinator.tick()
        text, generation = coordinator.reminders.acknowledge(
            "development-owner", saved["reminder_id"], attention["ack_token"]
        )
        # Explicitly simulated playback; the episode's provenance remains synthetic.
        store.event(
            episode,
            "simulation.playback_fixture",
            {"text": text, "physical_audio": False},
        )
        coordinator.reminders.playback_result(
            "development-owner", saved["reminder_id"], generation, True, 24000
        )
        store.end_episode(episode, "development-owner", "completed")
        export = Exports(store).prepare(
            "development-owner", episode, "attention_timing", "local-development"
        )
        return {
            "provenance": "synthetic",
            "physical_audio": False,
            "hardware_acceptance": False,
            "episode_id": episode,
            "reminder": coordinator.reminders.get(
                "development-owner", saved["reminder_id"]
            ),
            "export_id": export["export_id"],
            "sha256": export["sha256"],
            "analysis": export["package"]["evaluation"],
        }
    finally:
        store.close()


class LocalService:
    def __init__(self, config):
        reasoning_enabled = config.get("near", {}).get("enabled", False)
        live_enabled = config.get("live", {}).get("enabled", False)
        if live_enabled and (
            not reasoning_enabled
            or not config.get("embodied", {}).get("enabled", False)
        ):
            raise Rejected("Live requires embodied and NEAR enabled")
        self.config = config
        self.owner = config["runtime"]["owner"]
        self.store = Store(config["runtime"]["database"])
        self.store.recover()
        profile = config["runtime"]["profile"]
        if profile == "simulated":
            adapter = FakeRobot()
        elif profile == "gazebo":
            from .gazebo_adapter import GazeboAdapter

            adapter = GazeboAdapter(
                config["robot"]["goals"], map_version=config["embodied"]["map_version"]
            )
        elif profile == "orbo":
            from .ros_adapter import OrboAdapter

            adapter = OrboAdapter(config["robot"])
        elif profile == "booster_k1":
            from .k1_adapter import K1Adapter

            adapter = K1Adapter(config["robot"])
        else:
            raise Rejected("unsupported runtime profile")
        self.broker = Broker(self.store, adapter)
        self.reasoner = UnconfiguredReasoner()
        if reasoning_enabled:
            from .providers import NearReasoner

            self.reasoner = NearReasoner(
                model=config["near"]["model"],
                timeout=finite(config["near"].get("timeout_seconds", 40), 1, 120),
                max_completion_tokens=config["near"].get("max_completion_tokens", 1024),
            )
        self.coordinator = Coordinator(self.store, self.broker, self.reasoner)
        self.exports = Exports(self.store)
        from .economics import Ledger

        self.ledger = Ledger(self.store)
        self.commons = None
        if config.get("commons", {}).get("socket"):
            from .commons import Commons, ContributorIPC

            self.commons = Commons(
                self.store, ContributorIPC(config["commons"]["socket"])
            )
        self.jobs = set()
        self.embodied = None
        if config.get("embodied", {}).get("enabled", False):
            from .embodied_service import EmbodiedService

            self.embodied = EmbodiedService(self)

    async def interrupt(self):
        if self.embodied:
            for task_id in tuple(self.embodied.assistant.jobs):
                await self.embodied.assistant.pause(self.owner, task_id)
        if self.embodied and self.embodied.live:
            self.embodied.live.interrupt_speech()
        await self.coordinator.interrupt()

    async def call(self, request):
        method = request.get("method")
        args = request.get("args", {})
        if not isinstance(args, dict):
            raise Rejected("arguments must be an object")
        if method.startswith(("task.", "place.", "memory.", "live.", "evaluation.")):
            if self.embodied is None:
                raise Rejected("embodied profile not enabled")
            return await self.embodied.call(method, args)
        if method == "episode.start":
            return {
                "episode_id": self.store.episode(
                    self.owner,
                    profile=args.get("profile", "attention.v1"),
                    provenance="synthetic"
                    if self.broker.adapter.simulated
                    else "physical",
                )
            }
        if method == "episode.end":
            self.store.end_episode(args["episode_id"], self.owner, args["outcome"])
            return {"ended": True}
        if method == "reminder.create":
            return self.coordinator.reminders.create(
                self.owner,
                args["episode_id"],
                args["key"],
                args["text"],
                args["due_utc"],
            )
        if method in {"reminder.defer", "reminder.cancel"}:
            row = self.coordinator.reminders.get(self.owner, args["reminder_id"])
            result = self.coordinator.reminders.change(
                self.owner,
                args["reminder_id"],
                args["key"],
                due=args.get("due_utc") if method == "reminder.defer" else None,
            )
            if row["command_id"]:
                await self.broker.cancel(self.owner, row["command_id"])
            return result
        if method == "reminder.list":
            return [
                dict(r)
                for r in self.store.db.execute(
                    "SELECT * FROM reminders WHERE owner=? ORDER BY due LIMIT 100",
                    (self.owner,),
                )
            ]
        if method == "permission.set":
            self.store.grant(
                self.owner,
                args["scope"],
                args["destination"],
                args["enabled"],
                args["expires"],
            )
            if not args["enabled"]:
                scope = args["scope"]
                if self.embodied and scope in {"cloud.voice", "speech.play"}:
                    if self.embodied.live:
                        await self.embodied.live.close()
                    # A speech permission change does not revoke the durable task.
                elif scope in {
                    "audio.capture",
                    "cloud.reasoning",
                    "speech.play",
                    "robot.command",
                    "task.execute",
                    "observation.capture",
                    "observation.infer",
                }:
                    await self.interrupt()
            return {"updated": True}
        if method == "preference.set":
            return self.coordinator.reminders.set_preference(
                self.owner,
                args["episode_id"],
                "attention",
                args["value"],
                args["expected_version"],
                args["key"],
            )
        if method == "conversation.text":
            if self.embodied:
                return dict(await self.embodied.call("task.text", args), played=False)
            return {
                "text": await self.coordinator.converse(
                    self.owner, args["episode_id"], args["text"]
                ),
                "played": False,
            }
        if method == "stop":
            await self.interrupt()
            return {"stop_requested": True, "physical_cessation": "unverified"}
        if method == "robot.inspect":
            if not hasattr(self.broker.adapter, "inspect"):
                raise Rejected("adapter does not expose bridge inventory")
            return await self.broker.adapter.inspect()
        if method == "robot.action":
            self.store.permission(
                self.owner, "robot.command", self.broker.adapter.robot_id
            )
            if hasattr(self.broker.adapter, "inspect"):
                await self.broker.adapter.inspect()
            epoch = self.broker.acquire(self.owner)
            return await self.broker.dispatch(
                self.owner,
                args["episode_id"],
                args["key"],
                "humanoid.action",
                {"action_id": args["action_id"], "hold_seconds": args["hold_seconds"]},
                epoch,
                duration=args.get("duration", 10),
            )
        if method == "robot.reconcile":
            command = self.broker.command(args["command_id"])
            self.store.require_episode(command["episode_id"], self.owner)
            return await self.broker.reconcile(args["command_id"])
        if method == "robot.recover":
            from .k1_adapter import K1Adapter

            if not isinstance(self.broker.adapter, K1Adapter):
                raise Rejected("manual recovery is available only for the K1 bridge")
            command = self.broker.command(args["command_id"])
            self.store.require_episode(command["episode_id"], self.owner)
            self.store.permission(
                self.owner, "robot.command", self.broker.adapter.robot_id
            )
            await self.broker.adapter.rpc(
                "recover",
                {
                    "command_id": args["command_id"],
                    "observed_stopped": args["observed_stopped"],
                    "evidence_record": args["evidence_record"],
                },
            )
            return await self.broker.reconcile(args["command_id"])
        if method == "robot.gesture":
            if set(args) - {"episode_id", "key", "duration"}:
                raise Rejected("gesture accepts a duration, not arbitrary servo pulses")
            duration = args.get("duration", 2)
            finite(duration, 0.2, 5)
            if not isinstance(args.get("key"), str) or not 1 <= len(args["key"]) <= 128:
                raise Rejected("bounded gesture request key required")
            self.store.require_episode(args["episode_id"], self.owner)
            epoch = self.broker.gesture_epoch(self.owner)
            return await self.broker.dispatch(
                self.owner,
                args["episode_id"],
                args["key"],
                "attention.gesture",
                {"gesture": "attention"},
                epoch,
                duration=duration,
            )
        if method == "robot.teleop":
            spec = self.broker.adapter.capabilities.get("teleop.session")
            if not spec or not spec.qualified:
                raise Rejected("teleop is not configured and qualified")
            if set(args) - {"episode_id", "key", "duration"}:
                raise Rejected(
                    "teleop accepts a session budget, not velocity arguments"
                )
            duration = args.get("duration", 20)
            finite(duration, 1, 30)
            if not isinstance(args.get("key"), str) or not 1 <= len(args["key"]) <= 128:
                raise Rejected("bounded teleop request key required")
            self.store.require_episode(args["episode_id"], self.owner)
            epoch = self.broker.acquire(self.owner)
            return await self.broker.dispatch(
                self.owner,
                args["episode_id"],
                args["key"],
                "teleop.session",
                {},
                epoch,
                duration=duration,
            )
        if method == "robot.navigate":
            epoch = self.broker.acquire(self.owner)
            return await self.broker.dispatch(
                self.owner,
                args["episode_id"],
                args["key"],
                "navigation.named_goal",
                {"waypoint": args["waypoint"]},
                epoch,
                duration=args.get("duration", 25),
            )
        if method == "consumer.retrieve":
            from .consumer import EvidenceConsumer

            origin = self.config["commons"]["origin"]
            self.store.permission(self.owner, "commons.retrieve", origin)
            consumer = EvidenceConsumer(origin)
            try:
                report = await consumer.retrieve(
                    args["purpose"],
                    args["source_digests"],
                    privacy_risk=args["privacy_risk"],
                    submission_ids=args["submission_ids"],
                    limit=args.get("limit", 1),
                )
                self.store.permission(self.owner, "commons.retrieve", origin)
                self.store.event(
                    None,
                    "commons.consumer_retrieved",
                    {
                        "server_export_id": report["server_export_id"],
                        "response_sha256": report["response_sha256"],
                        "complete": report["complete"],
                    },
                )
                return report
            finally:
                await consumer.close()
        if method == "accounting.near.sync":
            self.store.permission(self.owner, "accounting.read", "near")
            from .economics import NearUsage

            reporter = NearUsage(
                self.config["reporting"]["near_organization"], self.ledger
            )
            try:
                return await reporter.sync(args["start"], args["end"])
            finally:
                await reporter.close()
        if method == "accounting.commons.sync":
            self.store.permission(self.owner, "accounting.read", "commons")
            from .economics import CommonsAccounting

            reporter = CommonsAccounting(
                self.config["commons"]["origin"],
                self.config["commons"]["account"],
                self.ledger,
            )
            try:
                return await reporter.sync(args["submission_ids"])
            finally:
                await reporter.close()
        if method == "accounting.summary":
            return self.ledger.summary()
        if method == "accounting.record":
            inserted = self.ledger.observe(
                args["provider"],
                args["account"],
                args["external_id"],
                args["kind"],
                args["body"],
                provenance="owner_assertion",
            )
            return {"inserted": inserted, "provenance": "owner_assertion"}
        if method.startswith("commons."):
            if self.commons is None:
                raise Rejected("contributor daemon socket not configured")
            if method == "commons.review":
                return await self.commons.review(
                    self.owner, args["export_id"], args["entry_id"]
                )
            if method == "commons.submit_reviewed":
                return await self.commons.submit_reviewed(
                    self.owner,
                    args["export_id"],
                    args["entry_id"],
                    args["envelope_digest"],
                    args["body_digest"],
                )
            if method == "commons.recovery_queue":
                return self.commons.recovery_queue(self.owner)
            if method == "commons.recover":
                return await self.commons.recover(
                    self.owner, args["export_id"], args["entry_id"]
                )
            if method == "commons.status":
                return await self.commons.status(
                    self.owner, args["export_id"], args["entry_id"]
                )
            if method == "commons.revoke":
                return await self.commons.revoke(
                    self.owner, args["export_id"], args["entry_id"]
                )
        if method == "journal":
            after = args.get("after", 0)
            if type(after) is not int or after < 0:
                raise Rejected("invalid journal cursor")
            return [
                dict(r)
                for r in self.store.db.execute(
                    "SELECT * FROM events WHERE seq>? ORDER BY seq LIMIT 100", (after,)
                )
            ]
        if method == "export.prepare":
            return self.exports.prepare(
                self.owner, args["episode_id"], args["purpose"], args["recipient"]
            )
        if method == "export.derive":
            return self.exports.derive(
                self.owner,
                args["export_id"],
                redactions=args.get("redactions", []),
                omit_event_ids=args.get("omit_event_ids", []),
            )
        if method == "export.preview":
            return self.exports.preview(self.owner, args["export_id"])
        if method == "export.approve":
            self.exports.approve(
                self.owner, args["export_id"], args["sha256"], expires=args["expires"]
            )
            return {"approved": True}
        if method == "export.trajectory":
            from .traces import trajectory
            from .contracts import digest

            package = json.loads(
                self.exports.approved_bytes(self.owner, args["export_id"])
            )
            content = trajectory(package).decode()
            return {
                "export_id": args["export_id"],
                "source_export_digest": digest(package),
                "trajectory_utf8": content,
                "uploaded": False,
            }
        if method == "export.revoke":
            self.exports.revoke(self.owner, args["export_id"])
            return {"revoked": True}
        raise Rejected("unknown local operation")

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.jobs.add(task)
        try:
            if len(self.jobs) > 8:
                raise Rejected("local request capacity reached")
            async with asyncio.timeout(180):
                line = await reader.readline()
                if not line or len(line) > 65536:
                    raise Rejected("request size limit")
                request = json.loads(line)
                if not isinstance(request, dict):
                    raise Rejected("request must be an object")
                result = {"ok": True, "result": await self.call(request)}
        except (Exception, asyncio.CancelledError) as exc:
            result = {
                "ok": False,
                "error": str(exc) if isinstance(exc, Rejected) else type(exc).__name__,
            }
        try:
            encoded = (canonical(result) + "\n").encode()
            if len(encoded) > 16_777_216:
                encoded = b'{"ok":false,"error":"response size limit; narrow the requested scope"}\n'
            writer.write(encoded)
            async with asyncio.timeout(5):
                await writer.drain()
        except (ConnectionError, BrokenPipeError, TimeoutError):
            pass
        finally:
            writer.close()
            await writer.wait_closed()
            self.jobs.discard(task)

    async def scheduler(self):
        while True:
            try:
                await self.coordinator.tick()
            except Exception as exc:
                self.store.event(
                    None, "scheduler.failure", {"reason": type(exc).__name__}
                )
            await asyncio.sleep(0.2)

    async def close(self):
        if self.embodied:
            await self.embodied.close()
        try:
            await self.interrupt()
        except Exception as error:
            self.store.event(
                None, "shutdown.stop_unconfirmed", {"error_class": type(error).__name__}
            )
        resources = [self.reasoner, self.broker.adapter]
        try:
            for resource in resources:
                if hasattr(resource, "close"):
                    try:
                        await resource.close()
                    except Exception as error:
                        self.store.event(
                            None,
                            "shutdown.close_failed",
                            {"error_class": type(error).__name__},
                        )
        finally:
            self.store.close()


async def serve(path):
    config = tomllib.loads(Path(path).read_text())
    socket_path = Path(config["runtime"]["socket"])
    socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if socket_path.exists():
        # Refuse to unlink another running instance's endpoint.
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(socket_path))
        except ConnectionRefusedError:
            socket_path.unlink()
        else:
            raise Rejected("runtime socket already active")
        finally:
            probe.close()
    service = LocalService(config)
    if config["runtime"]["profile"] == "gazebo":
        try:
            await service.broker.adapter.ready()
        except BaseException:
            await service.close()
            raise
    server = await asyncio.start_unix_server(
        service.handle, str(socket_path), limit=65537
    )
    socket_path.chmod(0o600)
    remote_server, remote_path = None, None
    if service.embodied and service.embodied.remote:
        remote_path = Path(config["embodied"]["remote"]["socket"])
        if remote_path == socket_path or remote_path.exists():
            raise Rejected("separate unused remote socket path required")
        remote_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        remote_server = await asyncio.start_unix_server(
            service.embodied.remote.handle, str(remote_path), limit=32769
        )
        remote_path.chmod(0o600)
    stop = asyncio.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    scheduler = asyncio.create_task(service.scheduler())
    try:
        async with server:
            await stop.wait()
    finally:
        scheduler.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await scheduler
        for task in tuple(service.jobs):
            task.cancel()
        await asyncio.gather(*service.jobs, return_exceptions=True)
        if remote_server:
            remote_server.close()
            await remote_server.wait_closed()
            remote_path.unlink(missing_ok=True)
        await service.close()
        socket_path.unlink(missing_ok=True)


async def request(socket_path, method, args):
    reader, writer = await asyncio.open_unix_connection(socket_path, limit=16_777_217)
    try:
        writer.write((canonical({"method": method, "args": args}) + "\n").encode())
        await writer.drain()
        async with asyncio.timeout(185):
            return json.loads(await reader.readline())
    finally:
        writer.close()
        await writer.wait_closed()


def diagnostics():
    packages = {}
    for name in ["orbo-runtime", "nearai-inference-sdk", "sounddevice", "httpx"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    return {
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "packages": packages,
        "ros_distro": os.getenv("ROS_DISTRO"),
        "target": "Raspberry Pi 5 / Ubuntu 24.04 ARM64 / ROS 2 Jazzy",
        "target_acceptance": "not_established",
    }


def main(args=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(prog="orbo")
    sub = parser.add_subparsers(dest="command", required=True)
    demo_parser = sub.add_parser(
        "demo", help="synthetic development scenario, no provider or hardware calls"
    )
    demo_parser.add_argument("--database", default="artifacts/private/demo.db")
    serve_parser = sub.add_parser("serve")
    serve_parser.add_argument("--config", required=True)
    req = sub.add_parser("request")
    req.add_argument("method")
    req.add_argument("--args", default="{}")
    req.add_argument("--socket", default="/run/orbo/runtime.sock")
    sub.add_parser("diagnostics")
    sub.add_parser("credentials", help="initialize/check shared credentials (use --help)")
    sub.add_parser("live-e2e", help="opt-in live integration tests (use --help)")
    sub.add_parser("intents", help="explicit NEAR Intents quote/approval/execution (use --help)")
    analysis = sub.add_parser("analyze")
    analysis.add_argument("package")
    arguments = list(sys.argv[1:] if args is None else args)
    # Delegate intact argv so the integration CLIs own their opt-in flags and
    # expose identical behavior through installed, ROS and module entry points.
    if arguments and arguments[0] == "credentials":
        from .credentials import main as credentials_main

        return credentials_main(arguments[1:])
    if arguments and arguments[0] == "live-e2e":
        from .live_e2e import main as live_e2e_main

        return live_e2e_main(arguments[1:])
    if arguments and arguments[0] == "intents":
        from .intents import main as intents_main

        return intents_main(arguments[1:])
    args = parser.parse_args(arguments)
    try:
        if args.command == "serve":
            asyncio.run(serve(args.config))
            return
        if args.command == "demo":
            result = asyncio.run(demo(args.database))
        elif args.command == "request":
            result = asyncio.run(
                request(args.socket, args.method, json.loads(args.args))
            )
        elif args.command == "analyze":
            result = analyze(json.loads(Path(args.package).read_text()))
        else:
            result = diagnostics()
        print(json.dumps(result, indent=2))
    except (Exception, KeyboardInterrupt) as exc:
        parser.exit(
            1, str(exc) if isinstance(exc, Rejected) else type(exc).__name__ + "\n"
        )


if __name__ == "__main__":
    raise SystemExit(main())

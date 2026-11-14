#!/usr/bin/env python3
"""On-robot BoosterOS bridge. Python 3.10+, vendor motion/balance remain in charge."""

import argparse
import asyncio
from collections import deque
import importlib.metadata
import json
import math
import os
from pathlib import Path
import signal
import sqlite3
import stat
import time
import uuid


SDK_VERSION = "1.2.5"
NATIVE_VERSION = "1.6.3"


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def bounded(value, low, high):
    if (
        type(value) not in (int, float)
        or not math.isfinite(value)
        or not low <= value <= high
    ):
        raise ValueError("number outside bounds")
    return value


class Engine:
    def __init__(self, robot, config, database):
        self.robot, self.config = robot, config
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, request TEXT, epoch INTEGER, state TEXT, detail TEXT)"
        )
        self.db.execute(
            "UPDATE commands SET state='unknown' WHERE state NOT IN ('completed','cancelled','rejected')"
        )
        self.db.commit()
        self.active = None
        self.health = None
        self.health_at = 0.0
        self.ready = False
        self.info = None
        self.catalog = {}
        self.joint_names = set()
        self.closing = False
        self.started = None
        self.samples = deque(maxlen=60)
        self.last_sample = 0.0
        self.cancel_job = None
        self.stop_generation = 0

    async def initialize(self):
        info = await asyncio.to_thread(lambda: self.robot.robot_info)
        if info.manufacturer != "Booster Robotics" or info.model not in {
            "K1",
            "Booster K1",
        }:
            raise ValueError("connected robot is not Booster K1")
        self.info = {
            "model": info.model,
            "firmware_version": info.firmware_version,
            "sdk_version": SDK_VERSION,
            "native_sdk_version": NATIVE_VERSION,
        }
        # Identity/firmware must match the commissioned physical controller.
        self.ready = bool(
            self.config.get("qualified") is True
            and self.config.get("serial_number") == info.serial_number
            and info.serial_number
            and self.config.get("firmware_version") == info.firmware_version
            and info.firmware_version
            and self.config.get("evidence_record")
        )
        joints = await asyncio.to_thread(self.robot.list_joints)
        self.joint_names = {joint.name for joint in joints}
        if not 1 <= len(self.joint_names) <= 64 or len(self.joint_names) != len(joints):
            raise ValueError("invalid vendor joint inventory")
        catalog = await asyncio.to_thread(self.robot.list_actions)
        self.catalog = {
            a.id: a
            for a in catalog
            if a.interruptible is True and a.type == "upper_body"
        }
        allowed = self.config.get("allowed_actions", [])
        if (
            not isinstance(allowed, list)
            or len(allowed) > 16
            or any(a not in self.catalog for a in allowed)
        ):
            raise ValueError(
                "only vendor-listed interruptible upper-body actions may be commissioned"
            )

    def snapshot(self):
        joints = self.robot.get_joint_states()
        fall = self.robot.get_fall_down_state()
        now = time.time()
        if not (
            0 <= now - joints.timestamp <= 0.5 and 0 <= now - fall.timestamp <= 0.5
        ):
            raise ValueError("stale controller feedback")
        mode = self.robot.get_mode()
        if fall.state != "normal" or mode != "walk":
            raise ValueError("controller not in upright vendor walking/balance mode")
        values = []
        for joint in joints:
            if joint.position is None or joint.velocity is None:
                raise ValueError("incomplete joint feedback")
            values.append(
                {
                    "name": joint.name,
                    "position": bounded(joint.position, -1e6, 1e6),
                    "velocity": bounded(joint.velocity, -1e6, 1e6),
                }
            )
        if (
            len(values) != len(self.joint_names)
            or {value["name"] for value in values} != self.joint_names
        ):
            raise ValueError("invalid joint feedback count")
        return {
            "source": "vendor_joint_feedback",
            "timestamp": joints.timestamp,
            "fall_timestamp": fall.timestamp,
            "mode": mode,
            "fall_state": fall.state,
            "joints": values,
        }

    async def observe(self):
        while not self.closing:
            try:
                snapshot = await asyncio.to_thread(self.snapshot)
                self.health, self.health_at = snapshot, time.monotonic()
            except Exception:
                self.health = None
            await asyncio.sleep(0.05)

    def require_health(self):
        if (
            self.health is None
            or time.monotonic() - self.health_at > 0.5
            or not 0 <= time.time() - self.health["timestamp"] <= 0.5
            or not 0 <= time.time() - self.health["fall_timestamp"] <= 0.5
        ):
            raise ValueError("fresh upright controller feedback required")

    def row(self, command):
        row = self.db.execute(
            "SELECT * FROM commands WHERE id=?", (command,)
        ).fetchone()
        if not row:
            raise ValueError("unknown command")
        return {
            "command_id": command,
            "state": row["state"],
            "detail": json.loads(row["detail"]),
        }

    def save(self, state, detail):
        self.db.execute(
            "UPDATE commands SET state=?,detail=? WHERE id=?",
            (state, encoded(detail), self.active["command_id"]),
        )
        self.db.commit()

    async def start(self, request):
        if set(request) != {
            "command_id",
            "action_id",
            "hold_seconds",
            "epoch",
            "duration",
            "issued_utc",
            "ttl_ms",
        }:
            raise ValueError("invalid action fields")
        command = str(uuid.UUID(request["command_id"]))
        body = encoded(request)
        old = self.db.execute(
            "SELECT request FROM commands WHERE id=?", (command,)
        ).fetchone()
        if old:
            if old[0] != body:
                raise ValueError("command identity reused with changed content")
            return self.row(command)
        if not self.ready or request["action_id"] not in self.config["allowed_actions"]:
            raise ValueError("action not commissioned")
        if (
            self.active
            or self.db.execute(
                "SELECT 1 FROM commands WHERE state='unknown'"
            ).fetchone()
        ):
            raise ValueError("busy or unreconciled controller outcome")
        self.require_health()
        epoch = request["epoch"]
        maximum = self.db.execute(
            "SELECT COALESCE(MAX(epoch),0) FROM commands"
        ).fetchone()[0]
        if type(epoch) is not int or not maximum < epoch < 2**63:
            raise ValueError("fresh owner epoch required")
        hold = bounded(request["hold_seconds"], 0.1, 20)
        duration = bounded(request["duration"], hold + 1, 30)
        self.remaining(request)
        generation = self.stop_generation
        if await asyncio.to_thread(self.robot.get_active_tasks):
            raise ValueError("vendor controller already owns a task")
        # A stop can run while the SDK query above is outstanding.
        self.require_health()
        if (
            self.active
            or generation != self.stop_generation
            or epoch
            <= self.db.execute(
                "SELECT COALESCE(MAX(epoch),0) FROM commands"
            ).fetchone()[0]
        ):
            raise ValueError("authority unavailable")
        self.db.execute(
            "INSERT INTO commands VALUES(?,?,?,'pending','{}')", (command, body, epoch)
        )
        self.db.commit()  # Intent precedes any SDK motion call.
        now = time.monotonic()
        try:
            remaining = self.remaining(request)
        except ValueError:
            # Admission or persistence may have outlasted the lease. No SDK
            # action was dispatched, so retain a terminal, idempotent rejection.
            self.db.execute(
                "UPDATE commands SET state='rejected',detail=? WHERE id=?",
                (
                    encoded(
                        {
                            "reason": "invalid_lease_before_sdk_call",
                            "evidence": "none",
                            "physical_observation": False,
                        }
                    ),
                    command,
                ),
            )
            self.db.commit()
            return self.row(command)
        self.active = {
            **request,
            "expires": now + remaining,
            "deadline": now + duration,
            "hold_until": None,
            "stop_reason": None,
            "handle": None,
        }
        self.samples.clear()
        self.started = asyncio.create_task(self.send_action(self.active))
        return self.row(command)

    @staticmethod
    def remaining(request):
        ttl = bounded(request["ttl_ms"], 1, 500) / 1000
        issued = bounded(request["issued_utc"], 0, 1e12)
        age = time.time() - issued
        if not 0 <= age < ttl:
            raise ValueError("expired or future owner lease; synchronize hosts")
        return ttl - age

    async def send_action(self, active):
        if active["stop_reason"] or time.monotonic() >= active["expires"]:
            self.save(
                "rejected",
                {
                    "reason": "cancelled_before_sdk_call",
                    "evidence": "none",
                    "physical_observation": False,
                },
            )
            self.active = None
            return
        try:
            handle = await asyncio.to_thread(self.robot.do_action, active["action_id"])
            active["handle"] = handle
            active["hold_until"] = time.monotonic() + active["hold_seconds"]
            self.save(
                "executing",
                {
                    **self.info,
                    "vendor_trace_id": handle.trace_id,
                    "vendor_task_id": handle.task_id,
                },
            )
        except Exception as error:
            active["stop_reason"] = "start_uncertain"
            self.save(
                "unknown",
                {
                    **self.info,
                    "error_class": type(error).__name__,
                    "physical_observation": False,
                },
            )
            # No resend. A lost start result may still have started the controller.

    def stop(self, reason):
        self.stop_generation += 1
        if self.active:
            self.active["stop_reason"] = reason
            self.active["expires"] = 0

    def renew(self, request):
        active = self.active
        if (
            not active
            or active["stop_reason"]
            or request.get("command_id") != active["command_id"]
            or request.get("epoch") != active["epoch"]
        ):
            raise ValueError("no renewable action authority")
        remaining = self.remaining(request)
        if (
            request["issued_utc"] <= active["issued_utc"]
            or time.monotonic() >= active["expires"]
        ):
            self.stop("expired_lease")
            raise ValueError("lease cannot resurrect or replay authority")
        active["issued_utc"] = request["issued_utc"]
        active["expires"] = min(time.monotonic() + remaining, active["deadline"])

    async def monitor(self):
        while not self.closing:
            active = self.active
            if active:
                now = time.monotonic()
                if now >= active["expires"] or now >= active["deadline"]:
                    active["stop_reason"] = active["stop_reason"] or "expired_lease"
                try:
                    self.require_health()
                except ValueError:
                    active["stop_reason"] = active["stop_reason"] or "stale_feedback"
                if active["hold_until"] and now >= active["hold_until"]:
                    active["stop_reason"] = active["stop_reason"] or "hold_complete"
                if self.health and now - self.last_sample >= 0.25:
                    self.samples.append(self.health)
                    self.last_sample = now
                handle = active["handle"]
                if handle is None and self.started.done() and active["stop_reason"]:
                    # A start RPC may throw after remote admission. Only stop a
                    # matching action; never start a replacement or change mode.
                    for observed in self.robot.get_active_tasks():
                        if observed.task_id == active["action_id"]:
                            active["handle"] = handle = observed
                            break
                if handle:
                    status = handle.status
                    if status in {"SUCCEEDED", "CANCELLED", "FAILED"}:
                        state = (
                            "unknown"
                            if status == "FAILED"
                            else (
                                "completed"
                                if status == "SUCCEEDED"
                                or active["stop_reason"] == "hold_complete"
                                else "cancelled"
                            )
                        )
                        self.save(
                            state,
                            {
                                **self.info,
                                "vendor_trace_id": handle.trace_id,
                                "vendor_task_id": handle.task_id,
                                "vendor_status": status,
                                "stop_reason": active["stop_reason"],
                                "feedback": list(self.samples),
                                "physical_observation": False,
                                "evidence": "controller_ack"
                                if state != "unknown"
                                else "none",
                            },
                        )
                        if status == "FAILED":
                            active["handle"] = None
                            active["stop_reason"] = "controller_failed"
                        else:
                            self.active = None
                    elif active["stop_reason"] and (
                        self.cancel_job is None or self.cancel_job.done()
                    ):
                        # SDK cancel can refuse before it observes the action start.
                        # Retry the stop (never the action) until terminal evidence.
                        if self.cancel_job:
                            try:
                                self.cancel_job.result()
                            except Exception:
                                pass
                        self.cancel_job = asyncio.create_task(
                            asyncio.to_thread(handle.cancel)
                        )
            await asyncio.sleep(0.05)

    async def recover(self, request):
        command = str(uuid.UUID(request["command_id"]))
        evidence = request.get("evidence_record", "")
        if (
            request.get("observed_stopped") is not True
            or not isinstance(evidence, str)
            or not 1 <= len(evidence) <= 512
        ):
            raise ValueError("owner stop observation and evidence record required")
        self.require_health()
        if await asyncio.to_thread(self.robot.get_active_tasks):
            raise ValueError("controller still owns a task")
        if self.active:
            if (
                self.active["command_id"] != command
                or not self.started.done()
                or self.active["handle"] is not None
            ):
                raise ValueError("controller command still unresolved")
            self.active = None
        if self.row(command)["state"] != "unknown":
            raise ValueError("only unknown outcomes require owner recovery")
        self.db.execute(
            "UPDATE commands SET state='cancelled',detail=? WHERE id=?",
            (
                encoded(
                    {
                        "evidence": "owner_assertion",
                        "evidence_record": evidence,
                        "physical_observation": False,
                    }
                ),
                command,
            ),
        )
        self.db.commit()
        return self.row(command)

    async def call(self, method, request):
        if method == "inspect":
            return {
                **(self.info or {}),
                "qualified": self.ready,
                "fresh_feedback": self.health is not None,
                "allowed_actions": self.config.get("allowed_actions", []),
                "epoch_seen": self.db.execute(
                    "SELECT COALESCE(MAX(epoch),0) FROM commands"
                ).fetchone()[0],
            }
        if method == "start":
            return await self.start(request)
        if method == "renew":
            if (
                self.active
                and self.active["command_id"] == request.get("command_id")
                and self.active["stop_reason"] == "hold_complete"
            ):
                return {"renewed": False, "stopping": True}
            self.renew(request)
            return {"renewed": True}
        if method == "status":
            return self.row(request["command_id"])
        if method == "stop":
            self.stop("owner_stop")
            return {"stop_requested": True, "physical_cessation": "unverified"}
        if method == "recover":
            return await self.recover(request)
        raise ValueError("unknown bridge operation")


async def serve(args):
    config = json.loads(args.config.read_text())
    for name, expected in [
        ("boosteros", SDK_VERSION),
        ("booster-robotics-sdk-python", NATIVE_VERSION),
    ]:
        if importlib.metadata.version(name) != expected:
            raise RuntimeError("unqualified SDK version: " + name)
    from boosteros.robots.booster import BoosterRobot

    os.umask(0o077)
    root = args.socket.parent
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    permissions = root.lstat()
    if (
        not stat.S_ISDIR(permissions.st_mode)
        or permissions.st_uid != os.getuid()
        or permissions.st_mode & 0o077
    ):
        raise RuntimeError("private owner-controlled bridge directory required")
    if args.socket.exists():
        raise RuntimeError(
            "socket already exists; verify the previous worker is stopped"
        )
    database = Path(args.database)
    db_parent = database.parent.lstat()
    if (
        not stat.S_ISDIR(db_parent.st_mode)
        or db_parent.st_uid != os.getuid()
        or db_parent.st_mode & 0o077
        or database.is_symlink()
    ):
        raise RuntimeError("private persistent worker database directory required")
    if database.exists():
        db_stat = database.lstat()
        if (
            not stat.S_ISREG(db_stat.st_mode)
            or db_stat.st_uid != os.getuid()
            or db_stat.st_mode & 0o077
        ):
            raise RuntimeError("private owner-controlled worker database required")
    robot = BoosterRobot(timeout=0.5, enable_tf_listener=False)
    engine = Engine(robot, config, args.database)
    try:
        await engine.initialize()
    except BaseException:
        await asyncio.to_thread(robot._close)
        engine.db.close()
        raise
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    connections = set()

    async def handle(reader, writer):
        task = asyncio.current_task()
        connections.add(task)
        try:
            if len(connections) > 8:
                raise ValueError("bridge capacity exceeded")
            raw = await asyncio.wait_for(reader.readline(), 2)
            request = json.loads(raw)
            try:
                result = await asyncio.wait_for(
                    engine.call(request["method"], request["args"]), 2
                )
                response = {"id": request["id"], "ok": True, "result": result}
            except Exception as error:
                response = {
                    "id": request["id"],
                    "ok": False,
                    "error": type(error).__name__,
                }
            payload = (encoded(response) + "\n").encode()
            if len(payload) > 1_048_576:
                raise ValueError("bridge response too large")
            writer.write(payload)
            await asyncio.wait_for(writer.drain(), 2)
        except Exception:
            pass
        finally:
            connections.discard(task)
            writer.close()
            await writer.wait_closed()

    observer = asyncio.create_task(engine.observe())
    monitor = asyncio.create_task(engine.monitor())
    monitor.add_done_callback(lambda task: stop.set() if not engine.closing else None)
    server = await asyncio.start_unix_server(handle, str(args.socket), limit=16384)
    args.socket.chmod(0o600)
    try:
        async with server:
            await stop.wait()
    finally:
        engine.stop("worker_shutdown")
        for _ in range(40):
            if engine.active is None:
                break
            await asyncio.sleep(0.05)
        engine.closing = True
        observer.cancel()
        monitor.cancel()
        await asyncio.gather(observer, monitor, return_exceptions=True)
        # Keep unresolved durable state on restart; closing SDK is not stop evidence.
        await asyncio.to_thread(robot._close)
        if engine.started and not engine.started.done():
            engine.started.cancel()
            await asyncio.gather(engine.started, return_exceptions=True)
        if engine.cancel_job:
            await asyncio.gather(engine.cancel_job, return_exceptions=True)
        engine.db.close()
        args.socket.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--socket", required=True, type=Path)
    parser.add_argument("--database", required=True)
    asyncio.run(serve(parser.parse_args()))

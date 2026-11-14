"""K1 SDK contract fixtures; these never constitute robot acceptance."""

import asyncio
import importlib.util
from pathlib import Path
import time
from types import SimpleNamespace as NS
import uuid

import pytest

SPEC = importlib.util.spec_from_file_location(
    "k1_worker", Path(__file__).parents[1] / "integrations/booster_k1/worker.py"
)
worker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(worker)


class Frame(list):
    def __init__(self, values):
        super().__init__(values)
        self.timestamp = time.time()


class Handle:
    status = "RUNNING"
    task_id = "hand_wave"
    trace_id = "synthetic-sdk-task"

    def __init__(self):
        self.cancel_calls = 0

    def cancel(self):
        self.cancel_calls += 1
        if self.cancel_calls < 3:
            return False  # Real SDK refuses until it has observed start.
        self.status = "CANCELLED"
        return True


class SDK:
    def __init__(self):
        self.robot_info = NS(
            manufacturer="Booster Robotics",
            model="K1",
            firmware_version="fixture",
            serial_number="fixture",
        )
        self.handle = Handle()
        self.starts = 0
        self.delay = 0
        self.read_delay = 0
        self.stale = False

    def list_joints(self):
        return [NS(name="fixture_joint")]

    def list_actions(self):
        return [NS(id="hand_wave", type="upper_body", interruptible=True)]

    def get_active_tasks(self):
        time.sleep(self.read_delay)
        return [self.handle] if self.starts and self.handle.status == "RUNNING" else []

    def get_joint_states(self):
        if self.stale:
            raise RuntimeError("fixture disconnected")
        return Frame([NS(name="fixture_joint", position=0.1, velocity=0.0)])

    def get_fall_down_state(self):
        return NS(timestamp=time.time(), state="normal")

    def get_mode(self):
        return "walk"

    def do_action(self, action_id):
        assert action_id == "hand_wave"
        self.starts += 1
        time.sleep(self.delay)
        return self.handle


def config():
    return {
        "qualified": True,
        "serial_number": "fixture",
        "firmware_version": "fixture",
        "evidence_record": "SYNTHETIC ONLY",
        "allowed_actions": ["hand_wave"],
    }


def request(epoch=1):
    return {
        "command_id": str(uuid.uuid4()),
        "action_id": "hand_wave",
        "hold_seconds": 0.1,
        "duration": 2,
        "epoch": epoch,
        "issued_utc": time.time(),
        "ttl_ms": 500,
    }


async def ready(engine):
    await engine.initialize()
    engine.health = engine.snapshot()
    engine.health_at = time.monotonic()


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0.01)


async def finish(engine, *jobs):
    engine.closing = True
    for job in jobs:
        job.cancel()
    await asyncio.gather(*jobs, return_exceptions=True)
    if engine.started:
        await engine.started
    if engine.cancel_job:
        await engine.cancel_job
    engine.db.close()


async def test_k1_hold_uses_vendor_stop_retries_and_feedback(tmp_path):
    sdk = SDK()
    engine = worker.Engine(sdk, config(), tmp_path / "worker.db")
    await ready(engine)
    observer, monitor = (
        asyncio.create_task(engine.observe()),
        asyncio.create_task(engine.monitor()),
    )
    req = request()
    try:
        await engine.start(req)
        await until(lambda: engine.row(req["command_id"])["state"] == "completed")
        row = engine.row(req["command_id"])
        assert sdk.starts == 1 and sdk.handle.cancel_calls == 3
        assert row["detail"]["evidence"] == "controller_ack"
        assert row["detail"]["physical_observation"] is False
        assert row["detail"]["feedback"][0]["source"] == "vendor_joint_feedback"
        await engine.start(req)
        assert sdk.starts == 1  # Durable idempotency.
    finally:
        await finish(engine, observer, monitor)


async def test_k1_lost_lease_cancels_late_start_without_resending(tmp_path):
    sdk = SDK()
    sdk.delay = 0.2
    engine = worker.Engine(sdk, config(), tmp_path / "worker.db")
    await ready(engine)
    observer, monitor = (
        asyncio.create_task(engine.observe()),
        asyncio.create_task(engine.monitor()),
    )
    req = request()
    req.update(ttl_ms=50, hold_seconds=1)
    try:
        await engine.start(req)
        await until(lambda: engine.row(req["command_id"])["state"] == "cancelled")
        assert sdk.starts == 1 and sdk.handle.cancel_calls >= 3
        assert engine.row(req["command_id"])["detail"]["stop_reason"] == "expired_lease"
        with pytest.raises(ValueError):
            engine.renew({**req, "issued_utc": time.time()})
    finally:
        await finish(engine, observer, monitor)


async def test_k1_stop_fences_inflight_admission(tmp_path):
    sdk = SDK()
    sdk.read_delay = 0.1
    engine = worker.Engine(sdk, config(), tmp_path / "worker.db")
    await ready(engine)
    task = asyncio.create_task(engine.start(request()))
    await asyncio.sleep(0.02)
    engine.stop("owner_stop")
    with pytest.raises(ValueError, match="authority"):
        await task
    assert sdk.starts == 0
    await finish(engine)


async def test_k1_lease_expiring_during_admission_is_durably_rejected(
    tmp_path, monkeypatch
):
    sdk = SDK()
    path = tmp_path / "worker.db"
    engine = worker.Engine(sdk, config(), path)
    await ready(engine)
    req = request()
    req["ttl_ms"] = 50
    clock = [req["issued_utc"]]
    monkeypatch.setattr(
        worker, "time", NS(time=lambda: clock[0], monotonic=time.monotonic)
    )

    def controller_check():
        clock[0] += 0.1  # The lease expires while admission is awaiting the SDK.
        return []

    monkeypatch.setattr(sdk, "get_active_tasks", controller_check)
    try:
        result = await engine.start(req)
        assert result["state"] == "rejected"
        assert result["detail"]["evidence"] == "none"
        assert result["detail"]["physical_observation"] is False
        assert engine.active is None and engine.started is None
        assert await engine.start(req) == result
        assert sdk.starts == 0
    finally:
        await finish(engine)

    recovered = worker.Engine(sdk, config(), path)
    await ready(recovered)
    try:
        assert recovered.row(req["command_id"]) == result
        assert await recovered.start(req) == result
        assert sdk.starts == 0
        fresh = request(2)
        fresh["issued_utc"] = clock[0]
        assert (await recovered.start(fresh))["state"] == "pending"
        await recovered.started
        assert sdk.starts == 1
        assert recovered.row(req["command_id"]) == result
    finally:
        await finish(recovered)


async def test_k1_restart_keeps_uncertainty_and_requires_owner_evidence(tmp_path):
    sdk = SDK()
    path = tmp_path / "worker.db"
    engine = worker.Engine(sdk, config(), path)
    await ready(engine)
    req = request()
    await engine.start(req)
    await engine.started
    await finish(engine)
    recovered = worker.Engine(sdk, config(), path)
    await ready(recovered)
    try:
        assert recovered.row(req["command_id"])["state"] == "unknown"
        with pytest.raises(ValueError, match="unreconciled"):
            await recovered.start(request(2))
        with pytest.raises(ValueError):
            await recovered.recover(
                {
                    "command_id": req["command_id"],
                    "observed_stopped": True,
                    "evidence_record": "fixture",
                }
            )
        sdk.handle.status = "CANCELLED"
        row = await recovered.recover(
            {
                "command_id": req["command_id"],
                "observed_stopped": True,
                "evidence_record": "fixture owner observation",
            }
        )
        assert row["detail"]["evidence"] == "owner_assertion"
    finally:
        await finish(recovered)


async def test_k1_wrong_identity_and_noninterruptible_action_rejected(tmp_path):
    sdk = SDK()
    sdk.robot_info.model = "T1"
    engine = worker.Engine(sdk, config(), tmp_path / "worker.db")
    with pytest.raises(ValueError, match="K1"):
        await engine.initialize()
    sdk.robot_info.model = "K1"
    sdk.list_actions = lambda: [
        NS(id="hand_wave", type="whole_body", interruptible=False)
    ]
    with pytest.raises(ValueError, match="interruptible"):
        await engine.initialize()
    await finish(engine)


async def test_real_unix_client_with_worker_engine_preserves_action_identity():
    import json
    import os
    import tempfile
    from orbo_runtime.broker import Broker
    from orbo_runtime.storage import Store
    from orbo_runtime.k1_adapter import K1Adapter

    with tempfile.TemporaryDirectory(prefix="k1-", dir="/tmp") as directory:
        root = Path(directory)
        root.chmod(0o700)
        sdk = SDK()
        engine = worker.Engine(sdk, config(), root / "commands.db")
        await ready(engine)

        async def handle(reader, writer):
            request = json.loads(await reader.readline())
            try:
                result = await engine.call(request["method"], request["args"])
                reply = {"id": request["id"], "ok": True, "result": result}
            except ValueError:
                reply = {"id": request["id"], "ok": False}
            writer.write((json.dumps(reply) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        socket = root / "bridge.sock"
        server = await asyncio.start_unix_server(handle, str(socket))
        socket.chmod(0o600)
        adapter = K1Adapter(
            {"socket": str(socket), "qualified": True, "allowed_actions": ["hand_wave"]}
        )
        observer, monitor = (
            asyncio.create_task(engine.observe()),
            asyncio.create_task(engine.monitor()),
        )
        try:
            async with server:
                info = await adapter.inspect()
                assert info["qualified"] and info["model"] == "K1"
                store = Store(root / "runtime.db")
                try:
                    store.grant(
                        "owner", "robot.command", "booster-k1", True, time.time() + 30
                    )
                    episode = store.episode(
                        "owner", profile="physical_task.v1", provenance="synthetic"
                    )
                    broker = Broker(store, adapter)
                    result = await broker.dispatch(
                        "owner",
                        episode,
                        "fixture-key",
                        "humanoid.action",
                        {"action_id": "hand_wave", "hold_seconds": 0.1},
                        broker.acquire("owner"),
                        duration=2,
                    )
                    assert (
                        result["state"] == "completed"
                        and result["evidence"] == "controller_ack"
                    )
                    assert sdk.starts == 1
                    row = store.db.execute(
                        "SELECT payload FROM events WHERE kind='action.completed'"
                    ).fetchone()
                    assert (
                        json.loads(row[0])["detail"]["vendor_trace_id"]
                        == "synthetic-sdk-task"
                    )
                finally:
                    store.close()
                os.chmod(socket, 0o666)
                from orbo_runtime.contracts import Rejected

                with pytest.raises(Rejected, match="private"):
                    await adapter.inspect()
        finally:
            await finish(engine, observer, monitor)


async def test_k1_stop_after_durable_intent_prevents_queued_sdk_call(tmp_path):
    sdk = SDK()
    engine = worker.Engine(sdk, config(), tmp_path / "worker.db")
    await ready(engine)
    req = request()
    await engine.start(req)
    engine.stop("owner_stop")
    await engine.started
    assert sdk.starts == 0 and engine.row(req["command_id"])["state"] == "rejected"
    await finish(engine)


async def test_k1_stale_feedback_stops_owned_action(tmp_path):
    sdk = SDK()
    engine = worker.Engine(sdk, config(), tmp_path / "worker.db")
    await ready(engine)
    req = request()
    req.update(hold_seconds=1)
    observer = asyncio.create_task(engine.observe())
    monitor = asyncio.create_task(engine.monitor())
    try:
        await engine.start(req)
        await engine.started
        sdk.stale = True
        await until(lambda: engine.row(req["command_id"])["state"] == "cancelled")
        assert (
            engine.row(req["command_id"])["detail"]["stop_reason"] == "stale_feedback"
        )
    finally:
        await finish(engine, observer, monitor)


async def test_k1_disconnected_runtime_closes_and_records_unconfirmed_stop(tmp_path):
    import sqlite3
    from orbo_runtime.cli import LocalService

    config = {
        "runtime": {
            "owner": "owner",
            "profile": "booster_k1",
            "database": str(tmp_path / "runtime.db"),
        },
        "robot": {
            "socket": str(tmp_path / "missing.sock"),
            "allowed_actions": ["hand_wave"],
            "qualified": False,
        },
    }
    service = LocalService(config)
    await service.close()
    with sqlite3.connect(tmp_path / "runtime.db") as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM events WHERE kind='shutdown.stop_unconfirmed'"
            ).fetchone()[0]
            == 1
        )

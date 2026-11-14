"""Session ownership races and lease isolation, without ROS or hardware."""

import asyncio
import threading
import time
from types import SimpleNamespace as NS

import pytest

from orbo_runtime.broker import Broker
from orbo_runtime.contracts import (
    Action,
    ActionResult,
    Capability,
    ManualClock,
    Rejected,
    identity,
)
from orbo_runtime.ros_adapter import OrboAdapter
from orbo_runtime.storage import Store


def adapter_fixture():
    adapter = object.__new__(OrboAdapter)
    adapter.robot_id = "orbo"
    adapter.health = NS(boot_id=42, epoch=0, armed=False, fault=0)
    adapter._snapshot = lambda: adapter.health
    adapter.lock = threading.Lock()
    adapter.session_lock = asyncio.Lock()
    adapter.session_generation = adapter.sequence = 0
    adapter.owned_session = None
    adapter.flipper_busy = False
    adapter.teleoperation = adapter.navigation = None
    adapter.statuses, adapter.command_keys, adapter.command_sessions = {}, {}, {}
    adapter.pulses = [1450, 1550]
    adapter.capabilities = {
        "attention.gesture": Capability(
            "attention.gesture", ("flippers",), 5, ("controller_ack",), True
        )
    }
    adapter.session_type = adapter.flipper_type = NS
    sessions, gestures = [], []

    def session(msg):
        sessions.append(msg)
        adapter.health.armed = msg.operation == 1
        if msg.operation == 1:
            adapter.health.epoch = msg.epoch

    adapter.session_pub = NS(publish=session)
    adapter.flipper_pub = NS(publish=gestures.append)
    return adapter, sessions, gestures


def action(epoch=1):
    return Action(
        identity(),
        identity(),
        "orbo",
        "attention.gesture",
        {"gesture": "attention"},
        epoch,
        time.time() + 2,
        2,
    )


async def published(gestures):
    async with asyncio.timeout(1):
        while not gestures:
            await asyncio.sleep(0.001)
    return gestures[-1]


async def test_gesture_joins_owned_wheels_and_ignores_old_epoch_completion():
    adapter, sessions, gestures = adapter_fixture()
    await adapter.arm(1, wheels=True)
    session = adapter.owned_session
    work = asyncio.create_task(adapter.dispatch(action()))
    msg = await published(gestures)
    adapter.statuses[msg.command_id] = (
        NS(state=3, epoch=0, evidence=1, reason=0),
        time.monotonic(),
    )
    await asyncio.sleep(0.03)
    assert not work.done() and adapter.owned_session is session
    adapter.statuses[msg.command_id] = (
        NS(state=3, epoch=1, evidence=1, reason=0),
        time.monotonic(),
    )
    result = await work
    assert result.state == "completed" and result.detail["shared_wheel_session"]
    assert [s.operation for s in sessions] == [1]
    assert adapter.health.armed and not adapter.flipper_busy


@pytest.mark.parametrize("failure", ["stop", "reboot", "fault", "wrong_epoch"])
async def test_shared_gesture_cannot_finish_after_session_is_invalidated(failure):
    adapter, sessions, gestures = adapter_fixture()
    await adapter.arm(1, wheels=True)
    work = asyncio.create_task(adapter.dispatch(action()))
    msg = await published(gestures)
    if failure == "stop":
        await adapter.stop_session()
    elif failure == "reboot":
        adapter.health.boot_id = 43
    elif failure == "fault":
        adapter.health.fault = 6
    else:
        adapter.health.epoch = 2
    adapter.statuses[msg.command_id] = (
        NS(state=3, epoch=1, evidence=1, reason=0),
        time.monotonic(),
    )
    with pytest.raises(Rejected, match="session changed"):
        await work
    assert not adapter.flipper_busy
    assert sum(s.operation == 1 for s in sessions) == 1


async def test_competing_gesture_and_foreign_session_are_rejected():
    adapter, sessions, gestures = adapter_fixture()
    adapter.health.armed = True
    with pytest.raises(Rejected, match="another owner"):
        await adapter.dispatch(action())
    assert not sessions and not gestures
    adapter.health.armed = False
    await adapter.arm(1, wheels=True)
    work = asyncio.create_task(adapter.dispatch(action()))
    await published(gestures)
    with pytest.raises(Rejected, match="already in progress"):
        await adapter.dispatch(action())
    with pytest.raises(Rejected, match="already owned"):
        await adapter.arm(2, wheels=True)
    assert len(gestures) == 1 and len(sessions) == 1
    await adapter.stop_session()
    with pytest.raises(Rejected):
        await work


async def test_stop_interrupts_arm_without_late_rearm():
    adapter, sessions, _ = adapter_fixture()
    # Leave the arm unacknowledged so stop interrupts its wait.
    adapter.session_pub.publish = sessions.append
    work = asyncio.create_task(adapter.arm(1, wheels=True))
    async with asyncio.timeout(1):
        while not sessions:
            await asyncio.sleep(0.001)
    await adapter.stop_session()
    adapter.health.epoch, adapter.health.armed = 1, True  # Delayed arm acknowledgment.
    with pytest.raises(Rejected, match="stopped during arming"):
        await work
    assert adapter.owned_session is None
    assert [s.operation for s in sessions] == [1, 0, 0]


async def test_reboot_during_stationary_replacement_never_arms_the_new_boot():
    adapter, sessions, _ = adapter_fixture()
    await adapter.arm(1)

    def reboot_on_stop(msg):
        sessions.append(msg)
        adapter.health.boot_id, adapter.health.epoch, adapter.health.armed = (
            43,
            0,
            False,
        )

    adapter.session_pub.publish = reboot_on_stop
    with pytest.raises(Rejected, match="restarted during arming"):
        await adapter.arm(2)
    assert [s.operation for s in sessions] == [1, 0, 0]
    assert adapter.owned_session is None


async def test_broker_joins_same_lease_without_renewal_and_keeps_resources_separate(
    tmp_path,
):
    clock = ManualClock()
    store = Store(tmp_path / "state.db", clock)
    start, finish = asyncio.Event(), asyncio.Event()

    class Robot:
        robot_id, simulated, shares_motion_session = "orbo", False, True
        epoch_seen, shared_wheel_epoch = 0, None
        capabilities = {
            "teleop.session": Capability(
                "teleop.session", ("wheels",), 30, ("controller_ack",), True
            ),
            "attention.gesture": Capability(
                "attention.gesture", ("flippers",), 5, ("controller_ack",), True
            ),
        }

        async def dispatch(self, request):
            if request.capability == "teleop.session":
                self.shared_wheel_epoch = request.epoch
                start.set()
                await finish.wait()
            return ActionResult(request.command_id, "completed", "controller_ack")

        async def controlled_stop(self, reason):
            finish.set()

    robot = Robot()
    broker = Broker(store, robot)
    episode = store.episode("owner", provenance="synthetic")
    store.grant("owner", "robot.command", "orbo", True, clock.utc() + 60)
    store.grant("other", "robot.command", "orbo", True, clock.utc() + 60)
    epoch = broker.acquire("owner")
    original_deadline = broker.lease_deadline
    work = asyncio.create_task(
        broker.dispatch("owner", episode, "drive", "teleop.session", {}, epoch, 20)
    )
    try:
        await start.wait()
        assert broker.gesture_epoch("owner") == epoch
        assert broker.lease_deadline == original_deadline
        with pytest.raises(Rejected, match="expired or fenced"):
            broker.gesture_epoch("other")
        with pytest.raises(Rejected, match="robot action active"):
            broker.acquire("owner")
        gesture = await broker.dispatch(
            "owner",
            episode,
            "gesture",
            "attention.gesture",
            {"gesture": "attention"},
            epoch,
        )
        assert gesture["state"] == "completed" and not work.done()
        robot.shared_wheel_epoch = None
        with pytest.raises(Rejected, match="no joinable"):
            broker.gesture_epoch("owner")
        robot.shared_wheel_epoch = epoch
        store.grant("owner", "robot.command", "orbo", False, clock.utc() + 60)
        with pytest.raises(Rejected):
            broker.gesture_epoch("owner")
        store.grant("owner", "robot.command", "orbo", True, clock.utc() + 60)
        finish.set()
        await work
        clock.advance(31)
        with pytest.raises(Rejected, match="expired or fenced"):
            broker.gesture_epoch("owner")
    finally:
        finish.set()
        await asyncio.gather(work, return_exceptions=True)
        store.close()

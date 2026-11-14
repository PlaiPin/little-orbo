"""Real Orbo micro-ROS adapter. Imports ROS only in the target profile."""

import asyncio
from collections import deque
from dataclasses import dataclass
import threading
import time
import uuid

from .contracts import ActionResult, Capability, Rejected


@dataclass(frozen=True)
class OwnedSession:
    boot: int
    epoch: int
    wheels: bool


class OrboAdapter:
    simulated = False
    shares_motion_session = True

    def __init__(self, config):
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from orbo_embedded_interfaces.msg import (
            ActuatorStatus,
            ControlSession,
            FlipperCommand,
            Health,
        )

        if config.get("teleop_enabled") and config.get("navigation_enabled"):
            raise Rejected("select teleop or navigation, never both")
        self.robot_id = config.get("id", "orbo")
        self.pulses = config.get("attention_pulse_us", [])
        qualified = config.get("flippers_qualified", False) is True
        if qualified and (
            len(self.pulses) != 2
            or any(type(v) is not int or not 0 < v < 20000 for v in self.pulses)
        ):
            raise Rejected("commissioned attention pulses required")
        self.capabilities = {
            "attention.gesture": Capability(
                "attention.gesture", ("flippers",), 5, ("controller_ack",), qualified
            )
        }
        self.session_type, self.flipper_type = ControlSession, FlipperCommand
        self.context = Context()
        rclpy.init(context=self.context)
        self.node = rclpy.create_node(
            "orbo_task_adapter", namespace="/orbo", context=self.context
        )
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.session_pub = self.node.create_publisher(
            ControlSession, "embedded/control_session", 1
        )
        self.flipper_pub = self.node.create_publisher(
            FlipperCommand, "embedded/flipper_command", 1
        )
        self.lock = threading.Lock()
        self.health = None
        self.retired_boots = deque(maxlen=8)
        self.health_at = 0.0
        self.statuses = {}
        self.command_keys = {}
        self.command_sessions = {}
        self.sequence = 0
        # Only the asyncio owner arms/stops; the ROS executor only records feedback.
        self.session_lock = asyncio.Lock()
        self.session_generation = 0
        self.owned_session = None
        self.flipper_busy = False
        self.node.create_subscription(
            Health, "embedded/health", self._health, qos_profile_sensor_data
        )
        self.node.create_subscription(
            ActuatorStatus,
            "embedded/actuator_status",
            self._status,
            qos_profile_sensor_data,
        )
        self.teleoperation = None
        if config.get("teleop_enabled", False):
            from .teleop import Teleoperation

            self.teleoperation = Teleoperation(
                self,
                config.get("calibration", "/etc/orbo/calibration.yaml"),
                config.get("teleop", {}),
            )
        self.navigation = None
        if config.get("navigation_enabled", False):
            from .navigation import Navigation

            self.navigation = Navigation(
                self, config.get("calibration", "/etc/orbo/calibration.yaml")
            )
        self.thread = threading.Thread(
            target=self.executor.spin, name="orbo-ros", daemon=True
        )
        self.thread.start()

    @property
    def epoch_seen(self):
        with self.lock:
            return self.health.epoch if self.health else 0

    def _health(self, msg):
        if msg.interface_version != 1:
            return
        with self.lock:
            if msg.boot_id in self.retired_boots:
                return
            if self.health and self.health.boot_id == msg.boot_id:
                if msg.mono_us <= self.health.mono_us or msg.epoch < self.health.epoch:
                    return
            elif self.health:
                self.retired_boots.append(self.health.boot_id)
                self.statuses.clear()
            self.health, self.health_at = msg, time.monotonic()

    def _status(self, msg):
        with self.lock:
            if not self.health or msg.boot_id != self.health.boot_id:
                return
            old = self.statuses.get(msg.command_id)
            if old and old[0].sequence >= msg.sequence:
                return
            if len(self.statuses) >= 128 and msg.command_id not in self.statuses:
                self.statuses.pop(next(iter(self.statuses)))
            self.statuses[msg.command_id] = (msg, time.monotonic())

    def _snapshot(self):
        with self.lock:
            if (
                not self.health
                or time.monotonic() - self.health_at > 0.6
                or not self.health.clock_synced
                or not 0 <= time.time_ns() // 1_000_000 - self.health.epoch_ms <= 600
            ):
                raise Rejected("fresh synchronized MCU health required")
            return self.health

    def _session(self, operation, epoch, *, wheels=False):
        health = self._snapshot()
        self.sequence += 1
        msg = self.session_type()
        msg.interface_version, msg.boot_id, msg.epoch = 1, health.boot_id, epoch
        msg.sequence = self.sequence
        msg.issued_epoch_ms = time.time_ns() // 1_000_000
        msg.valid_for_ms, msg.operation, msg.enable_wheels = 500, operation, wheels
        self.session_pub.publish(msg)

    def session_health(self, session):
        health = self._snapshot()
        if (
            self.owned_session is not session
            or health.boot_id != session.boot
            or health.epoch != session.epoch
            or not health.armed
            or health.fault
        ):
            raise Rejected("owned MCU session changed, faulted or disarmed")
        return health

    @property
    def shared_wheel_epoch(self):
        session = self.owned_session
        if session is None or not session.wheels:
            return None
        self.session_health(session)
        return session.epoch

    async def arm(self, epoch, *, wheels=False):
        async with self.session_lock:
            health = self._snapshot()
            if epoch <= health.epoch:
                raise Rejected("lease epoch must fence MCU epoch")
            if self.owned_session and self.owned_session.wheels:
                raise Rejected(
                    "wheel session already owned; stop before starting another"
                )
            if wheels and self.flipper_busy:
                raise Rejected(
                    "flipper command in progress; cannot replace its session"
                )
            if health.armed and (
                self.owned_session is None
                or health.boot_id != self.owned_session.boot
                or health.epoch != self.owned_session.epoch
            ):
                raise Rejected("MCU armed by another owner")
            generation = self.session_generation
            boot = health.boot_id

            def current():
                if generation != self.session_generation:
                    raise Rejected("session stopped during arming")
                health = self._snapshot()
                if health.boot_id != boot:
                    raise Rejected("MCU restarted during arming")
                return health

            try:
                # Only an idle, locally owned stationary session may be replaced.
                if health.armed:
                    self._session(0, health.epoch)
                    async with asyncio.timeout(0.7):
                        while current().armed:
                            await asyncio.sleep(0.01)
                current()
                self._session(1, epoch, wheels=wheels)
                async with asyncio.timeout(0.7):
                    while True:
                        health = current()
                        if health.armed and health.epoch == epoch and not health.fault:
                            break
                        await asyncio.sleep(0.01)
                self.owned_session = OwnedSession(health.boot_id, epoch, wheels)
                return health
            except BaseException:
                await self.stop_session()
                raise

    async def gesture_session(self, epoch):
        async with self.session_lock:
            session = self.owned_session
            if session and session.epoch == epoch:
                self.session_health(session)
                return session
            if session and session.wheels:
                raise Rejected("gesture must use the active wheel session epoch")
        await self.arm(epoch)
        return self.owned_session

    async def dispatch(self, action):
        if action.capability == "teleop.session" and self.teleoperation:
            return await self.teleoperation.dispatch(action)
        if action.capability == "navigation.named_goal" and self.navigation:
            return await self.navigation.dispatch(action)
        if action.capability != "attention.gesture" or action.parameters != {
            "gesture": "attention"
        }:
            raise Rejected("unsupported Orbo action")
        if not self.capabilities[action.capability].qualified:
            raise Rejected("flippers not commissioned")
        if self.flipper_busy:
            raise Rejected("flipper command already in progress")
        self.flipper_busy = True
        try:
            return await self.dispatch_gesture(action)
        finally:
            self.flipper_busy = False

    async def dispatch_gesture(self, action):
        started = time.monotonic()
        session = await self.gesture_session(action.epoch)
        health = self.session_health(session)
        remaining = min(
            action.deadline - time.time(),
            action.duration - (time.monotonic() - started),
        )
        if remaining <= 0.1:
            raise Rejected("gesture deadline elapsed during session admission")
        key = uuid.UUID(action.command_id).int & ((1 << 64) - 1)
        if key in self.command_keys and self.command_keys[key] != action.command_id:
            raise Rejected("embedded command identity collision")
        if key not in self.command_keys and len(self.command_keys) >= 128:
            oldest = next(iter(self.command_keys))
            self.command_keys.pop(oldest)
            self.command_sessions.pop(oldest, None)
        self.command_keys[key] = action.command_id
        self.command_sessions[key] = session
        self.sequence += 1
        msg = self.flipper_type()
        msg.boot_id, msg.epoch, msg.sequence, msg.command_id = (
            health.boot_id,
            action.epoch,
            self.sequence,
            key,
        )
        msg.issued_epoch_ms = time.time_ns() // 1_000_000 - 20
        msg.valid_for_ms = min(200, int(remaining * 1000))
        msg.duration_ms = min(5000, max(1, int((remaining - 0.1) * 1000)))
        msg.pulse_us = self.pulses
        self.flipper_pub.publish(msg)
        async with asyncio.timeout(remaining):
            while True:
                self.session_health(session)
                with self.lock:
                    observed = self.statuses.get(key)
                if observed and observed[0].epoch == action.epoch:
                    state = observed[0].state
                    if state in {3, 4, 5, 6}:
                        if observed[0].evidence != 1 or (
                            state == 3 and observed[0].reason
                        ):
                            raise Rejected("invalid gesture completion evidence")
                        return ActionResult(
                            action.command_id,
                            {
                                3: "completed",
                                4: "rejected",
                                5: "cancelled",
                                6: "failed",
                            }[state],
                            "controller_ack",
                            {
                                "physical_observation": False,
                                "mcu_reason": observed[0].reason,
                                "shared_wheel_session": session.wheels,
                            },
                        )
                await asyncio.sleep(0.01)

    async def controlled_stop(self, reason):
        if self.teleoperation:
            self.teleoperation.stop()
        if self.navigation:
            self.navigation.stop()
        await self.stop_session()

    async def stop_session(self):
        # Invalidate both streams immediately, even before MCU feedback arrives.
        # A pending arm cannot resume after this stop.
        self.session_generation += 1
        self.owned_session = None
        # Stop is allowed even if health is stale; no new motion is authorized.
        self.sequence += 1
        msg = self.session_type()
        msg.interface_version, msg.operation, msg.sequence = 1, 0, self.sequence
        self.session_pub.publish(msg)

    async def cancel(self, command_id):
        await self.controlled_stop("cancel")
        # A request to stop does not establish measured cessation.
        return ActionResult(command_id, "unknown", "none", {"stop_requested": True})

    async def reconcile(self, command_id):
        self._snapshot()
        if self.teleoperation and command_id in self.teleoperation.results:
            return self.teleoperation.results[command_id]
        if self.navigation and command_id in self.navigation.results:
            return self.navigation.results[command_id]
        key = uuid.UUID(command_id).int & ((1 << 64) - 1)
        with self.lock:
            observed = self.statuses.get(key)
        session = self.command_sessions.get(key)
        if (
            observed
            and session
            and observed[0].state == 3
            and observed[0].evidence == 1
            and observed[0].reason == 0
            and observed[0].boot_id == session.boot
            and observed[0].epoch == session.epoch
        ):
            return ActionResult(
                command_id,
                "completed",
                "controller_ack",
                {"physical_observation": False},
            )
        return ActionResult(command_id, "unknown", "none")

    async def close(self):
        await self.controlled_stop("runtime shutdown")
        self.executor.shutdown(timeout_sec=1)
        self.thread.join(timeout=1)
        self.node.destroy_node()
        self.context.shutdown()

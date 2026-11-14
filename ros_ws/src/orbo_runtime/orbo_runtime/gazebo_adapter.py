"""Gazebo-only adapter sharing task, broker, Nav2 and short velocity authority.

Requires room.launch.py embodied:=true. Explicit simulation domain checks prevent
this adapter from controlling a physical robot. The independently running existing
VelocityGate is the only publisher to the simulated drive controller in this mode.
"""

import asyncio
import os
from pathlib import Path
import threading
from types import SimpleNamespace

from .contracts import Rejected
from .navigation import Navigation


def require_simulation_domain():
    pair = (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION"))
    allowed = {("95", "little-orbo-sim")}
    if os.environ.get("ORBO_GAZEBO_TESTS") == "1":
        allowed |= {
            ("99", "little-orbo-sim-test"),
            ("100", "little-orbo-embodied-test"),
        }
    if pair not in allowed:
        raise Rejected("isolated Gazebo domain and partition required")


class GazeboAdapter:
    robot_id = "orbo-gazebo"
    simulated = True
    shares_motion_session = False

    def __init__(self, goals_path, *, map_version="living-room-map"):
        require_simulation_domain()
        import rclpy
        from rclpy.context import Context
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.parameter import Parameter
        from rclpy.signals import SignalHandlerOptions
        from orbo_embedded_interfaces.msg import ControlSession
        from .observations import ROSPoseSource
        import yaml

        self.context = Context()
        rclpy.init(context=self.context, signal_handler_options=SignalHandlerOptions.NO)
        self.node = rclpy.create_node(
            "embodied_sim_adapter",
            namespace="orbo_sim",
            context=self.context,
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        self.session_type = ControlSession
        self.session_pub = self.node.create_publisher(
            ControlSession, "embedded/control_session", 1
        )
        self.capabilities, self.sequence = {}, 0
        self.map_version = map_version
        self.health = SimpleNamespace(boot_id=1, epoch=0, armed=False)
        self.stop_subscription = self.node.create_subscription(
            ControlSession, "embedded/control_session", self._gate_stop, 1
        )
        self.dispatches = 0
        self._ready = False
        self.stops = 0
        goals = yaml.safe_load(Path(goals_path).read_text())
        self.waypoints = {
            name: dict(zip(("x", "y", "yaw"), values, strict=True))
            for name, values in goals.items()
        }
        self.navigation = Navigation(self, simulation_waypoints=self.waypoints)
        self.pose_source = ROSPoseSource(
            self.node, self.waypoints, map_version=map_version, yaw_tolerance=0.3
        )
        self.executor = SingleThreadedExecutor(context=self.context)
        self.executor.add_node(self.node)
        self.thread = threading.Thread(target=self.executor.spin, daemon=True)
        self.thread.start()

    async def ready(self, timeout=60):
        from lifecycle_msgs.msg import State
        from lifecycle_msgs.srv import GetState

        async with asyncio.timeout(timeout):
            for name in ("bt_navigator", "collision_monitor"):
                client = self.node.create_client(GetState, f"{name}/get_state")
                try:
                    while True:
                        if client.service_is_ready():
                            response = await self.navigation.wait(
                                client.call_async(GetState.Request())
                            )
                            if response.current_state.id == State.PRIMARY_STATE_ACTIVE:
                                break
                        await asyncio.sleep(0.1)
                finally:
                    self.node.destroy_client(client)
            while not self.navigation.client.server_is_ready():
                await asyncio.sleep(0.05)
            # The safety gate must be a separate process, not a task-local shim.
            while not any(
                n.node_name == "embodied_velocity_gate"
                for n in self.node.get_publishers_info_by_topic(
                    "/orbo_sim/drive_controller/cmd_vel"
                )
            ):
                await asyncio.sleep(0.1)
        self._ready = True

    async def arm(self, epoch, wheels=False):
        if not wheels:
            raise Rejected("simulation adapter only supports named navigation")
        self.health.epoch, self.health.armed = epoch, True
        return self.health

    def _snapshot(self):
        return SimpleNamespace(**vars(self.health))

    def _gate_stop(self, message):
        if message.interface_version == 1 and message.operation == 0:
            self.health.armed = False

    async def stop_session(self):
        self.health.armed = False
        self.sequence += 1
        stop = self.session_type()
        stop.interface_version, stop.operation, stop.sequence = 1, 0, self.sequence
        self.session_pub.publish(stop)

    async def dispatch(self, action):
        if not self._ready:
            raise Rejected("simulation safety gate and Nav2 must be ready")
        if action.capability != "navigation.named_goal":
            raise Rejected("simulation supports surveyed navigation only")
        self.dispatches += 1
        return await self.navigation.dispatch(action)

    async def controlled_stop(self, reason):
        self.stops += 1
        self.navigation.stop()
        await self.stop_session()

    async def cancel(self, command_id):
        await self.controlled_stop("cancel")
        return await self.navigation.cancel_goal(command_id)

    async def reconcile(self, command_id):
        return await self.navigation.query_result(command_id)

    def pose(self):
        return self.pose_source.snapshot()

    async def close(self):
        await self.controlled_stop("simulation shutdown")
        self.executor.shutdown(timeout_sec=2)
        self.thread.join(timeout=2)
        self.node.destroy_node()
        self.context.shutdown()

"""Named surveyed Nav2 goals, bounded action lifetime and expiring wheel authority."""

import asyncio
from collections import deque
import threading
import math
import time
import uuid
from pathlib import Path

from .commissioning import validate_calibration
from .contracts import ActionResult, Capability, Rejected


class Navigation:
    def __init__(self, adapter, calibration_path=None, *, simulation_waypoints=None):
        import yaml
        from geometry_msgs.msg import TwistStamped
        from nav2_msgs.action import NavigateToPose
        from rclpy.action import ActionClient
        from action_msgs.srv import CancelGoal

        if simulation_waypoints is not None:
            if not getattr(adapter, "simulated", False):
                raise Rejected("simulation waypoints cannot commission hardware")
            self.waypoints = simulation_waypoints
        else:
            calibration = validate_calibration(
                yaml.safe_load(Path(calibration_path).read_text()), navigation=True
            )
            self.waypoints = calibration["navigation"].get("waypoints", {})
        self.result_evidence = (
            "simulation" if getattr(adapter, "simulated", False) else "controller_ack"
        )
        if not self.waypoints or len(self.waypoints) > 32:
            raise Rejected("one to 32 surveyed named waypoints required")
        for name, pose in self.waypoints.items():
            if (
                not isinstance(name, str)
                or not name
                or len(name) > 64
                or not isinstance(pose, dict)
                or set(pose) != {"x", "y", "yaw"}
                or any(
                    type(v) not in {float, int} or not math.isfinite(v)
                    for v in pose.values()
                )
            ):
                raise Rejected("invalid surveyed waypoint")
        self.adapter, self.action_type = adapter, NavigateToPose
        self.client = ActionClient(adapter.node, NavigateToPose, "navigate_to_pose")
        self.result_client = adapter.node.create_client(
            NavigateToPose.Impl.GetResultService, "navigate_to_pose/_action/get_result"
        )
        self.cancel_type = CancelGoal
        self.cancel_client = adapter.node.create_client(
            CancelGoal, "navigate_to_pose/_action/cancel_goal"
        )
        self.lease_pub = adapter.node.create_publisher(
            adapter.session_type, "control/lease", 1
        )
        self.observation_lock = threading.Lock()
        self.command_stamp_ns = 0
        self.feedback = deque(maxlen=64)
        self.last_feedback_mono = 0.0
        adapter.node.create_subscription(TwistStamped, "cmd_vel", self._command, 1)
        self.active_goal = None
        self.stopping = False
        self.results = {}
        self.pending_results = {}
        adapter.capabilities["navigation.named_goal"] = Capability(
            "navigation.named_goal",
            ("wheels",),
            600,
            (self.result_evidence,),
            not getattr(adapter, "simulated", False),
        )

    def _command(self, message):
        stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
        with self.observation_lock:
            self.command_stamp_ns = max(self.command_stamp_ns, stamp)

    def _feedback(self, message):
        observation = message.feedback
        pose = observation.current_pose
        stamp_ns = pose.header.stamp.sec * 1_000_000_000 + pose.header.stamp.nanosec
        age_ns = self.adapter.node.get_clock().now().nanoseconds - stamp_ns
        if pose.header.frame_id != "map" or not 0 <= age_ns <= 2_000_000_000:
            return
        values = [
            pose.pose.position.x,
            pose.pose.position.y,
            pose.pose.orientation.z,
            pose.pose.orientation.w,
            observation.distance_remaining,
        ]
        if not all(math.isfinite(value) for value in values):
            return
        now = time.monotonic()
        with self.observation_lock:
            if now - self.last_feedback_mono < 0.2:
                return
            self.last_feedback_mono = now
            self.feedback.append(
                {
                    "source": "nav2_localization_feedback",
                    "received_mono": now,
                    "frame_id": pose.header.frame_id,
                    "stamp_ns": pose.header.stamp.sec * 1_000_000_000
                    + pose.header.stamp.nanosec,
                    "x": values[0],
                    "y": values[1],
                    "orientation_z": values[2],
                    "orientation_w": values[3],
                    "distance_remaining": values[4],
                    "recoveries": observation.number_of_recoveries,
                }
            )

    async def wait(self, future):
        # rclpy's executor resolves the future on its own thread.
        while not future.done():
            await asyncio.sleep(0.01)
        if future.cancelled():
            raise Rejected("ROS action future cancelled")
        return future.result()

    async def dispatch(self, action):
        if (
            set(action.parameters) != {"waypoint"}
            or action.parameters["waypoint"] not in self.waypoints
        ):
            raise Rejected("unknown commissioned waypoint")
        if self.active_goal or not self.client.server_is_ready():
            raise Rejected("Nav2 unavailable or busy; activate and localize before use")
        self.stopping = False
        deadline = time.monotonic() + min(
            action.duration, action.deadline - time.time()
        )
        goal = self.action_type.Goal()
        goal.pose.header.frame_id = "map"
        goal.pose.header.stamp = self.adapter.node.get_clock().now().to_msg()
        pose = self.waypoints[action.parameters["waypoint"]]
        goal.pose.pose.position.x, goal.pose.pose.position.y = (
            float(pose["x"]),
            float(pose["y"]),
        )
        goal.pose.pose.orientation.z = math.sin(pose["yaw"] / 2)
        goal.pose.pose.orientation.w = math.cos(pose["yaw"] / 2)
        with self.observation_lock:
            self.feedback.clear()
            self.command_stamp_ns = 0
            self.last_feedback_mono = time.monotonic()
        goal_stamp_ns = (
            goal.pose.header.stamp.sec * 1_000_000_000 + goal.pose.header.stamp.nanosec
        )
        send = self.client.send_goal_async(
            goal,
            feedback_callback=self._feedback,
            goal_uuid=self._goal_uuid(action.command_id),
        )
        try:
            self.active_goal = await self.wait(send)
            if self.active_goal.accepted:
                result_future = self._remember_goal(action.command_id, self.active_goal)
            if not self.active_goal.accepted or self.stopping:
                raise Rejected("navigation goal not admitted")
            # Planning can exceed the MCU's arm timeout. Wait for the current
            # goal's fresh collision-filtered command before granting motion.
            while not result_future.done():
                if self.stopping or time.monotonic() >= deadline:
                    raise Rejected("navigation cancelled or planning deadline elapsed")
                now_ns = self.adapter.node.get_clock().now().nanoseconds
                with self.observation_lock:
                    stamp_ns = self.command_stamp_ns
                if stamp_ns >= goal_stamp_ns and 0 <= now_ns - stamp_ns < 200_000_000:
                    break
                await asyncio.sleep(0.01)
            boot_id = None
            if not result_future.done():
                health = await self.adapter.arm(action.epoch, wheels=True)
                boot_id = health.boot_id
            while not result_future.done():
                health = self.adapter._snapshot()
                with self.observation_lock:
                    feedback_age = time.monotonic() - self.last_feedback_mono
                if feedback_age > 2:
                    raise Rejected("navigation localization feedback stale")
                if (
                    self.stopping
                    or time.monotonic() >= deadline
                    or health.boot_id != boot_id
                    or not health.armed
                    or health.epoch != action.epoch
                ):
                    raise Rejected("navigation authority expired or controller changed")
                self.adapter.sequence += 1
                lease = self.adapter.session_type()
                lease.interface_version, lease.operation, lease.enable_wheels = (
                    1,
                    2,
                    True,
                )
                lease.boot_id, lease.epoch, lease.sequence = (
                    boot_id,
                    action.epoch,
                    self.adapter.sequence,
                )
                lease.issued_epoch_ms = (
                    self.adapter.node.get_clock().now().nanoseconds // 1_000_000
                )
                lease.valid_for_ms = min(
                    250, max(1, int((deadline - time.monotonic()) * 1000))
                )
                self.lease_pub.publish(lease)
                await asyncio.sleep(0.05)
            response = await self.wait(result_future)
            # Nav2 SUCCEEDED is controller/localization evidence, not independent physical verification.
            state = {4: "completed", 5: "cancelled", 6: "failed"}.get(
                response.status, "unknown"
            )
            with self.observation_lock:
                feedback = list(self.feedback)
            result = ActionResult(
                action.command_id,
                state,
                self.result_evidence if state != "unknown" else "none",
                {
                    "nav2_status": response.status,
                    "error_code": getattr(response.result, "error_code", None),
                    "waypoint": action.parameters["waypoint"],
                    "goal_pose": {"frame_id": "map", **pose},
                    "navigation_feedback": feedback,
                    "motion_armed": boot_id is not None,
                    "physical_observation": False,
                },
            )
            self.results[action.command_id] = result
            if len(self.results) > 128:
                self.results.pop(next(iter(self.results)))
            return result
        finally:
            self.stopping = True
            # A goal response may arrive after cancellation of the local wait.
            await self.adapter.stop_session()
            if self.active_goal and self.active_goal.accepted:
                self.active_goal.cancel_goal_async()
            elif send.done():
                late = send.result()
                if late and late.accepted:
                    self._remember_goal(action.command_id, late)
                    late.cancel_goal_async()
            else:

                def cancel_late(future):
                    if not future.cancelled() and future.exception() is None:
                        late = future.result()
                        if late and late.accepted:
                            self._remember_goal(action.command_id, late)
                            late.cancel_goal_async()

                send.add_done_callback(cancel_late)
            self.active_goal = None

    def stop(self):
        self.stopping = True
        if self.active_goal:
            self.active_goal.cancel_goal_async()

    def _remember_goal(self, command_id, goal):
        if command_id not in self.pending_results:
            self.pending_results[command_id] = goal.get_result_async()
            if len(self.pending_results) > 128:
                self.pending_results.pop(next(iter(self.pending_results)))
        return self.pending_results[command_id]

    def reconcile(self, command_id):
        """Recover Nav2 terminal acknowledgment after local cancellation."""
        if command_id in self.results:
            return self.results[command_id]
        future = self.pending_results.get(command_id)
        if future is None and self.result_client.service_is_ready():
            request = self.action_type.Impl.GetResultService.Request()
            request.goal_id = self._goal_uuid(command_id)
            future = self.result_client.call_async(request)
            self.pending_results[command_id] = future
            if len(self.pending_results) > 128:
                self.pending_results.pop(next(iter(self.pending_results)))
        if future is None:
            return ActionResult(command_id, "unknown", "none")
        # rclpy Future.done() excludes the cancelled state.
        if future.cancelled():
            self.pending_results.pop(command_id, None)
            return ActionResult(command_id, "unknown", "none")
        if not future.done():
            return ActionResult(command_id, "unknown", "none")
        if future.exception():
            self.pending_results.pop(command_id, None)
            return ActionResult(command_id, "unknown", "none")
        response = future.result()
        state = {4: "completed", 5: "cancelled", 6: "failed"}.get(
            response.status, "unknown"
        )
        result = ActionResult(
            command_id,
            state,
            self.result_evidence if state != "unknown" else "none",
            {"nav2_status": response.status, "physical_observation": False},
        )
        if state != "unknown":
            self.results[command_id] = result
        self.pending_results.pop(command_id, None)
        return result

    @staticmethod
    def _goal_uuid(command_id):
        from unique_identifier_msgs.msg import UUID

        return UUID(uuid=list(uuid.UUID(command_id).bytes))

    async def query_result(self, command_id, timeout=0.25):
        """Read an existing goal's result with no redispatch or motor authority."""
        deadline = time.monotonic() + timeout
        while True:
            result = self.reconcile(command_id)
            if result.state != "unknown" or time.monotonic() >= deadline:
                return result
            future = self.pending_results.get(command_id)
            if future is None:
                return result
            await asyncio.sleep(0.01)

    async def cancel_goal(self, command_id):
        """Explicit cancellation by the durable UUID also works after restart."""
        if self.cancel_client.service_is_ready():
            request = self.cancel_type.Request()
            request.goal_info.goal_id = self._goal_uuid(command_id)
            try:
                async with asyncio.timeout(0.25):
                    await self.wait(self.cancel_client.call_async(request))
            except TimeoutError:
                pass
        return await self.query_result(command_id)

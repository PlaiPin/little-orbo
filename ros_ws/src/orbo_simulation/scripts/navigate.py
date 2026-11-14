#!/usr/bin/env python3
"""Send Nav2 goals only to the isolated Gazebo Orbo. Never publishes wheel commands."""

import argparse
import json
import math
import os
from pathlib import Path
import time

import rclpy
from action_msgs.msg import GoalStatus
from action_msgs.srv import CancelGoal
from ament_index_python.packages import get_package_share_directory
from geometry_msgs.msg import PoseStamped
from nav2_msgs.action import NavigateToPose
from lifecycle_msgs.msg import State
from lifecycle_msgs.srv import GetState
from rclpy.action import ActionClient
from rclpy.parameter import Parameter
from rclpy.signals import SignalHandlerOptions
import yaml


def check_domain():
    allowed = {("95", "little-orbo-sim")}
    if os.environ.get("ORBO_GAZEBO_TESTS") == "1":
        allowed.add(("99", "little-orbo-sim-test"))
    if (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION")) not in allowed:
        raise RuntimeError(
            "Use pixi run orbo-sim-goal in the isolated simulation domain"
        )


class Navigator:
    def __init__(self):
        check_domain()
        self.node = rclpy.create_node(
            "sim_goal_client",
            namespace="orbo_sim",
            parameter_overrides=[Parameter("use_sim_time", value=True)],
        )
        self.client = ActionClient(self.node, NavigateToPose, "navigate_to_pose")
        self.handle = None
        self.goal_pending = False

    def wait(self, future, timeout):
        deadline = time.monotonic() + timeout
        while not future.done():
            rclpy.spin_once(self.node, timeout_sec=0.1)
            if time.monotonic() > deadline:
                raise TimeoutError("Nav2 response timed out")
        return future.result()

    def cancel(self):
        if self.handle is not None:
            reply = self.wait(self.handle.cancel_goal_async(), 5)
        else:
            client = self.node.create_client(
                CancelGoal, "navigate_to_pose/_action/cancel_goal"
            )
            if not client.wait_for_service(timeout_sec=5):
                raise RuntimeError("Nav2 cancel service unavailable")
            try:
                reply = self.wait(client.call_async(CancelGoal.Request()), 5)
            finally:
                self.node.destroy_client(client)
        print(
            f"Cancellation response: {len(reply.goals_canceling)} goal(s) canceling",
            flush=True,
        )
        return reply

    def wait_until_active(self):
        # Action services exist before lifecycle activation. Wait for the entire
        # command chain, otherwise a goal sent during startup is rejected.
        deadline = time.monotonic() + 60
        for name in ("bt_navigator", "collision_monitor"):
            service = self.node.create_client(GetState, f"{name}/get_state")
            try:
                while time.monotonic() < deadline:
                    if service.wait_for_service(timeout_sec=1):
                        reply = self.wait(service.call_async(GetState.Request()), 5)
                        if reply.current_state.id == State.PRIMARY_STATE_ACTIVE:
                            break
                    rclpy.spin_once(self.node, timeout_sec=0.1)
                else:
                    raise RuntimeError(
                        f"Nav2 {name} is not active; check the simulator log"
                    )
            finally:
                self.node.destroy_client(service)
        if not self.client.wait_for_server(timeout_sec=5):
            raise RuntimeError("Nav2 is not active; start orbo-sim")

    def go(self, x, y, yaw, timeout=120):
        if (
            not all(math.isfinite(v) for v in (x, y, yaw))
            or abs(x) > 1.85
            or abs(y) > 1.85
        ):
            raise ValueError("Goal must be finite and inside the living room (±1.85 m)")
        self.wait_until_active()
        msg = NavigateToPose.Goal()
        msg.pose = PoseStamped()
        msg.pose.header.frame_id = "map"
        msg.pose.header.stamp = self.node.get_clock().now().to_msg()
        msg.pose.pose.position.x = float(x)
        msg.pose.pose.position.y = float(y)
        msg.pose.pose.orientation.z = math.sin(yaw / 2)
        msg.pose.pose.orientation.w = math.cos(yaw / 2)
        self.goal_pending = True
        self.handle = self.wait(self.client.send_goal_async(msg), 10)
        self.goal_pending = False
        if not self.handle.accepted:
            self.handle = None
            raise RuntimeError("Nav2 rejected goal")
        print(f"Navigating to ({x:.2f}, {y:.2f}), yaw {yaw:.2f}", flush=True)
        result = self.wait(self.handle.get_result_async(), timeout)
        self.handle = None
        if result.status != GoalStatus.STATUS_SUCCEEDED:
            raise RuntimeError(f"Nav2 result status {result.status}: {result.result}")
        print("Goal reached", flush=True)
        return {"goal": [x, y, yaw], "status": "succeeded"}

    def close(self):
        self.client.destroy()
        self.node.destroy_node()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    goal = commands.add_parser("goal")
    goal.add_argument("x", type=float)
    goal.add_argument("y", type=float)
    goal.add_argument("yaw", type=float, nargs="?", default=0.0)
    named = commands.add_parser("named")
    named.add_argument("name", choices=["entry", "tv", "sofa", "reading"])
    commands.add_parser("tour")
    commands.add_parser("cancel")
    args = parser.parse_args()
    check_domain()
    # Keep the ROS context alive while KeyboardInterrupt requests cancellation.
    rclpy.init(signal_handler_options=SignalHandlerOptions.NO)
    nav = Navigator()
    try:
        if args.command == "cancel":
            nav.cancel()
            return
        goals = yaml.safe_load(
            (
                Path(get_package_share_directory("orbo_simulation"))
                / "config/goals.yaml"
            ).read_text()
        )
        targets = (
            [(args.x, args.y, args.yaw)]
            if args.command == "goal"
            else (
                [goals[args.name]]
                if args.command == "named"
                else [goals[k] for k in ("tv", "sofa", "reading", "entry")]
            )
        )
        results = [nav.go(*target) for target in targets]
        print(json.dumps(results))
    except BaseException as error:
        if nav.handle is not None or nav.goal_pending:
            try:
                nav.cancel()
            except Exception as exc:
                print(f"Cancellation unconfirmed: {exc}", flush=True)
        if isinstance(error, KeyboardInterrupt):
            raise SystemExit(130) from None
        raise
    finally:
        nav.close()
        rclpy.shutdown()


if __name__ == "__main__":
    main()

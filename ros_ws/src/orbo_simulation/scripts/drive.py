#!/usr/bin/env python3
"""Drive only the Gazebo robot, using stamped commands and simulation time."""

import argparse
import math
import os
import select
import sys
import termios
import time
import tty

import rclpy
from geometry_msgs.msg import TwistStamped
from nav_msgs.msg import Odometry
from rclpy.parameter import Parameter


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "mode", choices=["keyboard", "demo", "stop"], nargs="?", default="keyboard"
    )
    args = parser.parse_args()
    if os.environ.get("ROS_DOMAIN_ID") != "95":
        parser.error("Simulation domain 95 required. Use pixi run orbo-sim-drive.")
    if args.mode == "keyboard" and not sys.stdin.isatty():
        parser.error("Keyboard mode needs an interactive terminal")
    rclpy.init()
    node = rclpy.create_node(
        "sim_drive",
        namespace="orbo_sim",
        parameter_overrides=[Parameter("use_sim_time", value=True)],
    )
    publisher = node.create_publisher(
        TwistStamped, "/orbo_sim/drive_controller/cmd_vel", 1
    )
    state = {"odom": None, "received": 0.0}

    def received(msg):
        state.update(odom=msg, received=time.monotonic())

    subscription = node.create_subscription(
        Odometry, "/orbo_sim/drive_controller/odom", received, 10
    )

    def send(linear=0.0, angular=0.0):
        msg = TwistStamped()
        msg.header.stamp = node.get_clock().now().to_msg()
        msg.twist.linear.x, msg.twist.angular.z = linear, angular
        publisher.publish(msg)

    previous = None
    try:
        deadline = time.monotonic() + 30
        while state["odom"] is None or publisher.get_subscription_count() == 0:
            rclpy.spin_once(node, timeout_sec=0.05)
            if time.monotonic() > deadline:
                raise RuntimeError(
                    "No active simulated drive controller; start pixi run orbo-sim first"
                )
        if args.mode == "stop":
            for _ in range(5):
                send()
                rclpy.spin_once(node, timeout_sec=0.04)
            return
        if args.mode == "keyboard":
            print(
                "SIMULATION — W/S forward/reverse, A/D turn, Space stop, Q quit.\n"
                "Hold/repeat a key to move; commands expire after 0.35 s without input.",
                flush=True,
            )
            previous = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())
            linear = angular = 0.0
            last_key = 0.0
            while rclpy.ok():
                rclpy.spin_once(node, timeout_sec=0.01)
                if select.select([sys.stdin], [], [], 0.01)[0]:
                    key = sys.stdin.read(1).lower()
                    if key in ("q", "\x03"):
                        break
                    linear, angular = {
                        "w": (0.12, 0.0),
                        "s": (-0.12, 0.0),
                        "a": (0.0, 0.8),
                        "d": (0.0, -0.8),
                    }.get(key, (0.0, 0.0))
                    last_key = time.monotonic()
                if (
                    time.monotonic() - last_key > 0.35
                    or time.monotonic() - state["received"] > 0.5
                ):
                    linear = angular = 0.0
                send(linear, angular)
                time.sleep(0.02)
        else:
            # A bounded odometry-based square in the clear southwest part of the room.
            # Scripted driving, not Nav2 planning or obstacle avoidance.
            def yaw(msg):
                q = msg.pose.pose.orientation
                return math.atan2(
                    2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)
                )

            def segment(turn=False):
                start = state["odom"]
                start_yaw = yaw(start)
                wall_deadline = time.monotonic() + 30
                while rclpy.ok():
                    rclpy.spin_once(node, timeout_sec=0.02)
                    if (
                        time.monotonic() > wall_deadline
                        or time.monotonic() - state["received"] > 1
                    ):
                        raise RuntimeError(
                            "Simulation paused or odometry stopped during demo"
                        )
                    current = state["odom"]
                    if turn:
                        delta = math.atan2(
                            math.sin(yaw(current) - start_yaw),
                            math.cos(yaw(current) - start_yaw),
                        )
                        remaining = math.pi / 2 - delta
                        if remaining < 0.015:
                            break
                        send(0.0, min(0.8, max(0.12, remaining * 2)))
                    else:
                        a, b = start.pose.pose.position, current.pose.pose.position
                        remaining = 0.5 - math.hypot(b.x - a.x, b.y - a.y)
                        if remaining < 0.005:
                            break
                        send(min(0.12, max(0.025, remaining)), 0.0)
                    time.sleep(0.02)
                for _ in range(12):
                    send()
                    rclpy.spin_once(node, timeout_sec=0.025)

            print(
                "Driving a 0.5 m square using simulated encoder odometry.", flush=True
            )
            for side in range(4):
                segment()
                segment(turn=True)
                print(f"Completed side {side + 1}/4", flush=True)
    finally:
        if previous is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, previous)
        if rclpy.ok():
            for _ in range(5):
                send()
                rclpy.spin_once(node, timeout_sec=0.02)
        node.destroy_subscription(subscription)
        node.destroy_node()
        rclpy.shutdown()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass

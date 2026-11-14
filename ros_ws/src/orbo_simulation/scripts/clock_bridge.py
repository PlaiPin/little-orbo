#!/usr/bin/env python3
"""Bridge Gazebo time at 100 Hz without flooding every Nav2 node at physics rate."""

import os

from gz.msgs10.clock_pb2 import Clock as GzClock
from gz.transport13 import Node as GzNode, SubscribeOptions
import rclpy
from rclpy.executors import ExternalShutdownException
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rosgraph_msgs.msg import Clock


def main():
    allowed = {("95", "little-orbo-sim")}
    if os.environ.get("ORBO_GAZEBO_TESTS") == "1":
        allowed.add(("99", "little-orbo-sim-test"))
    if (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION")) not in allowed:
        raise RuntimeError("Simulation domain/partition required")
    rclpy.init()
    node = rclpy.create_node("simulation_clock")
    publisher = node.create_publisher(
        Clock, "/clock", QoSProfile(depth=1, reliability=ReliabilityPolicy.BEST_EFFORT)
    )
    transport = GzNode()
    options = SubscribeOptions()
    options.msgs_per_sec = 100

    def receive(msg):
        if rclpy.ok():
            clock = Clock()
            clock.clock.sec = msg.sim.sec
            clock.clock.nanosec = msg.sim.nsec
            publisher.publish(clock)

    # Transport throttles before invoking Python. Forward actual simulation
    # timestamps (including rewinds); never synthesize or advance paused time.
    transport.subscribe(GzClock, "/clock", receive, options)
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, ExternalShutdownException):
        pass
    finally:
        transport.unsubscribe("/clock")
        node.destroy_node()
        if rclpy.ok():
            rclpy.shutdown()


if __name__ == "__main__":
    main()

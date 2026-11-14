"""Terminal Joy source. Every motion key expires; never grants robot authority."""

import os
import select
import sys
import termios
import time
import tty


def main():
    import rclpy
    from sensor_msgs.msg import Joy

    if not sys.stdin.isatty():
        raise SystemExit("Run in an interactive terminal (SSH with -t is supported)")
    rclpy.init()
    node = rclpy.create_node("orbo_keyboard", namespace="/orbo")
    pub = node.create_publisher(Joy, "teleop/joy", 1)
    saved = termios.tcgetattr(sys.stdin)
    expires, axes = 0.0, [0.0, 0.0]
    print(
        "W/S forward/reverse; A/D rotate; SPACE neutral; Q exit. Motion key repeat expires after 150 ms. Authorize a bounded robot.teleop session separately.",
        flush=True,
    )
    try:
        tty.setcbreak(sys.stdin.fileno())
        while rclpy.ok():
            if select.select([sys.stdin], [], [], 0.02)[0]:
                keys = os.read(sys.stdin.fileno(), 4096).decode(errors="ignore").lower()
                key = (
                    "q" if any(k in keys for k in ("q", "\x03")) else (keys[-1:] or "q")
                )
                if key in ("q", "\x03", ""):
                    break
                axes = {
                    "w": [1.0, 0.0],
                    "s": [-1.0, 0.0],
                    "a": [0.0, 1.0],
                    "d": [0.0, -1.0],
                }.get(key, [0.0, 0.0])
                expires = time.monotonic() + 0.15
            held = time.monotonic() < expires and any(axes)
            msg = Joy()
            msg.header.stamp = node.get_clock().now().to_msg()
            msg.axes = axes if held else [0.0, 0.0]
            msg.buttons = [int(held)]
            pub.publish(msg)
            rclpy.spin_once(node, timeout_sec=0)
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, saved)
        node.destroy_node()
        rclpy.try_shutdown()

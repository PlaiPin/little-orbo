"""Camera launch validation and metadata-only ROS health monitoring."""

import math
from pathlib import Path
import re
import time

from .contracts import Rejected


def camera_spec(config):
    if config.get("enabled") is not True:
        raise Rejected("camera capture disabled; explicitly configure and enable it")
    driver = config.get("driver")
    if driver not in {"libcamera", "v4l2"}:
        raise Rejected("camera driver must be libcamera or v4l2")
    for key in ("width", "height"):
        if type(config.get(key)) is not int or not 16 <= config[key] <= 4096:
            raise Rejected("invalid camera image dimensions")
    if config["width"] * config["height"] > 1920 * 1080:
        raise Rejected("camera profile bounded to 1920x1080 pixels")
    frame = config.get("frame_id", "")
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,63}", frame):
        raise Rejected("explicit camera optical frame required")
    info = config.get("calibration_file", "")
    if info and (not Path(info).is_absolute() or not Path(info).is_file()):
        raise Rejected("camera calibration must name an existing absolute YAML path")
    uri = Path(info).as_uri() if info else ""
    if driver == "libcamera":
        selected = config.get("camera")
        if not isinstance(selected, str) or not selected.strip():
            raise Rejected("explicit libcamera camera identifier required")
        params = {
            "camera": selected,
            "width": config["width"],
            "height": config["height"],
            "frame_id": frame,
            "role": "viewfinder",
            "format": "RGB888",
            "camera_info_url": uri,
        }
        package, executable = "camera_ros", "camera_node"
        remaps = [
            ("~/image_raw", "/orbo/camera/image_raw"),
            ("~/camera_info", "/orbo/camera/camera_info"),
        ]
    else:
        selected = config.get("video_device", "")
        if not isinstance(selected, str) or not selected.startswith("/dev/"):
            raise Rejected("explicit V4L2 device path required")
        fmt = config.get("pixel_format", "YUYV")
        if fmt not in {"YUYV", "UYVY", "GREY"}:
            raise Rejected("unsupported V4L2 pixel format")
        params = {
            "video_device": selected,
            "image_size": [config["width"], config["height"]],
            "camera_frame_id": frame,
            "camera_info_url": uri,
            "pixel_format": fmt,
            "output_encoding": "rgb8",
        }
        package, executable = "v4l2_camera", "v4l2_camera_node"
        remaps = [
            ("image_raw", "/orbo/camera/image_raw"),
            ("camera_info", "/orbo/camera/camera_info"),
        ]
    mount = config.get("mount")
    if mount is not None:
        if (
            not isinstance(mount, dict)
            or not mount.get("evidence_record")
            or any(
                type(mount.get(k)) not in (int, float) or not math.isfinite(mount[k])
                for k in ("x", "y", "z", "roll", "pitch", "yaw")
            )
        ):
            raise Rejected("measured camera optical transform and evidence required")
    return package, executable, params, remaps


class CameraHealth:
    def __init__(self, frame):
        self.frame, self.last_stamp, self.received = frame, 0, None
        self.frames, self.calibrated = 0, False
        self.info_received = None
        self.shape = None
        self.error = "no frames received"

    def observe(self, message, now_ns, mono):
        stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
        if (
            message.header.frame_id != self.frame
            or not 0 <= now_ns - stamp < 1_000_000_000
            or stamp <= self.last_stamp
        ):
            self.error = "invalid frame identity or stale/reordered timestamp"
            return
        if (
            not 0 < message.width * message.height <= 1920 * 1080
            or not 0 < message.step <= message.width * 16
            or len(message.data) != message.step * message.height
        ):
            self.error = "invalid or oversized image layout"
            return
        self.last_stamp, self.received, self.shape = (
            stamp,
            mono,
            (message.width, message.height),
        )
        self.frames += 1
        self.error = None

    def info(self, message, mono):
        self.calibrated = bool(
            message.header.frame_id == self.frame
            and self.shape == (message.width, message.height)
            and all(math.isfinite(v) for v in message.k)
            and message.k[0] > 0
            and message.k[4] > 0
        )
        self.info_received = mono

    def status(self, mono):
        if self.error or self.received is None or mono - self.received >= 1:
            return 2, self.error or "camera stream stale"
        if (
            not self.calibrated
            or self.info_received is None
            or mono - self.info_received >= 1
        ):
            return 1, "frames arriving; intrinsics missing, stale or mismatched"
        return (
            0,
            "frames and calibrated CameraInfo arriving; physical qualification pending",
        )


def main():
    import rclpy
    from rclpy.node import Node
    from rclpy.qos import qos_profile_sensor_data
    from sensor_msgs.msg import Image, CameraInfo
    from diagnostic_msgs.msg import DiagnosticArray, DiagnosticStatus, KeyValue

    class Monitor(Node):
        def __init__(self):
            super().__init__("orbo_camera_monitor", namespace="/orbo")
            self.declare_parameter("frame_id", "camera_optical_frame")
            self.health = CameraHealth(self.get_parameter("frame_id").value)
            self.create_subscription(
                Image,
                "camera/image_raw",
                lambda m: self.health.observe(
                    m, self.get_clock().now().nanoseconds, time.monotonic()
                ),
                qos_profile_sensor_data,
            )
            self.create_subscription(
                CameraInfo,
                "camera/camera_info",
                lambda m: self.health.info(m, time.monotonic()),
                qos_profile_sensor_data,
            )
            self.output = self.create_publisher(
                DiagnosticArray, "camera/diagnostics", 1
            )
            self.create_timer(0.5, self.tick)

        def tick(self):
            level, text = self.health.status(time.monotonic())
            status = DiagnosticStatus(
                level=bytes([level]),
                name="orbo/camera",
                message=text,
                hardware_id=self.health.frame,
            )
            status.values = [
                KeyValue(key="frames", value=str(self.health.frames)),
                KeyValue(key="raw_storage", value="disabled"),
                KeyValue(key="cloud_upload", value="disabled"),
            ]
            msg = DiagnosticArray()
            msg.header.stamp = self.get_clock().now().to_msg()
            msg.status = [status]
            self.output.publish(msg)

    rclpy.init()
    node = Monitor()
    try:
        rclpy.spin(node)
    except (KeyboardInterrupt, rclpy.executors.ExternalShutdownException):
        pass
    finally:
        node.destroy_node()
        rclpy.try_shutdown()

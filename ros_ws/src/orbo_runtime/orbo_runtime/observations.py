"""Opt-in, bounded snapshots from a latest-frame source; no continuous upload."""

import asyncio
from dataclasses import dataclass
import math
import struct
import threading
import time
import zlib

from .contracts import Rejected, finite
from .evidence_memory import label


@dataclass(frozen=True)
class RGBFrame:
    data: bytes
    width: int
    height: int
    captured_at: float
    received_mono: float
    frame_id: str
    clock_domain: str
    source_stamp_ns: int
    map_version: str
    calibration_version: str
    viewpoint: str
    pose: dict
    pose_received_mono: float


def encode_png(frame):
    """Encode tightly packed RGB8 using only stdlib, with explicit memory limits."""
    if (
        type(frame.width) is not int
        or type(frame.height) is not int
        or not 16 <= frame.width <= 1920
        or not 16 <= frame.height <= 1080
        or frame.width * frame.height > 1920 * 1080
        or not isinstance(frame.data, bytes)
        or len(frame.data) != frame.width * frame.height * 3
    ):
        raise Rejected("invalid bounded RGB8 frame")
    # Fast sampled exposure test. This does not certify focus or object visibility.
    pixels = frame.data[:: max(3, (len(frame.data) // 4096 // 3) * 3)]
    average = sum(pixels) / len(pixels)
    if average < 8 or average > 247:
        raise Rejected("unsuitable frame exposure")

    def chunk(kind, data):
        return (
            struct.pack("!I", len(data))
            + kind
            + data
            + struct.pack("!I", zlib.crc32(kind + data))
        )

    stride = frame.width * 3
    raw = b"".join(
        b"\0" + frame.data[offset : offset + stride]
        for offset in range(0, len(frame.data), stride)
    )
    result = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack("!2I5B", frame.width, frame.height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 3))
        + chunk(b"IEND", b"")
    )
    if len(result) > 2_000_000:
        raise Rejected("encoded snapshot exceeds 2 MB")
    return result, {
        "exposure_mean_sample": round(average, 2),
        "exposure_usable": True,
        "focus_verified": False,
        "visibility_verified": False,
    }


class LatestFrame:
    """One-slot buffer shared with ROS callback; only an enabled capture consumes it."""

    def __init__(self):
        self.lock = threading.Lock()
        self.latest = None

    def update(self, frame):
        with self.lock:
            if self.latest and frame.source_stamp_ns <= self.latest.source_stamp_ns:
                return
            self.latest = frame

    def snapshot(self):
        with self.lock:
            if self.latest is None:
                raise Rejected("camera unavailable")
            return self.latest


class ObservationService:
    def __init__(self, memory, frame_source, *, max_age=1.0, timeout=3.0):
        self.memory, self.source = memory, frame_source
        self.max_age = finite(max_age, 0.05, 2.0)
        self.timeout = finite(timeout, 0.1, 10.0)
        self.busy = False

    async def capture(
        self,
        owner,
        environment,
        map_version,
        task_id,
        revision,
        viewpoint,
        *,
        retention_seconds=3600,
        check_revision=None,
    ):
        if self.busy:
            raise Rejected("snapshot capture busy")
        self.memory.store.permission(owner, "observation.capture", environment)
        self.memory.store.permission(owner, "observation.retain", environment)
        if check_revision:
            check_revision()
        self.busy = True
        try:
            if hasattr(self.source, "refresh"):
                async with asyncio.timeout(self.timeout):
                    await self.source.refresh()
            frame = self.source.snapshot()
            self._validate(frame, map_version, viewpoint)
            async with asyncio.timeout(self.timeout):
                png, quality = await asyncio.to_thread(encode_png, frame)
            # Recheck authority and freshness after encoding, before persistent effects.
            self._validate(frame, map_version, viewpoint)
            self.memory.store.permission(owner, "observation.capture", environment)
            if check_revision:
                check_revision()
            return self.memory.add_observation(
                owner,
                environment,
                {
                    "task_id": task_id,
                    "revision": revision,
                    "map_version": map_version,
                    "captured_at": frame.captured_at,
                    "received_mono": frame.received_mono,
                    "boot": self.memory.store.boot,
                    "clock_domain": frame.clock_domain,
                    "source_stamp_ns": frame.source_stamp_ns,
                    "frame_id": frame.frame_id,
                    "calibration_version": frame.calibration_version,
                    "viewpoint": viewpoint,
                    "pose": frame.pose,
                    "pose_received_mono": frame.pose_received_mono,
                    "width": frame.width,
                    "height": frame.height,
                    "quality": quality,
                    "permitted_uses": ["private_memory"],
                },
                png,
                retention_seconds=retention_seconds,
            )
        finally:
            self.busy = False

    def _validate(self, frame, map_version, viewpoint):
        clock = self.memory.store.clock
        if not isinstance(frame, RGBFrame):
            raise Rejected("RGBFrame required")
        if frame.map_version != map_version or frame.viewpoint != viewpoint:
            raise Rejected("snapshot pose/map does not match admitted viewpoint")
        if not 0 <= clock.mono() - frame.received_mono <= self.max_age:
            raise Rejected("stale camera frame")
        if not 0 <= clock.mono() - frame.pose_received_mono <= self.max_age:
            raise Rejected("stale localization")
        if not 0 <= clock.utc() - frame.captured_at <= self.max_age:
            raise Rejected("stale capture timestamp")
        for name in (
            "clock_domain",
            "frame_id",
            "calibration_version",
            "map_version",
            "viewpoint",
        ):
            label(getattr(frame, name), name)
        if type(frame.source_stamp_ns) is not int or frame.source_stamp_ns <= 0:
            raise Rejected("capture source timestamp required")
        if (
            not isinstance(frame.pose, dict)
            or set(frame.pose) != {"x", "y", "yaw", "frame_id"}
            or frame.pose["frame_id"] != "map"
            or any(
                type(frame.pose[k]) not in (int, float)
                or not math.isfinite(frame.pose[k])
                for k in ("x", "y", "yaw")
            )
        ):
            raise Rejected("fresh finite map pose required")


class ROSFrameSource(LatestFrame):
    """ROS RGB/Image + CameraInfo adapter; pose_provider must supply fresh map pose.

    pose_provider() -> {map_version, viewpoint, pose, received_mono}.
    ROS timestamps are validated against the node clock; UTC records receipt
    minus source age, explicitly retaining the ROS source stamp and clock domain.
    """

    def __init__(
        self,
        node,
        pose_provider,
        *,
        frame_id,
        calibration_version,
        clock_domain="ros",
        image_topic="/orbo/camera/image_raw",
        info_topic="/orbo/camera/camera_info",
    ):
        super().__init__()
        from sensor_msgs.msg import Image, CameraInfo
        from rclpy.qos import qos_profile_sensor_data

        self.node, self.pose_provider = node, pose_provider
        self.frame_id, self.calibration_version = frame_id, calibration_version
        self.clock_domain = clock_domain
        self.info = None
        self.subscriptions = [
            node.create_subscription(
                Image, image_topic, self.on_image, qos_profile_sensor_data
            ),
            node.create_subscription(
                CameraInfo, info_topic, self.on_info, qos_profile_sensor_data
            ),
        ]

    async def refresh(self):
        with self.lock:
            initial_stamp = self.latest.source_stamp_ns if self.latest else 0
        provider = getattr(self.pose_provider, "__self__", self.pose_provider)
        if hasattr(provider, "refresh"):
            await provider.refresh()
        # Wait for the next frame paired with the refreshed pose, not an old cached frame.
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            with self.lock:
                frame = self.latest
            if (
                frame is not None
                and frame.source_stamp_ns > initial_stamp
                and 0 <= time.monotonic() - frame.pose_received_mono <= 1.0
            ):
                return
            await asyncio.sleep(0.01)
        raise Rejected("camera/localization pairing unavailable")

    def on_info(self, message):
        if (
            message.header.frame_id == self.frame_id
            and len(message.k) == 9
            and all(math.isfinite(v) for v in message.k)
            and message.k[0] > 0
            and message.k[4] > 0
        ):
            self.info = (message.width, message.height, time.monotonic())

    def on_image(self, message):
        now = time.monotonic()
        stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
        age = (self.node.get_clock().now().nanoseconds - stamp) / 1e9
        if (
            message.header.frame_id != self.frame_id
            or message.encoding not in {"rgb8", "bgr8"}
            or not 0 <= age < 1
            or not self.info
            or now - self.info[2] >= 1
            or (message.width, message.height) != self.info[:2]
            or not 16 <= message.width <= 1920
            or not 16 <= message.height <= 1080
            or not message.width * 3 <= message.step <= message.width * 3 + 4096
            or len(message.data) != message.step * message.height
        ):
            return
        pose = self.pose_provider()
        if pose is None:
            return
        data = b"".join(
            bytes(
                message.data[
                    row * message.step : row * message.step + message.width * 3
                ]
            )
            for row in range(message.height)
        )
        if message.encoding == "bgr8":
            rgb = bytearray(data)
            rgb[0::3], rgb[2::3] = data[2::3], data[0::3]
            data = bytes(rgb)
        self.update(
            RGBFrame(
                data,
                message.width,
                message.height,
                time.time() - age,
                now,
                self.frame_id,
                self.clock_domain,
                stamp,
                pose["map_version"],
                self.calibration_version,
                pose["viewpoint"],
                pose["pose"],
                pose["received_mono"],
            )
        )


class ROSPoseSource:
    """Fresh AMCL localization matched to surveyed poses, never a commanded goal.

    Call select_current_view(True) only for a navigation-free describe task, then
    reset it for named-place tasks. This changes the view label, never the pose.
    """

    def __init__(
        self,
        node,
        waypoints,
        *,
        map_version,
        topic="amcl_pose",
        frame_id="map",
        position_tolerance=0.15,
        yaw_tolerance=math.radians(10),
        max_age=1.0,
        max_position_variance=0.04,
        max_yaw_variance=0.03,
        refresh_service="request_nomotion_update",
        clock=None,
    ):
        from geometry_msgs.msg import PoseWithCovarianceStamped
        from rclpy.qos import QoSProfile, ReliabilityPolicy, DurabilityPolicy
        from std_srvs.srv import Empty
        from .contracts import SystemClock

        self.node, self.clock = node, clock or SystemClock()
        self.map_version = label(map_version, "map version")
        self.frame_id = label(frame_id, "map frame")
        if self.frame_id != "map":
            raise Rejected("observation pipeline requires map frame")
        if not isinstance(waypoints, dict) or not 1 <= len(waypoints) <= 32:
            raise Rejected("one to 32 surveyed waypoints required")
        self.waypoints = {}
        for name, pose in waypoints.items():
            label(name, "waypoint")
            if not isinstance(pose, dict) or set(pose) != {"x", "y", "yaw"}:
                raise Rejected("surveyed x/y/yaw pose required")
            self.waypoints[name] = {
                key: finite(value, -100000, 100000) for key, value in pose.items()
            }
        self.position_tolerance = finite(position_tolerance, 0.01, 0.5)
        self.yaw_tolerance = finite(yaw_tolerance, 0.01, math.pi / 4)
        self.max_age = finite(max_age, 0.05, 2.0)
        self.max_position_variance = finite(max_position_variance, 0.00001, 0.25)
        self.max_yaw_variance = finite(max_yaw_variance, 0.00001, 0.25)
        self.lock, self.latest, self.last_stamp = threading.Lock(), None, 0
        self.current_view = False
        qos = QoSProfile(
            depth=1,
            reliability=ReliabilityPolicy.RELIABLE,
            durability=DurabilityPolicy.TRANSIENT_LOCAL,
        )
        self.subscription = node.create_subscription(
            PoseWithCovarianceStamped, topic, self.observe, qos
        )
        self.empty_type = Empty
        self.refresh_client = (
            node.create_client(Empty, refresh_service) if refresh_service else None
        )

    def select_current_view(self, enabled):
        if type(enabled) is not bool:
            raise Rejected("explicit current-view mode required")
        with self.lock:
            self.current_view = enabled

    def observe(self, message):
        now = self.clock.mono()
        stamp = message.header.stamp.sec * 1_000_000_000 + message.header.stamp.nanosec
        age = (self.node.get_clock().now().nanoseconds - stamp) / 1e9
        pose, covariance = message.pose.pose, message.pose.covariance
        q = pose.orientation
        values = [pose.position.x, pose.position.y, q.x, q.y, q.z, q.w, *covariance]
        valid = (
            message.header.frame_id == self.frame_id
            and stamp > 0
            and 0 <= age <= self.max_age
            and len(covariance) == 36
            and all(math.isfinite(value) for value in values)
            and all(
                value >= 0 for value in (covariance[0], covariance[7], covariance[35])
            )
            and max(covariance[0], covariance[7]) <= self.max_position_variance
            and covariance[35] <= self.max_yaw_variance
            and abs(q.x * q.x + q.y * q.y + q.z * q.z + q.w * q.w - 1) <= 0.01
        )
        with self.lock:
            if not valid:
                self.latest = None
                return
            if stamp <= self.last_stamp:
                return
            self.last_stamp = stamp
            self.latest = {
                "pose": {
                    "x": pose.position.x,
                    "y": pose.position.y,
                    "yaw": math.atan2(
                        2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z)
                    ),
                    "frame_id": self.frame_id,
                },
                "received_mono": now - age,
                "map_version": self.map_version,
                "source_stamp_ns": stamp,
            }

    def snapshot(self):
        with self.lock:
            if (
                self.latest is None
                or not 0
                <= self.clock.mono() - self.latest["received_mono"]
                <= self.max_age
            ):
                return None
            result = dict(self.latest, pose=dict(self.latest["pose"]))
            current_view = self.current_view
        pose = result["pose"]
        if current_view:
            return dict(result, viewpoint="current")
        matches = []
        for name, target in self.waypoints.items():
            distance = math.hypot(pose["x"] - target["x"], pose["y"] - target["y"])
            yaw = abs(
                math.atan2(
                    math.sin(pose["yaw"] - target["yaw"]),
                    math.cos(pose["yaw"] - target["yaw"]),
                )
            )
            if distance <= self.position_tolerance and yaw <= self.yaw_tolerance:
                matches.append((distance, name))
        if not matches:
            return None
        matches.sort()
        # Equal-distance aliases must not arbitrarily establish semantic coverage.
        if len(matches) > 1 and abs(matches[0][0] - matches[1][0]) < 1e-6:
            return None
        return dict(result, viewpoint=matches[0][1])

    async def refresh(self):
        """Request a stationary AMCL update if stale; does not command movement."""
        if self.snapshot() is not None:
            return
        if self.refresh_client is None or not self.refresh_client.service_is_ready():
            raise Rejected("fresh localization unavailable")
        future = self.refresh_client.call_async(self.empty_type.Request())
        async with asyncio.timeout(2):
            while not future.done():
                await asyncio.sleep(0.01)
            if future.cancelled() or future.exception() is not None:
                raise Rejected("localization refresh failed")
            while self.snapshot() is None:
                await asyncio.sleep(0.01)

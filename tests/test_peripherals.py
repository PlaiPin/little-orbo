import io
import math
from types import SimpleNamespace as NS
import wave

import pytest

from orbo_runtime.audio_check import levels
from orbo_runtime.camera import CameraHealth, camera_spec
from orbo_runtime.contracts import Rejected
from orbo_runtime.teleop import JoyInput


CALIBRATION = {
    "max_linear_m_s": 0.3,
    "max_angular_rad_s": 2.0,
    "wheel_radius": 0.05,
    "wheel_separation": 0.2,
    "max_wheel_rad_s": 4,
}
MAPPING = {
    "linear_axis": 0,
    "angular_axis": 1,
    "deadman_button": 0,
    "linear_scale": 1.0,
    "angular_scale": 1.0,
}


def test_deadman_combined_wheel_bounds_and_expiring_input():
    joy = JoyInput(MAPPING, CALIBRATION)
    joy.accept(1_000_000_000, [1, 1], [1], 1_000_000_000, 10)
    result = joy.current(1_010_000_000, 10.01)
    assert result.held
    assert (
        max(
            abs(result.linear - result.angular * 0.1),
            abs(result.linear + result.angular * 0.1),
        )
        <= 0.2
    )
    joy.accept(1_020_000_000, [1, 1], [0], 1_020_000_000, 10.02)
    assert joy.current(1_020_000_000, 10.02).linear == 0
    with pytest.raises(Rejected):
        joy.current(1_220_000_000, 10.22)


@pytest.mark.parametrize(
    "stamp,axes,buttons",
    [
        (0, [0, 0], [1]),
        (2_000_000_000, [0, 0], [1]),
        (1_000_000_000, [math.nan, 0], [1]),
        (1_000_000_000, [1.1, 0], [1]),
        (1_000_000_000, [0], [1]),
        (1_000_000_000, [0, 0], [2]),
    ],
)
def test_invalid_teleop_input_clears_previous_command(stamp, axes, buttons):
    joy = JoyInput(MAPPING, CALIBRATION)
    joy.accept(999_000_000, [1, 0], [1], 1_000_000_000, 10)
    with pytest.raises(Rejected):
        joy.accept(stamp, axes, buttons, 1_000_000_000, 10)
    with pytest.raises(Rejected):
        joy.current(1_000_000_000, 10)


def test_camera_opt_in_driver_and_measured_transform(tmp_path):
    config = {
        "enabled": False,
        "driver": "libcamera",
        "camera": "actual-ov5647-name",
        "width": 640,
        "height": 480,
        "frame_id": "camera_optical_frame",
    }
    with pytest.raises(Rejected, match="disabled"):
        camera_spec(config)
    config["enabled"] = True
    assert camera_spec(config)[:2] == ("camera_ros", "camera_node")
    config["mount"] = {"x": 0}
    with pytest.raises(Rejected, match="measured"):
        camera_spec(config)
    config.pop("mount")
    config.update(driver="v4l2", video_device="/dev/v4l/by-id/test")
    assert camera_spec(config)[2]["image_size"] == [640, 480]
    config["calibration_file"] = str(tmp_path / "absent.yaml")
    with pytest.raises(Rejected, match="calibration"):
        camera_spec(config)


def test_camera_diagnostics_never_call_missing_intrinsics_calibrated():
    health = CameraHealth("camera_optical_frame")
    msg = NS(
        header=NS(frame_id="camera_optical_frame", stamp=NS(sec=10, nanosec=0)),
        width=2,
        height=2,
        step=6,
        data=bytes(12),
    )
    assert health.status(20)[0] == 2
    health.observe(msg, 10_010_000_000, 20)
    assert health.status(20)[0] == 1
    info = NS(header=msg.header, width=2, height=2, k=[0.0] * 9)
    health.info(info, 20)
    assert health.status(20)[0] == 1
    info.k[0] = info.k[4] = 100.0
    health.info(info, 20)
    assert health.status(20)[0] == 0
    assert health.status(21)[0] == 2
    health.observe(msg, 10_020_000_000, 21)
    assert health.status(21)[0] == 2  # Replay cannot refresh freshness.


def test_audio_record_check_reports_levels_without_raw_samples():
    buf = io.BytesIO()
    with wave.open(buf, "wb") as writer:
        writer.setnchannels(1)
        writer.setsampwidth(2)
        writer.setframerate(16000)
        writer.writeframes(b"\x00\x00\xff\x7f\x00\x80")
    report = levels(buf.getvalue())
    assert report["frames"] == 3 and report["clipped_samples"] == 2
    assert report["peak_dbfs"] == 0
    assert report["raw_audio_saved"] is False
    assert not any(isinstance(value, bytes) for value in report.values())


async def test_unconfigured_teleop_never_changes_motion_lease(tmp_path):
    from orbo_runtime.cli import LocalService

    service = LocalService(
        {
            "runtime": {
                "profile": "simulated",
                "owner": "test",
                "database": str(tmp_path / "state.db"),
            }
        }
    )
    try:
        with pytest.raises(Rejected, match="not configured"):
            await service.call({"method": "robot.teleop", "args": {}})
        assert (
            service.store.db.execute("SELECT COUNT(*) FROM leases").fetchone()[0] == 0
        )
    finally:
        await service.close()

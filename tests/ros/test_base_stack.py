"""Real controllers, gate and plugin with synthetic encoders; isolated DDS only."""

from contextlib import ExitStack
import importlib.util
import json
import math
import os
from pathlib import Path
import subprocess
import time
from types import SimpleNamespace

import pytest

from process_cleanup import stop_process_group

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit ROS integration lane"
)
ROOT = Path(__file__).resolve().parents[2]
REPEATS = int(os.environ.get("ORBO_CONTROL_REPEATS", "1"))
if not 1 <= REPEATS <= 20:
    raise ValueError("ORBO_CONTROL_REPEATS must be between 1 and 20")


@pytest.fixture
def stack(tmp_path):
    import rclpy
    from synthetic_firmware import SyntheticFirmware

    assert os.environ.get("ROS_DOMAIN_ID") == os.environ.get("ORBO_TEST_DOMAIN", "96")
    assert os.environ.get("ROS_DOMAIN_ID") in {"96", "97", "98", "99"}, (
        "Never test on the board domain"
    )
    spec = importlib.util.spec_from_file_location(
        "base_bench", ROOT / "tools/control-base.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    with ExitStack() as cleanup:
        rclpy.init()
        cleanup.callback(rclpy.try_shutdown)
        state = SyntheticFirmware()
        cleanup.callback(state.close)
        timing = {"owner": []}
        log = cleanup.enter_context((tmp_path / "stack.log").open("w"))
        process = subprocess.Popen(
            [
                "ros2",
                "launch",
                "orbo_bringup",
                "control.launch.py",
                "calibration:="
                + str(ROOT / "ros_ws/src/orbo_description/config/bench-cad.yaml"),
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        cleanup.callback(stop_process_group, process)
        node = rclpy.create_node("synthetic_owner")
        cleanup.callback(node.destroy_node)
        controller = module.BasePublisher(node, 0.033, 0.1018)
        cleanup.callback(controller.close)
        cleanup.callback(controller.stop)
        try:
            controller.connect_and_arm()
            yield SimpleNamespace(
                controller=controller, state=state, module=module, timing=timing
            )
        finally:
            timing.update(state.request("timing"))
            (tmp_path / "timing.json").write_text(json.dumps(timing))


def drive(stack, seconds, velocity):
    start = time.monotonic()
    while time.monotonic() - start < seconds:
        stack.timing["owner"].append([time.monotonic(), "begin"])
        stack.controller.publish(velocity)
        stack.timing["owner"].append([time.monotonic(), "published"])
        stack.controller.spin(0.05)


def yaw(msg):
    q = msg.pose.pose.orientation
    return math.atan2(2 * (q.w * q.z + q.x * q.y), 1 - 2 * (q.y * q.y + q.z * q.z))


@pytest.mark.parametrize("run", range(REPEATS))
def test_30_second_odometry_uses_feedback_not_command(stack, run):
    drive(stack, 30, [0.6, 0.6])
    assert stack.controller.stop()
    a, b = stack.controller.samples[0], stack.controller.samples[-1]
    dx = b[1].pose.pose.position.x - a[1].pose.pose.position.x
    expected = 0.033 * (b[2][0] - a[2][0])
    assert abs(dx - expected) < 0.002
    assert 0.36 < dx < 0.42  # Requested-speed integration would be ~0.594 m.
    assert abs(a[1].pose.pose.position.x) < 0.001
    assert (
        abs(yaw(a[1])) < 0.001
    )  # Different absolute counts must not create initial yaw.
    received = stack.state["received"]
    assert len(received) > 500
    times = [t for t, _ in received]
    assert 15 < (len(times) - 1) / (times[-1] - times[0]) < 25
    # DDS may retransmit packets, but the plugin must not spend a new command
    # sequence/rate-limit slot republishing the same source timestamp.
    stamps = [message.issued_epoch_ms for _, message in received]
    assert all(b > a for a, b in zip(stamps, stamps[1:]))
    assert stack.state["arms"] == 1
    epoch = stack.state["epoch"]
    for _, msg in received:
        assert msg.boot_id == 42 and msg.epoch == epoch
        assert 0 < msg.valid_for_ms <= 200
    assert max(m.velocity_rad_s[0] for _, m in received) > 0.5


@pytest.mark.parametrize(
    "measured,requested", [([-0.4, -0.4], [-0.6, -0.6]), ([-0.4, 0.4], [-0.6, 0.6])]
)
def test_reverse_and_turn_odometry(stack, measured, requested):
    stack.state["measured_velocity"] = measured
    drive(stack, 3, requested)
    assert stack.controller.stop()
    a, b = stack.controller.samples[0], stack.controller.samples[-1]
    delta = [b[2][i] - a[2][i] for i in range(2)]
    expected_distance = 0.033 * sum(delta) / 2
    expected_yaw = 0.033 * (delta[1] - delta[0]) / 0.1018
    assert (
        abs(b[1].pose.pose.position.x - a[1].pose.pose.position.x - expected_distance)
        < 0.002
    )
    assert abs(math.remainder(yaw(b[1]) - yaw(a[1]) - expected_yaw, 2 * math.pi)) < 0.02


def test_source_loss_stops_refresh_and_does_not_rearm(stack):
    drive(stack, 1.3, [0.6, 0.6])
    lost_at = time.monotonic()
    stack.controller.spin(0.7)
    assert not stack.state["armed"]
    assert stack.state["received"][-1][0] < lost_at + 0.3
    count = len(stack.state["received"])
    with pytest.raises(RuntimeError, match="disarmed"):
        stack.controller.publish([0.6, 0.6])
    stack.controller.spin(0.3)
    assert len(stack.state["received"]) == count
    assert stack.state["arms"] == 1


@pytest.mark.parametrize("failure", ["feedback_loss", "reboot"])
def test_feedback_loss_and_reboot_stop_hardware_without_rearm(stack, failure):
    drive(stack, 1.3, [0.6, 0.6])
    stops = stack.state["stops"]
    if failure == "feedback_loss":
        stack.state["feedback"] = False
    else:
        stack.state.update(
            boot=43, epoch=0, sequence=0, armed=False, position=[0.0, 0.0]
        )
    stack.controller.spin(0.5)
    assert stack.state["stops"] > stops
    assert not stack.state["armed"]
    count = len(stack.state["received"])
    stack.state["feedback"] = True
    stack.controller.spin(0.3)
    assert len(stack.state["received"]) == count
    assert stack.state["arms"] == 1
    with pytest.raises(RuntimeError):
        stack.controller.publish([0.6, 0.6])

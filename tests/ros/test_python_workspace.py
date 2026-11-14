"""Installed ROS executables, launched away from the checkout; no physical I/O."""

from contextlib import ExitStack
import json
import os
from pathlib import Path
import subprocess
import tempfile

import pytest

from process_cleanup import stop_process_group

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit ROS integration lane"
)


@pytest.fixture(autouse=True)
def isolated_domain():
    assert os.environ.get("ROS_DOMAIN_ID") == "96", "Never test on the board domain"


def run(tmp_path, *args):
    result = subprocess.run(
        ["ros2", *args], cwd=tmp_path, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout


def test_python_executables_are_installed_and_resolve_packaged_modules(tmp_path):
    from ament_index_python.packages import get_package_prefix
    import orbo_runtime

    prefix = Path(get_package_prefix("orbo_runtime")).resolve()
    # This lane uses a copy install: imports must not fall back to editable source.
    assert Path(orbo_runtime.__file__).resolve().is_relative_to(prefix)
    names = {line.split()[1] for line in run(tmp_path, "pkg", "executables", "orbo_runtime").splitlines()}
    assert {"runtime", "audio_check", "base_bench", "wheel_bench", "flipper_bench",
            "teleop_keyboard", "velocity_gate", "camera_monitor", "installed_smoke"} <= names
    for name in ("base_bench", "wheel_bench", "flipper_bench", "audio_check", "runtime"):
        output = run(tmp_path, "run", "orbo_runtime", name, "--help", "--ros-args", "-r", "__node:=packaging_probe")
        assert "usage:" in output
    # Non-hardware CLI validates dependency visibility through the installed shebang.
    output = run(tmp_path, "run", "orbo_runtime", "runtime", "demo", "--database", str(tmp_path / "demo.db"))
    assert json.loads(output)["hardware_acceptance"] is False
    assert "Graceful shutdown removed owner socket" in run(
        tmp_path, "run", "orbo_runtime", "installed_smoke"
    )


def test_packaged_bench_launch_and_executable_keep_arm_stop_and_odometry(tmp_path):
    from synthetic_firmware import SyntheticFirmware

    with ExitStack() as cleanup:
        state = SyntheticFirmware()
        cleanup.callback(state.close)
        log = cleanup.enter_context((tmp_path / "bench.log").open("w"))
        launch = subprocess.Popen(
            ["ros2", "launch", "orbo_bringup", "bench.launch.py"],
            cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        cleanup.callback(stop_process_group, launch)
        output = run(tmp_path, "run", "orbo_runtime", "base_bench", "drive", "--linear", "0.02", "--seconds", "3")
        assert "Drive completed:" in output
        assert "Measured odometry:" in output
        assert "Stopped and disarmed" in output
        assert state["arms"] == 1
        assert not state["armed"]
        assert state["received"]


@pytest.fixture
def short_socket():
    # macOS sockaddr_un has a shorter path limit than pytest's nested tmp_path.
    with tempfile.TemporaryDirectory(prefix="orbo-ros-", dir="/tmp") as directory:
        yield Path(directory) / "owner.sock"


async def test_runtime_launch_handles_ros_arguments_and_owner_ipc(tmp_path, short_socket):
    import asyncio
    from orbo_runtime.cli import request

    endpoint = short_socket
    config = tmp_path / "runtime.toml"
    config.write_text(
        '[runtime]\nowner="test"\nprofile="orbo"\n'
        f'database={json.dumps(str(tmp_path / "journal.db"))}\n'
        f'socket={json.dumps(str(endpoint))}\n'
        '[robot]\nid="orbo"\n[near]\nenabled=false\n[voice]\nenabled=false\n'
    )
    with (tmp_path / "runtime.log").open("w") as log:
        process = subprocess.Popen(
            ["ros2", "launch", "orbo_bringup", "runtime.launch.py", f"config:={config}"],
            cwd=tmp_path, stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
        )
        try:
            async with asyncio.timeout(15):
                while not endpoint.exists():
                    assert process.poll() is None, (tmp_path / "runtime.log").read_text()
                    await asyncio.sleep(0.05)
            assert endpoint.stat().st_mode & 0o777 == 0o600
            result = await request(endpoint, "episode.start", {})
            assert result["ok"], result
            stopped = await request(endpoint, "stop", {})
            assert stopped["ok"], stopped
        finally:
            stop_process_group(process)
        assert not endpoint.exists(), (tmp_path / "runtime.log").read_text()

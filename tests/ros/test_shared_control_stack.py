"""One real runtime owner, ros2_control and DDS; synthetic MCU only (domain 96)."""

import asyncio
from contextlib import ExitStack
import os
from pathlib import Path
import subprocess
import tempfile
import time
from types import SimpleNamespace as NS

import pytest
import yaml

from process_cleanup import stop_process_group

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit ROS integration lane"
)
ROOT = Path(__file__).resolve().parents[2]


async def wait_for(predicate, timeout=10):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


@pytest.fixture
async def shared(tmp_path):
    assert os.environ.get("ROS_DOMAIN_ID") == "96", "Never test on the board domain"
    from sensor_msgs.msg import Joy
    from orbo_runtime.cli import LocalService, request
    from synthetic_firmware import SyntheticFirmware

    profile = yaml.safe_load(
        (ROOT / "ros_ws/src/orbo_description/config/bench-cad.yaml").read_text()
    )
    profile.update(
        bench_only=False, wheels_qualified=True, evidence_record="SYNTHETIC TEST ONLY"
    )
    calibration = tmp_path / "synthetic.yaml"
    calibration.write_text(yaml.safe_dump(profile))
    with ExitStack() as cleanup:
        state = SyntheticFirmware()
        cleanup.callback(state.close)
        log = cleanup.enter_context((tmp_path / "control.log").open("w"))
        for command in (
            [
                "ros2",
                "launch",
                "orbo_bringup",
                "control.launch.py",
                "calibration:=" + str(calibration),
            ],
            [
                "ros2",
                "run",
                "orbo_runtime",
                "velocity_gate",
                "--ros-args",
                "-p",
                "source:=teleop",
                "-p",
                "max_linear:=0.0396",
                "-p",
                "max_angular:=0.778",
            ],
        ):
            process = subprocess.Popen(
                command, stdout=log, stderr=subprocess.STDOUT, start_new_session=True
            )
            cleanup.callback(stop_process_group, process)
        service = LocalService(
            {
                "runtime": {
                    "profile": "orbo",
                    "owner": "test",
                    "database": str(tmp_path / "state.db"),
                },
                "robot": {
                    "id": "orbo",
                    "calibration": str(calibration),
                    "teleop_enabled": True,
                    "flippers_qualified": True,
                    "attention_pulse_us": [1450, 1550],
                    "teleop": {
                        "qualified": True,
                        "evidence_record": "SYNTHETIC TEST ONLY",
                        "linear_axis": 0,
                        "angular_axis": 1,
                        "deadman_button": 0,
                        "linear_scale": 0.5,
                        "angular_scale": 0.5,
                    },
                },
            }
        )
        adapter = service.broker.adapter
        input_state = {"send": True, "held": False}
        joy = adapter.node.create_publisher(Joy, "teleop/joy", 1)

        def publish():
            if input_state["send"]:
                msg = Joy()
                stamp = time.time_ns() - 20_000_000
                msg.header.stamp.sec, msg.header.stamp.nanosec = divmod(stamp, 10**9)
                msg.axes = [1.0, 0.0]
                msg.buttons = [int(input_state["held"])]
                joy.publish(msg)

        timer = adapter.node.create_timer(0.02, publish)
        socket_dir = cleanup.enter_context(
            tempfile.TemporaryDirectory(prefix="orbo-shared-", dir="/tmp")
        )
        socket = str(Path(socket_dir) / "owner.sock")
        server = await asyncio.start_unix_server(service.handle, socket)
        tasks = []

        async def call(method, **args):
            return await request(socket, method, args)

        async def controller_command(*args):
            result = await asyncio.to_thread(
                subprocess.run,
                ["ros2", "control", *args, "-c", "/orbo/controller_manager"],
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0, (
                result.stdout + result.stderr + (tmp_path / "control.log").read_text()
            )

        try:
            await wait_for(
                lambda: adapter.health is not None
                and adapter.teleoperation.input.last is not None
            )
            await controller_command(
                "set_hardware_component_state", "OrboWheels", "active"
            )
            await controller_command(
                "switch_controllers", "--activate", "drive_controller", "--strict"
            )
            episode = service.store.episode("test", provenance="synthetic")
            service.store.grant("test", "robot.command", "orbo", True, time.time() + 60)

            async def drive(duration=20):
                task = asyncio.create_task(
                    call(
                        "robot.teleop",
                        episode_id=episode,
                        key="drive",
                        duration=duration,
                    )
                )
                tasks.append(task)
                await wait_for(lambda: adapter.owned_session is not None or task.done())
                if task.done():
                    pytest.fail(str(await task))
                input_state["held"] = True
                await wait_for(
                    lambda: any(
                        abs(m.velocity_rad_s[0]) > 0.1 for _, m in state["received"]
                    )
                )
                return task

            async def gesture(key="gesture", duration=1):
                return await call(
                    "robot.gesture", episode_id=episode, key=key, duration=duration
                )

            yield NS(
                service=service,
                adapter=adapter,
                state=state,
                input=input_state,
                call=call,
                drive=drive,
                gesture=gesture,
                tasks=tasks,
                episode=episode,
            )
        finally:
            await service.broker.stop("test cleanup")
            await asyncio.gather(*tasks, return_exceptions=True)
            server.close()
            await server.wait_closed()
            adapter.node.destroy_timer(timer)
            await service.close()


async def test_gestures_share_one_arm_while_real_wheel_commands_continue(shared):
    drive = await shared.drive()
    epoch = shared.state["epoch"]
    lease_deadline = shared.service.broker.lease_deadline
    before = len(shared.state["received"])
    gesture = asyncio.create_task(shared.gesture(duration=1.5))
    shared.tasks.append(gesture)
    await wait_for(lambda: shared.state["flipper_active"])
    duplicate = await shared.gesture(duration=1.5)
    assert duplicate["ok"]  # Idempotent retry does not republish a gesture.
    busy = await shared.gesture(key="overlap")
    assert not busy["ok"] and "resource busy" in busy["error"]
    other_drive = await shared.call(
        "robot.teleop", episode_id=shared.episode, key="competing", duration=5
    )
    assert not other_drive["ok"] and "robot action active" in other_drive["error"]
    result = await gesture
    assert result["ok"] and result["result"]["state"] == "completed", result
    assert not drive.done() and shared.state["armed"]
    assert shared.state["arms"] == 1 and shared.state["epoch"] == epoch
    assert shared.service.broker.lease_deadline == lease_deadline
    commands = shared.state["received"][before:]
    assert len(commands) >= 15
    assert all(msg.epoch == epoch and msg.boot_id == 42 for _, msg in commands)
    assert max(b[0] - a[0] for a, b in zip(commands, commands[1:])) < 0.2
    flippers = shared.state["flipper_commands"]
    assert len(flippers) == 1 and flippers[0][1].epoch == epoch
    assert (await shared.gesture(key="second"))["result"]["state"] == "completed"
    assert shared.state["arms"] == 1 and not drive.done()
    await shared.call("stop")
    await drive
    await wait_for(lambda: not shared.state["armed"])
    assert not shared.state["flipper_active"]


@pytest.mark.parametrize(
    "failure", ["stop", "input_loss", "reboot", "fault", "lease_expiry"]
)
async def test_stop_and_failures_end_both_streams_without_rearm(shared, failure):
    drive = await shared.drive()
    gesture = asyncio.create_task(shared.gesture(duration=4))
    shared.tasks.append(gesture)
    await wait_for(lambda: shared.state["flipper_active"])
    if failure == "stop":
        await shared.call("stop")
    elif failure == "input_loss":
        shared.input["send"] = False
    elif failure == "reboot":
        shared.state.update(boot=43, epoch=0, sequence=0, armed=False)
    elif failure == "fault":
        shared.state.update(armed=False, fault=6)
    else:
        shared.service.broker.lease_deadline = time.monotonic() - 1
    results = await asyncio.wait_for(asyncio.gather(drive, gesture), timeout=3)
    assert all(not result["ok"] for result in results), results
    await wait_for(
        lambda: not shared.state["armed"] and not shared.state["flipper_active"]
    )
    count = len(shared.state["received"])
    await asyncio.sleep(0.3)
    assert len(shared.state["received"]) == count
    assert shared.state["arms"] == 1 and not shared.state["flipper_finishes"]


async def test_rejected_flipper_target_does_not_rearm_or_interrupt_wheels(shared):
    drive = await shared.drive()
    shared.state["reject_flippers"] = True
    result = await shared.gesture()
    assert result["ok"] and result["result"]["state"] == "rejected"
    assert not drive.done() and shared.state["armed"] and shared.state["arms"] == 1
    shared.state["reject_flippers"] = False
    assert (await shared.gesture(key="valid"))["result"]["state"] == "completed"


async def test_driving_budget_ends_an_unfinished_gesture(shared):
    drive = await shared.drive(duration=3)
    gesture = asyncio.create_task(shared.gesture(duration=4))
    shared.tasks.append(gesture)
    await wait_for(lambda: shared.state["flipper_active"])
    wheel_result, flipper_result = await asyncio.wait_for(
        asyncio.gather(drive, gesture), 5
    )
    assert wheel_result["ok"] and wheel_result["result"]["state"] == "completed"
    assert not flipper_result["ok"]
    assert not shared.state["armed"] and not shared.state["flipper_active"]
    assert shared.state["arms"] == 1 and not shared.state["flipper_finishes"]

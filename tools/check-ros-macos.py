#!/usr/bin/env python3
"""Check native Jazzy and exchange messages locally, without robot access."""

import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import tempfile
import time


def main():
    if os.environ.get("ROS_DISTRO") != "jazzy":
        raise RuntimeError("Run through the Jazzy environment: pixi run ros-check")
    import rclpy

    print(f"ROS_DISTRO={os.environ['ROS_DISTRO']}", flush=True)
    print(f"Host: {platform.system()} {platform.mac_ver()[0]} {platform.machine()}")
    print(f"Python: {sys.version.split()[0]} ({sys.executable})")
    print(f"rclpy: {rclpy.__file__}", flush=True)
    subprocess.run(
        ["ros2", "--help"], check=True, stdout=subprocess.DEVNULL, timeout=60
    )

    with tempfile.TemporaryDirectory(prefix="orbo-ros-check-") as directory:
        root = Path(directory)
        env = {
            **os.environ,
            "ROS_DOMAIN_ID": "93",
            "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
            "ROS_STATIC_PEERS": "",
            "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
            "ROS_LOG_DIR": str(root / "log"),
            "PYTHONUNBUFFERED": "1",
        }
        # External DDS configuration must not override this local-only check.
        for name in (
            "FASTRTPS_DEFAULT_PROFILES_FILE",
            "FASTDDS_DEFAULT_PROFILES_FILE",
            "ROS_DISCOVERY_SERVER",
            "ROS_SUPER_CLIENT",
            "ROS_LOCALHOST_ONLY",
        ):
            env.pop(name, None)
        env["SKIP_DEFAULT_XML"] = "1"
        topic = f"/orbo_setup_check_{os.getpid()}"
        processes = []
        logs = []
        try:
            for package, executable in (
                ("demo_nodes_py", "listener"),
                ("demo_nodes_cpp", "talker"),
            ):
                log = (root / f"{executable}.log").open("w")
                logs.append(log)
                processes.append(
                    subprocess.Popen(
                        [
                            "ros2",
                            "run",
                            package,
                            executable,
                            "--ros-args",
                            "-r",
                            f"chatter:={topic}",
                        ],
                        env=env,
                        stdout=log,
                        stderr=subprocess.STDOUT,
                        start_new_session=True,
                    )
                )
            # First launches on macOS can spend tens of seconds loading libraries.
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                if any(process.poll() is not None for process in processes):
                    raise RuntimeError("A ROS demo process exited before verification")
                received = (root / "listener.log").read_text()
                if "I heard:" in received and "Hello World:" in received:
                    print(
                        "PASS: C++ talker -> Python listener over localhost (domain 93)."
                    )
                    print(
                        "No robot, motion, camera, microphone, or cloud service was used."
                    )
                    return
                time.sleep(0.1)
            raise RuntimeError("No talker/listener exchange within 120 seconds")
        except BaseException:
            for name in ("listener", "talker"):
                path = root / f"{name}.log"
                if path.exists():
                    print(f"{name} output:\n{path.read_text()}", file=sys.stderr)
            raise
        finally:
            for process in processes:
                try:
                    os.killpg(process.pid, signal.SIGINT)
                except ProcessLookupError:
                    pass
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait(timeout=5)
            for log in logs:
                log.close()


if __name__ == "__main__":
    main()

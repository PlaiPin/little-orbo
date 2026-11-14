#!/usr/bin/env python3
"""Fetch exact source commits; never overwrite an existing different checkout."""

import argparse
import json
from pathlib import Path
import shutil
import subprocess

ROOT = Path(__file__).resolve().parents[1]
LOCK = json.loads((ROOT / "firmware/orbo/dependencies.source-lock.json").read_text())


def run(*args, cwd=None):
    subprocess.run(args, cwd=cwd, check=True)


def checkout(path, url, revision):
    if not path.exists():
        path.mkdir(parents=True)
        run("git", "init", "-q", str(path))
        run("git", "remote", "add", "origin", url, cwd=path)
        run("git", "fetch", "--depth=1", "origin", revision, cwd=path)
        run("git", "checkout", "--detach", "FETCH_HEAD", cwd=path)
    actual = subprocess.check_output(
        ["git", "-C", str(path), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != revision:
        raise SystemExit(f"Source revision mismatch: {path}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sdk-dir", type=Path, default=ROOT / ".sdk")
    args = parser.parse_args()
    component = args.sdk_dir.resolve() / "micro_ros_espidf_component"
    checkout(
        component,
        "https://github.com/micro-ROS/micro_ros_espidf_component.git",
        LOCK["micro_ros_espidf_component"]["commit"],
    )
    for patch in sorted((ROOT / "firmware/orbo/patches").glob("*.patch")):
        check = subprocess.run(
            ["git", "apply", "--reverse", "--check", str(patch)],
            cwd=component,
            capture_output=True,
        )
        if check.returncode:
            run("git", "apply", "--check", str(patch), cwd=component)
            run("git", "apply", str(patch), cwd=component)
    for repo in LOCK["repositories"]:
        checkout(component / repo["path"], repo["url"], repo["commit"])
    src = component / "micro_ros_src/src"
    for package in [
        "rosidl/rosidl_typesupport_introspection_cpp",
        "rcl_logging/rcl_logging_log4cxx",
        "rcl_logging/rcl_logging_spdlog",
        "rclc/rclc_examples",
        "rcl/rcl_yaml_param_parser",
        "ros2_tracing/test_tracetools",
        "ros2_tracing/lttngpy",
    ]:
        if (src / package).is_dir():
            (src / package / "COLCON_IGNORE").touch()
    shutil.copytree(
        ROOT / "ros_ws/src/orbo_embedded_interfaces",
        src / "extra_packages/orbo_embedded_interfaces",
        dirs_exist_ok=True,
    )
    (component / ".orbo-sources-locked").write_text(json.dumps(LOCK, sort_keys=True))
    print(
        "Pinned micro-ROS sources ready. Install ESP-IDF version in the lock separately, then run tools/build-firmware.sh."
    )


if __name__ == "__main__":
    main()

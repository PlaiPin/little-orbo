#!/usr/bin/env python3
"""Give native RViz a macOS app identity and launch a preview or live viewer."""

import os
import argparse
from pathlib import Path
import plistlib
import shutil
import tempfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', help='View the running domain-42 control stack')
    parser.add_argument('--internal', action='store_true', help='Preview with outer shells hidden')
    parser.add_argument('--pi', action='store_true', help='Combined remote Pi viewer using subnet discovery')
    parser.add_argument('launch_args', nargs='*', help='ROS launch arguments for --pi, e.g. domain_id:=42')
    args = parser.parse_args()
    if sum((args.live, args.internal, args.pi)) > 1:
        parser.error('--live, --internal and --pi are mutually exclusive')
    if args.launch_args and not args.pi:
        parser.error('ROS launch arguments require --pi')
    root = Path(__file__).resolve().parents[1]
    prefix = Path(os.environ["CONDA_PREFIX"])
    contents = root / ".pixi/Orbo RViz.app/Contents"
    binary = contents / "MacOS/rviz2"
    binary.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement also permits refreshing the bundle after a Pixi update.
    with tempfile.NamedTemporaryFile(dir=binary.parent, delete=False) as temporary:
        replacement = Path(temporary.name)
    try:
        shutil.copy2(prefix / "bin/rviz2", replacement)
        replacement.replace(binary)
    finally:
        replacement.unlink(missing_ok=True)
    # Preserve the executable's original @loader_path/../{lib,opt} lookups.
    for name in ("lib", "opt", "plugins"):
        link = contents / name
        if not link.exists():
            link.symlink_to(prefix / name, target_is_directory=True)
    with (contents / "Info.plist").open("wb") as stream:
        plistlib.dump(
            {
                "CFBundleIdentifier": "org.little-orbo.rviz-preview",
                "CFBundleName": "Orbo RViz",
                "CFBundleExecutable": "rviz2",
                "CFBundlePackageType": "APPL",
                "CFBundleVersion": "1",
                "NSHighResolutionCapable": True,
            },
            stream,
        )
    if args.pi:
        # Let the ROS launch file own domain/discovery settings; preserve static
        # peers and DDS configuration for the operator's network.
        os.execvpe(
            "ros2",
            ["ros2", "launch", "orbo_description", "rviz.launch.py",
             f"rviz_executable:={binary}", *args.launch_args],
            dict(os.environ),
        )
    env = {
        **os.environ,
        "ROS_DOMAIN_ID": "42" if args.live else "94",
        "ROS_AUTOMATIC_DISCOVERY_RANGE": "LOCALHOST",
        "ROS_STATIC_PEERS": "",
        "RMW_IMPLEMENTATION": "rmw_fastrtps_cpp",
        "SKIP_DEFAULT_XML": "1",
    }
    for name in (
        "ROS_DISCOVERY_SERVER",
        "ROS_SUPER_CLIENT",
        "ROS_LOCALHOST_ONLY",
        "FASTRTPS_DEFAULT_PROFILES_FILE",
        "FASTDDS_DEFAULT_PROFILES_FILE",
    ):
        env.pop(name, None)
    if args.live:
        os.execvpe(str(binary), [str(binary), '-d', str(root/'ros_ws/src/orbo_description/config/orbo-live.rviz')], env)
    os.execvpe(
        "ros2",
        [
            "ros2",
            "launch",
            "orbo_description",
            "preview.launch.py",
            f"rviz_executable:={binary}",
            'view:=' + ('internal' if args.internal else 'full'),
        ],
        env,
    )


if __name__ == "__main__":
    main()

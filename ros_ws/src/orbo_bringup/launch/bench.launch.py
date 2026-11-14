"""Lifted-base stack only; the separate base_bench executable owns each arm."""

from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import IncludeLaunchDescription
from launch.launch_description_sources import PythonLaunchDescriptionSource


def generate_launch_description():
    bringup = Path(get_package_share_directory("orbo_bringup"))
    description = Path(get_package_share_directory("orbo_description"))
    return LaunchDescription([
        IncludeLaunchDescription(
            PythonLaunchDescriptionSource(str(bringup / "launch/control.launch.py")),
            launch_arguments={
                "calibration": str(description / "config/bench-cad.yaml"),
            }.items(),
        ),
    ])

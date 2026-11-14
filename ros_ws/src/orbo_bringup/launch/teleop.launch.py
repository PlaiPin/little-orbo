from pathlib import Path
import yaml
from ament_index_python.packages import get_package_share_directory
from launch_ros.actions import Node
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from orbo_runtime.commissioning import validate_calibration


def setup(context):
    calibration = validate_calibration(
        yaml.safe_load(
            Path(LaunchConfiguration("calibration").perform(context)).read_text()
        )
    )
    source = LaunchConfiguration("input").perform(context)
    if source not in {"keyboard", "joystick", "remote"}:
        raise RuntimeError("input must be keyboard, joystick or remote")
    actions = [
        Node(
            package="orbo_runtime", executable="velocity_gate", output="screen",
            parameters=[{"source": "teleop", "max_linear": calibration["max_linear_m_s"],
                         "max_angular": calibration["max_angular_rad_s"]}],
        ),
    ]
    if source == "joystick":
        name = LaunchConfiguration("device_name").perform(context)
        if not name:
            raise RuntimeError("select the actual joystick device_name")
        actions.append(
            IncludeLaunchDescription(
                PythonLaunchDescriptionSource(
                    str(
                        Path(get_package_share_directory("orbo_bringup"))
                        / "launch/joy.launch.py"
                    )
                ),
                launch_arguments={
                    "device_name": name,
                    "driver": LaunchConfiguration("driver").perform(context),
                }.items(),
            )
        )
    # Keyboard runs separately in a real owner terminal, never an unattended service.
    return actions


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "calibration", default_value="/etc/orbo/calibration.yaml"
            ),
            DeclareLaunchArgument("input", default_value="keyboard"),
            DeclareLaunchArgument("device_name", default_value=""),
            DeclareLaunchArgument("driver", default_value="joy_node"),
            OpaqueFunction(function=setup),
        ]
    )

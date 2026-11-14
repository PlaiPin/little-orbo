from pathlib import Path
import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, ExecuteProcess, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from orbo_runtime.commissioning import validate_calibration


def setup(context):
    calibration = validate_calibration(
        yaml.safe_load(
            Path(LaunchConfiguration("calibration").perform(context)).read_text()
        )
    )
    source = LaunchConfiguration("input").perform(context)
    if source not in {"keyboard", "joystick"}:
        raise RuntimeError("input must be keyboard or joystick")
    actions = [
        ExecuteProcess(
            cmd=[
                "orbo-velocity-gate",
                "--ros-args",
                "-p",
                "source:=teleop",
                "-p",
                "max_linear:=" + str(calibration["max_linear_m_s"]),
                "-p",
                "max_angular:=" + str(calibration["max_angular_rad_s"]),
            ]
        )
    ]
    if source == "joystick":
        name = LaunchConfiguration("device_name").perform(context)
        if not name:
            raise RuntimeError("select the actual joystick device_name")
        actions.append(
            Node(
                package="joy",
                executable="joy_node",
                namespace="/orbo/teleop",
                parameters=[
                    {
                        "device_name": name,
                        "autorepeat_rate": 50.0,
                        "sticky_buttons": False,
                        "deadzone": 0.1,
                    }
                ],
                output="screen",
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
            OpaqueFunction(function=setup),
        ]
    )

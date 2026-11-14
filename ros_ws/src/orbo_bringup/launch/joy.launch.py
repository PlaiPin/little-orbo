"""Joystick input only: run on the computer with the USB/Bluetooth controller."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def setup(context):
    name = LaunchConfiguration("device_name").perform(context).strip()
    driver = LaunchConfiguration("driver").perform(context)
    if not name:
        raise RuntimeError(
            "select the actual joystick device_name using joy_enumerate_devices"
        )
    if driver not in {"joy_node", "game_controller_node"}:
        raise RuntimeError("driver must be joy_node or game_controller_node")
    return [
        Node(
            package="joy",
            executable=driver,
            name="orbo_joy",
            namespace="/orbo/teleop",
            parameters=[
                {
                    "device_name": name,
                    "autorepeat_rate": 50.0,
                    "sticky_buttons": False,
                    "deadzone": 0.1,
                    "coalesce_interval_ms": 1,
                    "use_sim_time": False,
                }
            ],
            output="screen",
        )
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "device_name",
                default_value="",
                description="Exact name from ros2 run joy joy_enumerate_devices",
            ),
            DeclareLaunchArgument(
                "driver",
                default_value="joy_node",
                description="joy_node or SDL-mapped game_controller_node",
            ),
            OpaqueFunction(function=setup),
        ]
    )

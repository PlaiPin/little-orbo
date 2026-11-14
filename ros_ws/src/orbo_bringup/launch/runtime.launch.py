"""Start the application owner; this launch grants no motion permissions."""

from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("config", default_value="/etc/orbo/runtime.toml"),
        Node(
            package="orbo_runtime", executable="runtime", output="screen",
            arguments=["serve", "--config", LaunchConfiguration("config")],
        ),
    ])

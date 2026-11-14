"""View an existing Orbo ROS graph; no camera, control, or robot publishers start."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, UnsetEnvironmentVariable
from launch.substitutions import EnvironmentVariable, LaunchConfiguration
from launch_ros.actions import Node


def generate_launch_description():
    config = Path(get_package_share_directory("orbo_description")) / "config/orbo-pi.rviz"
    return LaunchDescription([
        DeclareLaunchArgument(
            "domain_id", default_value=EnvironmentVariable("ROS_DOMAIN_ID", default_value="42"),
            description="ROS domain shared with the Pi",
        ),
        DeclareLaunchArgument(
            "discovery_range", default_value="SUBNET", choices=["SUBNET", "LOCALHOST"],
            description="SUBNET for the Pi; LOCALHOST for an isolated local check",
        ),
        DeclareLaunchArgument(
            "camera_topic", default_value="/orbo/camera/image_raw",
            description="Use /camera/image_raw for the standalone Pi camera script",
        ),
        DeclareLaunchArgument("fixed_frame", default_value="odom"),
        DeclareLaunchArgument("rviz_executable", default_value="rviz2"),
        # The deprecated setting would otherwise override subnet discovery.
        UnsetEnvironmentVariable("ROS_LOCALHOST_ONLY"),
        Node(
            package="rviz2",
            executable=LaunchConfiguration("rviz_executable"),
            name="orbo_rviz",
            arguments=["-d", str(config), "-f", LaunchConfiguration("fixed_frame")],
            remappings=[("/orbo/camera/image_raw", LaunchConfiguration("camera_topic"))],
            additional_env={
                "ROS_DOMAIN_ID": LaunchConfiguration("domain_id"),
                "ROS_AUTOMATIC_DISCOVERY_RANGE": LaunchConfiguration("discovery_range"),
            },
            output="screen",
        ),
    ])

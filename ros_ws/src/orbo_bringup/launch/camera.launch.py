from pathlib import Path
import yaml
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from orbo_runtime.camera import camera_spec


def setup(context):
    config = yaml.safe_load(
        Path(LaunchConfiguration("config").perform(context)).read_text()
    )
    package, executable, params, remaps = camera_spec(config)
    actions = [
        Node(
            package=package,
            executable=executable,
            namespace="/orbo",
            name="camera",
            parameters=[params],
            remappings=remaps,
            output="screen",
        ),
        Node(
            package="orbo_runtime", executable="camera_monitor", output="screen",
            parameters=[{"frame_id": config["frame_id"]}],
        ),
    ]
    if config.get("mount"):
        mount = config["mount"]
        arguments = ["--frame-id", "base_link", "--child-frame-id", config["frame_id"]]
        for key in ("x", "y", "z", "roll", "pitch", "yaw"):
            arguments += ["--" + key, str(mount[key])]
        actions.append(
            Node(
                package="tf2_ros",
                executable="static_transform_publisher",
                namespace="/orbo",
                arguments=arguments,
            )
        )
    return actions


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument("config", default_value="/etc/orbo/camera.yaml"),
            OpaqueFunction(function=setup),
        ]
    )

"""Display the repository's exported CAD meshes, without hardware controllers."""

from pathlib import Path

from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, RegisterEventHandler, EmitEvent, OpaqueFunction
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def setup(context):
    share = Path(get_package_share_directory("orbo_description"))
    view = LaunchConfiguration('view').perform(context)
    if view not in ('full', 'internal'):
        raise ValueError('view must be full or internal')
    filename = 'orbo-preview-internal.urdf' if view == 'internal' else 'orbo-preview.urdf'
    publisher = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        parameters=[
            {"robot_description": (share / "config" / filename).read_text()}
        ],
        output="screen",
    )
    rviz = Node(
        package="rviz2",
        executable=LaunchConfiguration("rviz_executable"),
        arguments=["-d", str(share / "config/orbo-preview.rviz")],
        output="screen",
    )
    return [
            RegisterEventHandler(
                OnProcessExit(
                    target_action=rviz,
                    on_exit=[EmitEvent(event=Shutdown(reason="RViz closed"))],
                )
            ),
            publisher,
            rviz,
        ]


def generate_launch_description():
    return LaunchDescription([
        DeclareLaunchArgument("rviz_executable", default_value="rviz2"),
        DeclareLaunchArgument("view", default_value="full"),
        OpaqueFunction(function=setup),
    ])

"""Gazebo-only Orbo; never loads the physical hardware plugin or robot runtime."""

import os
import shutil
from pathlib import Path

import xacro
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    ExecuteProcess,
    OpaqueFunction,
    RegisterEventHandler,
    EmitEvent,
)
from launch.event_handlers import OnProcessExit
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def setup(context):
    pair = (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION"))
    allowed = {("95", "little-orbo-sim")}
    if os.environ.get("ORBO_GAZEBO_TESTS") == "1":
        allowed.add(("99", "little-orbo-sim-test"))
    if pair not in allowed:
        raise RuntimeError(
            "Use pixi run orbo-sim (isolated ROS domain 95 / Gazebo partition little-orbo-sim)"
        )
    share = Path(get_package_share_directory("orbo_simulation"))
    description = Path(get_package_share_directory("orbo_description"))
    robot = xacro.process_file(str(share / "urdf/orbo.sim.urdf.xacro")).toxml()
    robot = robot.replace("package://orbo_description/", description.as_uri() + "/")
    gui = LaunchConfiguration("gui").perform(context).lower() == "true"
    # macOS cannot run Gazebo's GUI and server inside the same gz process.
    command = ["gz", "sim", "-s", "-r", "-v", "2", str(share / "worlds/room.sdf")]
    simulator = ExecuteProcess(cmd=command, output="screen")
    viewer = []
    if gui:
        gui_command = [
            "gz",
            "sim",
            "-g",
            "-v",
            "2",
            "--gui-config",
            str(share / "config/gui.config"),
        ]
        if os.environ.get("ORBO_GZ_RUBY"):
            gui_command = [
                os.environ["ORBO_GZ_RUBY"],
                shutil.which("gz"),
                *gui_command[1:],
                "--render-engine-gui-api-backend",
                "opengl",
            ]
        gui_process = ExecuteProcess(cmd=gui_command, output="screen")
        viewer = [
            gui_process,
            RegisterEventHandler(
                OnProcessExit(
                    target_action=gui_process,
                    on_exit=[EmitEvent(event=Shutdown(reason="Gazebo window closed"))],
                )
            ),
        ]
    state = Node(
        package="robot_state_publisher",
        executable="robot_state_publisher",
        namespace="orbo_sim",
        parameters=[{"robot_description": robot, "use_sim_time": True}],
        output="screen",
    )
    spawn = Node(
        package="ros_gz_sim",
        executable="create",
        namespace="orbo_sim",
        arguments=[
            "-world",
            "orbo_room",
            "-name",
            "orbo",
            "-topic",
            "/orbo_sim/robot_description",
            "-x",
            "-0.8",
            "-y",
            "-0.8",
            "-z",
            "0.035",
        ],
        output="screen",
    )
    controllers = Node(
        package="controller_manager",
        executable="spawner",
        arguments=[
            "joint_state_broadcaster",
            "drive_controller",
            "--activate-as-group",
            "-c",
            "/orbo_sim/controller_manager",
            "--controller-manager-timeout",
            "60",
        ],
        output="screen",
    )

    def after_spawn(event, _context):
        if event.returncode != 0:
            return [EmitEvent(event=Shutdown(reason="Could not spawn simulated Orbo"))]
        return [controllers]

    def after_controllers(event, _context):
        if event.returncode != 0:
            return [
                EmitEvent(
                    event=Shutdown(
                        reason="Could not activate simulated drive controllers"
                    )
                )
            ]
        # Wait for initialization before the GUI requests the initial world state.
        return viewer

    return [
        simulator,
        RegisterEventHandler(
            OnProcessExit(
                target_action=simulator,
                on_exit=[EmitEvent(event=Shutdown(reason="Gazebo exited"))],
            )
        ),
        Node(
            package="ros_gz_bridge",
            executable="parameter_bridge",
            arguments=["/clock@rosgraph_msgs/msg/Clock[gz.msgs.Clock"],
            output="screen",
        ),
        state,
        spawn,
        RegisterEventHandler(OnProcessExit(target_action=spawn, on_exit=after_spawn)),
        RegisterEventHandler(
            OnProcessExit(target_action=controllers, on_exit=after_controllers)
        ),
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument("gui", default_value="true"),
            OpaqueFunction(function=setup),
        ]
    )

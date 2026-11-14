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
    IncludeLaunchDescription,
)
from launch.event_handlers import OnProcessExit
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.events import Shutdown
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node


def setup(context):
    pair = (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION"))
    allowed = {("95", "little-orbo-sim")}
    if os.environ.get("ORBO_GAZEBO_TESTS") == "1":
        allowed.add(("99", "little-orbo-sim-test"))
        allowed.add(("100", "little-orbo-embodied-test"))
    if pair not in allowed:
        raise RuntimeError(
            "Use pixi run orbo-sim (isolated ROS domain 95 / Gazebo partition little-orbo-sim)"
        )
    share = Path(get_package_share_directory("orbo_simulation"))
    description = Path(get_package_share_directory("orbo_description"))
    sensors = LaunchConfiguration("sensors").perform(context).lower() == "true"
    navigation = LaunchConfiguration("navigation").perform(context).lower() == "true"
    embodied = LaunchConfiguration("embodied").perform(context).lower() == "true"
    if embodied and not navigation:
        raise RuntimeError("Embodied simulation requires navigation")
    world = LaunchConfiguration("world").perform(context)
    if world not in ("room", "living_room"):
        raise RuntimeError("world must be room or living_room")
    if navigation and (not sensors or world != "living_room"):
        raise RuntimeError("Nav2 requires sensors and the matching living_room map")
    robot = xacro.process_file(
        str(share / "urdf/orbo.sim.urdf.xacro"),
        mappings={"sensors": str(sensors).lower()},
    ).toxml()
    robot = robot.replace("package://orbo_description/", description.as_uri() + "/")
    gui = LaunchConfiguration("gui").perform(context).lower() == "true"
    # macOS cannot run Gazebo's GUI and server inside the same gz process.
    command = [
        "gz",
        "sim",
        "-s",
        "-r",
        "-v",
        "2",
        str(share / f"worlds/{world}.sdf"),
        "--render-engine-server-api-backend",
        "opengl",
    ]
    simulator = ExecuteProcess(cmd=command, output="screen")
    clock_bridge = Node(
        package="orbo_simulation", executable="clock_bridge", output="screen"
    )
    viewer = []
    if gui:
        gui_command = [
            "gz",
            "sim",
            "-g",
            "-v",
            "2",
            "--gui-config",
            str(share / "config" / ("sensors.gui.config" if sensors else "gui.config")),
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
        return viewer + (
            [
                IncludeLaunchDescription(
                    PythonLaunchDescriptionSource(
                        str(share / "launch/navigation.launch.py")
                    ),
                    launch_arguments={"embodied": str(embodied).lower()}.items(),
                )
            ]
            if navigation
            else []
        )

    return [
        simulator,
        RegisterEventHandler(
            OnProcessExit(
                target_action=simulator,
                on_exit=[EmitEvent(event=Shutdown(reason="Gazebo exited"))],
            )
        ),
        clock_bridge,
        RegisterEventHandler(
            OnProcessExit(
                target_action=clock_bridge,
                on_exit=[EmitEvent(event=Shutdown(reason="Simulation clock exited"))],
            )
        ),
        *(
            [
                Node(
                    package="ros_gz_bridge",
                    executable="parameter_bridge",
                    namespace="orbo_sim",
                    parameters=[{"use_sim_time": True}],
                    arguments=[
                        "/orbo_sim/scan@sensor_msgs/msg/LaserScan[gz.msgs.LaserScan",
                        "/orbo_sim/camera/image_raw@sensor_msgs/msg/Image[gz.msgs.Image",
                        "/orbo_sim/camera/camera_info@sensor_msgs/msg/CameraInfo[gz.msgs.CameraInfo",
                    ],
                    output="screen",
                )
            ]
            if sensors
            else []
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
            DeclareLaunchArgument("world", default_value="living_room"),
            DeclareLaunchArgument("sensors", default_value="true"),
            DeclareLaunchArgument("navigation", default_value="true"),
            DeclareLaunchArgument("embodied", default_value="false"),
            OpaqueFunction(function=setup),
        ]
    )

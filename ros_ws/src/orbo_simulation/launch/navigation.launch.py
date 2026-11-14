"""AMCL and Nav2 in the isolated simulator domain; no hardware runtime."""

import os
from pathlib import Path
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import OpaqueFunction, DeclareLaunchArgument
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from launch_ros.descriptions import ParameterFile
from nav2_common.launch import RewrittenYaml


def setup(context):
    allowed = {("95", "little-orbo-sim")}
    if os.environ.get("ORBO_GAZEBO_TESTS") == "1":
        allowed.add(("99", "little-orbo-sim-test"))
        allowed.add(("100", "little-orbo-embodied-test"))
    if (os.environ.get("ROS_DOMAIN_ID"), os.environ.get("GZ_PARTITION")) not in allowed:
        raise RuntimeError("Simulation domain/partition required")
    embodied = LaunchConfiguration("embodied").perform(context).lower() == "true"
    share = Path(get_package_share_directory("orbo_simulation"))
    # ROS parameter keys must include the namespace, including nested costmaps.
    params = ParameterFile(
        RewrittenYaml(
            source_file=str(share / "config/nav2.yaml"),
            root_key="orbo_sim",
            param_rewrites={},
            convert_types=True,
        ),
        allow_substs=True,
    )
    servers = [
        ("nav2_map_server", "map_server"),
        ("nav2_amcl", "amcl"),
        ("nav2_controller", "controller_server"),
        ("nav2_planner", "planner_server"),
        ("nav2_behaviors", "behavior_server"),
        ("nav2_bt_navigator", "bt_navigator"),
        ("nav2_velocity_smoother", "velocity_smoother"),
        ("nav2_collision_monitor", "collision_monitor"),
    ]
    nodes = []
    for package, name in servers:
        extra = {"use_sim_time": True}
        if name == "collision_monitor" and embodied:
            extra["cmd_vel_out_topic"] = "cmd_vel"
        if name == "velocity_smoother" and embodied:
            # Jazzy otherwise repeats an input stamp with changed smoothed values,
            # which correctly fails the broker gate's command-order check.
            extra["stamp_smoothed_velocity_with_smoothing_time"] = True
            extra["velocity_timeout"] = 0.15
        if name == "map_server":
            extra["yaml_filename"] = str(share / "maps/living_room.yaml")
        remaps = []  # RSP/controller publish global /tf; all nodes share that tree.
        if name in ("controller_server", "behavior_server", "velocity_smoother"):
            remaps = [("cmd_vel", "cmd_vel_nav")]
        nodes.append(
            Node(
                package=package,
                executable=name,
                name=name,
                namespace="orbo_sim",
                parameters=[params, extra],
                remappings=remaps,
                output="screen",
            )
        )
    for group, names in [
        ("localization", ["map_server", "amcl"]),
        ("navigation", [n for _, n in servers[2:]]),
    ]:
        nodes.append(
            Node(
                package="nav2_lifecycle_manager",
                executable="lifecycle_manager",
                name=f"lifecycle_manager_{group}",
                namespace="orbo_sim",
                parameters=[
                    {
                        "use_sim_time": True,
                        "autostart": True,
                        "node_names": names,
                        "bond_timeout": 4.0,
                    }
                ],
                output="screen",
            )
        )
    if embodied:
        nodes.append(
            Node(
                package="orbo_runtime",
                executable="velocity_gate",
                namespace="orbo_sim",
                name="embodied_velocity_gate",
                parameters=[
                    {
                        "use_sim_time": True,
                        "max_linear": 0.15,
                        "max_angular": 1.0,
                        "source": "nav2",
                    }
                ],
                output="screen",
            )
        )
    return nodes


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument("embodied", default_value="false"),
            OpaqueFunction(function=setup),
        ]
    )

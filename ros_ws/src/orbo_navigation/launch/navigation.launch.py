from pathlib import Path
import tempfile
import yaml
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import (
    DeclareLaunchArgument,
    IncludeLaunchDescription,
    OpaqueFunction,
    GroupAction,
)
from launch.launch_description_sources import PythonLaunchDescriptionSource
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node, SetRemap
from orbo_runtime.commissioning import validate_calibration


def setup(context):
    path = LaunchConfiguration("calibration").perform(context)
    calibration = validate_calibration(
        yaml.safe_load(Path(path).read_text()), navigation=True
    )
    map_path = Path(LaunchConfiguration("map").perform(context))
    if not map_path.is_file():
        raise RuntimeError("A surveyed map YAML is required")
    nav = calibration["navigation"]
    share = Path(get_package_share_directory("nav2_bringup"))
    params = yaml.safe_load((share / "params/nav2_params.yaml").read_text())
    # Start from the installed Jazzy default nodes, explicitly make all velocity paths stamped.
    for name in [
        "controller_server",
        "behavior_server",
        "velocity_smoother",
        "collision_monitor",
    ]:
        params.setdefault(name, {}).setdefault("ros__parameters", {})[
            "enable_stamped_cmd_vel"
        ] = True
    for name in [
        "controller_server",
        "velocity_smoother",
        "behavior_server",
        "bt_navigator",
    ]:
        params[name]["ros__parameters"]["odom_topic"] = "/orbo/drive_controller/odom"
    # Installed Jazzy defaults describe a different robot. Replace physical and
    # task bounds with the commissioned profile before any lifecycle activation.
    params["bt_navigator"]["ros__parameters"]["robot_base_frame"] = "base_link"
    controller = params["controller_server"]["ros__parameters"]
    follow = controller["FollowPath"]
    if follow.get("plugin") != "nav2_mppi_controller::MPPIController":
        raise RuntimeError(
            "Installed Nav2 controller differs from qualified MPPI configuration"
        )
    follow.update(
        {
            "vx_max": calibration["max_linear_m_s"],
            "vx_min": -calibration["max_linear_m_s"],
            "vy_max": 0.0,
            "wz_max": calibration["max_angular_rad_s"],
            "motion_model": "DiffDrive",
            "ax_max": nav["max_linear_accel"],
            "ax_min": -nav["max_linear_accel"],
            "ay_max": 0.0,
            "ay_min": 0.0,
            "az_max": nav["max_angular_accel"],
            "visualize": False,
        }
    )
    controller["general_goal_checker"].update(
        {
            "xy_goal_tolerance": nav["goal_xy_tolerance"],
            "yaw_goal_tolerance": nav["goal_yaw_tolerance"],
        }
    )
    collision = params["collision_monitor"]["ros__parameters"]
    collision.update(
        {
            "base_frame_id": "base_link",
            "source_timeout": nav["scan_timeout"],
            "observation_sources": ["scan"],
        }
    )
    collision["scan"].update({"topic": nav["scan_topic"]})
    collision["FootprintApproach"]["footprint_topic"] = (
        "local_costmap/published_footprint"
    )
    collision["FootprintApproach"]["time_before_collision"] = nav[
        "collision_time_horizon"
    ]
    smoother = params["velocity_smoother"]["ros__parameters"]
    smoother.update(
        {
            "max_accel": [nav["max_linear_accel"], 0.0, nav["max_angular_accel"]],
            "max_decel": [-nav["max_linear_accel"], 0.0, -nav["max_angular_accel"]],
            "velocity_timeout": 0.2,
            # Changing smoothed values need progressing source timestamps for
            # the strict command-order gate (Nav2 Jazzy smoother semantics).
            "stamp_smoothed_velocity_with_smoothing_time": True,
        }
    )
    params["amcl"]["ros__parameters"].update(
        {
            "base_frame_id": "base_link",
            "odom_frame_id": "odom",
            "global_frame_id": "map",
            "scan_topic": nav["scan_topic"],
        }
    )
    half_l, half_w = calibration["base_length"] / 2, calibration["base_width"] / 2
    footprint = str(
        [[half_l, half_w], [half_l, -half_w], [-half_l, -half_w], [-half_l, half_w]]
    )
    for name in ["local_costmap", "global_costmap"]:
        costmap = params[name][name]["ros__parameters"]
        costmap.update({"robot_base_frame": "base_link", "footprint": footprint})
        costmap.pop("robot_radius", None)
        for layer_name in ["obstacle_layer", "voxel_layer"]:
            if layer_name in costmap:
                layer = costmap[layer_name]
                layer["observation_sources"] = "scan"
                layer["scan"] = {
                    "topic": nav["scan_topic"],
                    "sensor_frame": nav["sensor_frame"],
                    "data_type": "LaserScan",
                    "marking": True,
                    "clearing": True,
                    "obstacle_max_range": nav["obstacle_max"],
                    "raytrace_max_range": nav["raytrace_max"],
                }
        costmap["inflation_layer"]["inflation_radius"] = nav["inflation_radius"]
    params["velocity_smoother"]["ros__parameters"].update(
        {
            "max_velocity": [
                calibration["max_linear_m_s"],
                0.0,
                calibration["max_angular_rad_s"],
            ],
            "min_velocity": [
                -calibration["max_linear_m_s"],
                0.0,
                -calibration["max_angular_rad_s"],
            ],
        }
    )
    output = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="orbo-nav2-", delete=False
    )
    yaml.safe_dump(params, output)
    output.close()
    return [
        GroupAction(
            [
                # Override Nav2's relative TF remaps to share the control/RViz tree.
                SetRemap(src="/tf", dst="/tf"),
                SetRemap(src="/tf_static", dst="/tf_static"),
                IncludeLaunchDescription(
                    PythonLaunchDescriptionSource(
                        str(share / "launch/bringup_launch.py")
                    ),
                    launch_arguments={
                        "namespace": "orbo",
                        "use_namespace": "true",
                        "map": str(map_path),
                        "params_file": output.name,
                        "use_sim_time": "false",
                        "autostart": "false",
                        "use_composition": "false",
                    }.items(),
                ),
            ]
        ),
        Node(
            package="orbo_runtime",
            executable="velocity_gate",
            output="screen",
            parameters=[
                {
                    "source": "nav2",
                    "max_linear": calibration["max_linear_m_s"],
                    "max_angular": calibration["max_angular_rad_s"],
                }
            ],
        ),
    ]


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "calibration", default_value="/etc/orbo/calibration.yaml"
            ),
            DeclareLaunchArgument("map"),
            OpaqueFunction(function=setup),
        ]
    )

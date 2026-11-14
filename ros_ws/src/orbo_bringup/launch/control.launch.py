from pathlib import Path
import yaml
import xacro
from ament_index_python.packages import get_package_share_directory
from launch import LaunchDescription
from launch.actions import DeclareLaunchArgument, OpaqueFunction
from launch.substitutions import LaunchConfiguration
from launch_ros.actions import Node
from orbo_runtime.commissioning import validate_calibration


def setup(context):
    path = LaunchConfiguration("calibration").perform(context)
    calibration = validate_calibration(yaml.safe_load(Path(path).read_text()), allow_bench=True)
    if calibration.get("bench_only"):
        print("LIFTED BENCH: CAD dimensions; encoder odometry is not ground-distance calibration.")
    description = xacro.process_file(
        str(
            Path(get_package_share_directory("orbo_description"))
            / "config/orbo.urdf.xacro"
        ),
        mappings={"calibration": path},
    ).toxml()
    params = {
        "update_rate": 50,
        "hardware_components_initial_state.inactive": ["OrboWheels"],
        "joint_state_broadcaster.type": "joint_state_broadcaster/JointStateBroadcaster",
        "drive_controller.type": "diff_drive_controller/DiffDriveController",
    }
    drive = {
        "left_wheel_names": ["left_wheel_joint"],
        "right_wheel_names": ["right_wheel_joint"],
        "wheel_separation": calibration["wheel_separation"],
        "wheel_radius": calibration["wheel_radius"],
        "open_loop": False,
        "position_feedback": True,
        "publish_rate": 50.0,
        "odom_frame_id": "odom",
        "base_frame_id": "base_link",
        "enable_odom_tf": True,
        "cmd_vel_timeout": 0.2,
        "tf_frame_prefix_enable": False,
        "linear.x.max_velocity": calibration["max_linear_m_s"],
        "linear.x.min_velocity": -calibration["max_linear_m_s"],
        "angular.z.max_velocity": calibration["max_angular_rad_s"],
        "angular.z.min_velocity": -calibration["max_angular_rad_s"],
    }
    # Controller receives its own parameters through the spawner, with a generated local file.
    import tempfile

    target = tempfile.NamedTemporaryFile(
        mode="w", suffix=".yaml", prefix="orbo-controller-", delete=False
    )
    yaml.safe_dump({"/orbo/drive_controller": {"ros__parameters": drive}}, target)
    target.close()
    actions = [
        Node(
            package="robot_state_publisher",
            executable="robot_state_publisher",
            namespace="orbo",
            parameters=[{"robot_description": description}],
        ),
        Node(
            package="controller_manager",
            executable="ros2_control_node",
            namespace="orbo",
            parameters=[params],
            remappings=[
                ("robot_description", "/orbo/robot_description"),
            ],
        ),
        Node(
            package="controller_manager",
            executable="spawner",
            namespace="orbo",
            arguments=["joint_state_broadcaster", "-c", "/orbo/controller_manager"]
                + (["--inactive"] if calibration.get("bench_only") else []),
        ),
        Node(
            package="controller_manager",
            executable="spawner",
            namespace="orbo",
            arguments=[
                "drive_controller",
                "-c",
                "/orbo/controller_manager",
                "--param-file",
                target.name,
                "--inactive",
            ],
        ),
    ]
    if calibration.get("bench_only"):
        actions.append(Node(
            package="orbo_runtime", executable="velocity_gate", output="screen",
            parameters=[{"source": "teleop", "max_linear": calibration["max_linear_m_s"],
                         "max_angular": calibration["max_angular_rad_s"]}],
        ))
    return actions


def generate_launch_description():
    return LaunchDescription(
        [
            DeclareLaunchArgument(
                "calibration", default_value="/etc/orbo/calibration.yaml"
            ),
            OpaqueFunction(function=setup),
        ]
    )

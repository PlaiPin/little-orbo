"""Evaluate installed launch files without starting drivers or enabling motion."""

import importlib.util
import os
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_ROS_TESTS") != "1", reason="explicit Jazzy integration lane"
)


@pytest.fixture(autouse=True)
def isolated_domain():
    assert os.environ.get("ROS_DOMAIN_ID") == "96", "Never test on the robot domain"


def load_launch(package, name):
    from ament_index_python.packages import get_package_share_directory

    path = Path(get_package_share_directory(package)) / "launch" / name
    spec = importlib.util.spec_from_file_location(package + name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def context_for(**values):
    from launch import LaunchContext

    context = LaunchContext()
    context.launch_configurations.update(values)
    return context


def resolve_topics(action, context, monkeypatch):
    import rclpy
    from launch.actions import ExecuteProcess

    # Evaluate the real launch Node (including scoped remap precedence) but
    # suppress process execution: camera and navigation drivers must not start.
    monkeypatch.setattr(ExecuteProcess, "execute", lambda self, ctx: None)
    action.execute(context)
    args = ["--ros-args"]
    for source, target in action.expanded_remapping_rules or []:
        args.extend(["-r", source + ":=" + target])
    ros = rclpy.Context()
    ros.init(args=[])
    node = rclpy.create_node(
        "launch_resolution_probe",
        namespace="/orbo",
        context=ros,
        cli_args=args,
        use_global_arguments=False,
    )
    try:
        return {
            topic: node.resolve_topic_name(topic)
            for topic in ("/tf", "/tf_static", "cmd_vel")
        }
    finally:
        node.destroy_node()
        ros.shutdown()


def test_camera_mount_joins_global_tf(tmp_path, monkeypatch):
    import yaml

    config = {
        "enabled": True,
        "driver": "libcamera",
        "camera": "SYNTHETIC",
        "width": 640,
        "height": 480,
        "frame_id": "camera_optical_frame",
        "mount": {
            **dict.fromkeys(("x", "y", "z", "roll", "pitch", "yaw"), 0.0),
            "evidence_record": "SYNTHETIC TEST ONLY",
        },
    }
    path = tmp_path / "camera.yaml"
    path.write_text(yaml.safe_dump(config))
    context = context_for(config=str(path))
    actions = load_launch("orbo_bringup", "camera.launch.py").setup(context)
    resolved = resolve_topics(actions[-1], context, monkeypatch)
    assert resolved["/tf_static"] == "/tf_static"


def test_nav2_uses_global_tf_and_namespaced_velocity(tmp_path, monkeypatch):
    import yaml
    from ament_index_python.packages import get_package_share_directory
    from launch.actions import GroupAction, IncludeLaunchDescription
    from launch_ros.actions import Node, SetRemap

    profile = yaml.safe_load(
        (
            Path(get_package_share_directory("orbo_description"))
            / "config/bench-cad.yaml"
        ).read_text()
    )
    profile.update(
        bench_only=False, wheels_qualified=True, evidence_record="SYNTHETIC TEST ONLY"
    )
    profile["navigation"] = {
        "qualified": True,
        "operating_surface": "floor",
        "sensor_type": "LaserScan",
        "scan_topic": "/synthetic_scan",
        "sensor_frame": "test_laser",
        **dict.fromkeys(
            (
                "range_max",
                "obstacle_max",
                "raytrace_max",
                "inflation_radius",
                "max_linear_accel",
                "max_angular_accel",
                "goal_xy_tolerance",
                "goal_yaw_tolerance",
                "scan_timeout",
                "collision_time_horizon",
            ),
            0.1,
        ),
    }
    calibration = tmp_path / "calibration.yaml"
    calibration.write_text(yaml.safe_dump(profile))
    map_path = tmp_path / "map.yaml"
    map_path.write_text("# No map is loaded by this configuration-only test.\n")
    context = context_for(calibration=str(calibration), map=str(map_path))
    actions = load_launch("orbo_navigation", "navigation.launch.py").setup(context)
    scoped = actions[0].get_sub_entities()
    for action in scoped:
        if isinstance(action, SetRemap):
            action.execute(context)
    include = next(
        action for action in scoped if isinstance(action, IncludeLaunchDescription)
    )
    arguments = dict(include.launch_arguments)
    assert arguments["autostart"] == "false"
    assert arguments["use_composition"] == "false"
    context.launch_configurations.update(arguments)
    context.launch_configurations.update(
        ros_namespace="/orbo",
        use_respawn="false",
        log_level="info",
        container_name="nav2_container",
    )
    params_path = Path(arguments["params_file"])
    try:
        params = yaml.safe_load(params_path.read_text())
        assert (
            params["controller_server"]["ros__parameters"]["odom_topic"]
            == "/orbo/drive_controller/odom"
        )
        description = load_launch(
            "nav2_bringup", "navigation_launch.py"
        ).generate_launch_description()
        nodes = [
            node
            for group in description.entities
            if isinstance(group, GroupAction)
            for node in group.get_sub_entities()
            if isinstance(node, Node)
        ]
        assert nodes
        for node in nodes:
            resolved = resolve_topics(node, context, monkeypatch)
            assert resolved["/tf"] == "/tf"
            assert resolved["/tf_static"] == "/tf_static"
            assert resolved["cmd_vel"].startswith("/orbo/")
    finally:
        params_path.unlink()


def test_navigation_rejects_nominal_bench_geometry():
    from ament_index_python.packages import get_package_share_directory
    from orbo_runtime.contracts import Rejected

    profile = (
        Path(get_package_share_directory("orbo_description")) / "config/bench-cad.yaml"
    )
    with pytest.raises(Rejected, match="wheel qualification"):
        load_launch("orbo_navigation", "navigation.launch.py").setup(
            context_for(calibration=str(profile), map="unused")
        )

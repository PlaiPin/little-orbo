"""Validate supplied calibration; never fill missing physical dimensions/sensors."""

import math

from .contracts import Rejected


def validate_calibration(data, *, navigation=False, allow_bench=False):
    bench = allow_bench and data.get("bench_only") is True and not navigation
    if data.get("wheels_qualified") is not True and not bench:
        raise Rejected("wheel qualification record required")
    for key in [
        "wheel_radius",
        "wheel_separation",
        "wheel_width",
        "base_length",
        "base_width",
        "base_height",
        "max_wheel_rad_s",
        "max_linear_m_s",
        "max_angular_rad_s",
    ]:
        value = data.get(key)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise Rejected("missing or invalid measured calibration: " + key)
    if data["max_wheel_rad_s"] > 6:
        raise Rejected("wheel limit exceeds hardware plugin bound")
    if bench and data["max_wheel_rad_s"] > 1.2:
        raise Rejected("lifted bench profile exceeds tested firmware bound")
    if (
        not isinstance(data.get("evidence_record"), str)
        or not data["evidence_record"].strip()
    ):
        raise Rejected("commissioning evidence reference required")
    if navigation:
        nav = data.get("navigation", {})
        if nav.get("qualified") is not True or nav.get("operating_surface") not in {
            "floor",
            "constrained_table",
        }:
            raise Rejected("qualified operating area required")
        if (
            nav.get("sensor_type") != "LaserScan"
            or not nav.get("scan_topic")
            or not nav.get("sensor_frame")
        ):
            raise Rejected(
                "this Nav2 profile requires an actual qualified LaserScan source and frame"
            )
        for name in [
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
        ]:
            value = nav.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise Rejected("measured navigation parameter required: " + name)
        if (
            nav["obstacle_max"] > nav["range_max"]
            or nav["raytrace_max"] > nav["range_max"]
        ):
            raise Rejected("costmap range exceeds qualified sensor range")
    return data

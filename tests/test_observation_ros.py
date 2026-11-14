import math
import sys
from types import SimpleNamespace as NS

import pytest

from orbo_runtime.contracts import ManualClock, Rejected
from orbo_runtime.observations import ROSPoseSource


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setitem(sys.modules, "geometry_msgs", NS())
    monkeypatch.setitem(
        sys.modules, "geometry_msgs.msg", NS(PoseWithCovarianceStamped=object)
    )
    monkeypatch.setitem(sys.modules, "rclpy", NS())
    monkeypatch.setitem(
        sys.modules,
        "rclpy.qos",
        NS(
            QoSProfile=lambda **kwargs: kwargs,
            ReliabilityPolicy=NS(RELIABLE="reliable"),
            DurabilityPolicy=NS(TRANSIENT_LOCAL="transient"),
        ),
    )
    monkeypatch.setitem(sys.modules, "std_srvs", NS())
    monkeypatch.setitem(
        sys.modules, "std_srvs.srv", NS(Empty=NS(Request=lambda: object()))
    )
    clock = ManualClock()
    node = NS(stamp=10_000_000_000, subscriptions=[])
    node.get_clock = lambda: NS(now=lambda: NS(nanoseconds=node.stamp))
    node.create_subscription = lambda kind, topic, cb, qos: node.subscriptions.append(
        (topic, cb, qos)
    )
    source = ROSPoseSource(
        node,
        {"shelf": {"x": 1, "y": 2, "yaw": 0}},
        map_version="survey-a",
        refresh_service=None,
        clock=clock,
    )
    return source, node, clock


def pose(*, x=1, y=2, yaw=0, stamp=10, frame="map", variance=0.001):
    covariance = [0.0] * 36
    covariance[0] = covariance[7] = covariance[35] = variance
    return NS(
        header=NS(frame_id=frame, stamp=NS(sec=stamp, nanosec=0)),
        pose=NS(
            covariance=covariance,
            pose=NS(
                position=NS(x=x, y=y, z=0),
                orientation=NS(x=0, y=0, z=math.sin(yaw / 2), w=math.cos(yaw / 2)),
            ),
        ),
    )


def test_pose_requires_actual_position_freshness_covariance_and_frame(setup):
    source, node, clock = setup
    source.observe(pose())
    assert source.snapshot()["viewpoint"] == "shelf"
    assert source.snapshot()["pose"]["x"] == 1
    assert node.subscriptions[0][0] == "amcl_pose"
    assert node.subscriptions[0][2]["durability"] == "transient"
    clock.advance(1.1)
    assert source.snapshot() is None
    node.stamp = 11_000_000_000
    source.observe(pose(stamp=11, x=9))
    assert source.snapshot() is None
    source.select_current_view(True)
    current = source.snapshot()
    assert current["viewpoint"] == "current" and current["pose"]["x"] == 9
    source.select_current_view(False)
    assert source.snapshot() is None


@pytest.mark.parametrize(
    "changes",
    [
        {"x": float("nan")},
        {"frame": "odom"},
        {"variance": 0.9},
        {"variance": -1},
        {"stamp": 1},
        {"stamp": 12},
        {"yaw": 1},
    ],
)
def test_bad_pose_cannot_establish_surveyed_coverage(setup, changes):
    source, _, _ = setup
    source.observe(pose(**changes))
    assert source.snapshot() is None


def test_new_invalid_localization_invalidates_previous_pose(setup):
    source, _, _ = setup
    source.observe(pose())
    assert source.snapshot()
    source.observe(pose(variance=3))
    assert source.snapshot() is None


async def test_refresh_requires_live_localization_service(setup):
    source, _, _ = setup
    with pytest.raises(Rejected):
        await source.refresh()
    source.observe(pose())
    await source.refresh()

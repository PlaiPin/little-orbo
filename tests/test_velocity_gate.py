import pytest

from orbo_runtime.contracts import Rejected
from orbo_runtime.velocity_gate import Velocity, VelocityGate


def test_exclusive_source_and_original_stamp():
    gate = VelocityGate(max_linear=0.2, max_angular=0.5)
    gate.authorize("nav2", 1, 10.5, 10)
    sample = Velocity("nav2", 1_000_000_000, 0.1, 0)
    gate.accept(sample, 1_050_000_000, 10.05)
    assert gate.current(1_100_000_000, 10.1).stamp_ns == 1_000_000_000
    with pytest.raises(Rejected):
        gate.accept(Velocity("teleop", 1_100_000_000, 0.1, 0), 1_100_000_000, 10.1)
    assert gate.current(1_200_000_000, 10.2) is None


def test_lease_loss_and_source_switch_require_new_epoch():
    gate = VelocityGate(max_linear=0.2, max_angular=0.5)
    gate.authorize("nav2", 1, 0.1, 0)
    with pytest.raises(Rejected):
        gate.authorize("teleop", 1, 0.1, 0)
    gate.accept(Velocity("nav2", 10, 0.1, 0), 10, 0)
    assert gate.current(20, 0.11) is None
    with pytest.raises(Rejected):
        gate.accept(Velocity("nav2", 30, 0.1, 0), 30, 0.11)
    gate.authorize("teleop", 2, 0.3, 0.2)


@pytest.mark.parametrize(
    "sample",
    [
        Velocity("nav2", 11, 0, 0),
        Velocity("nav2", 10, float("nan"), 0),
        Velocity("nav2", 10, 0.3, 0),
    ],
)
def test_invalid_stamped_velocity(sample):
    gate = VelocityGate(max_linear=0.2, max_angular=0.5)
    gate.authorize("nav2", 1, 0.5, 0)
    with pytest.raises(Rejected):
        gate.accept(sample, 10, 0)


def test_stopped_epoch_cannot_be_renewed():
    gate = VelocityGate(max_linear=0.2, max_angular=0.5)
    gate.authorize("teleop", 1, 0.2, 0)
    gate.stop()
    with pytest.raises(Rejected):
        gate.authorize("teleop", 1, 0.3, 0.1)
    gate.authorize("teleop", 2, 0.3, 0.1)

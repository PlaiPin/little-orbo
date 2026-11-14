"""Real pinned SDK lifecycle with a synthetic controller boundary; no robot I/O."""

import os
import time
from types import SimpleNamespace as NS

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv("ORBO_K1_SDK_TESTS") != "1", reason="explicit real Booster SDK lane"
)


def test_k1_vendor_action_catalog_and_observed_stop_lifecycle():
    from booster_robotics_sdk_python import Action, BodyControl, HandAction
    from boosteros.robots.booster._motion.actions import get_action_spec
    from boosteros.robots.booster._motion.action_task import BoosterActionTaskHandle

    spec = get_action_spec("hand_wave", "K1")
    assert (
        spec.info.interruptible
        and spec.info.type == "upper_body"
        and spec.stop is not None
    )
    calls = []

    class Controller:
        running = False

        def WaveHand(self, command):
            calls.append(command)
            self.running = command == HandAction.kHandOpen

        def get_status(self, **kwargs):
            return NS(
                current_actions=[Action.kHandWave] if self.running else [],
                current_body_control=BodyControl.kWBCGait,
            )

    robot = NS(loco_client=Controller(), _get_model=lambda: "K1")
    handle = BoosterActionTaskHandle(robot=robot, spec=spec)
    handle._start()
    deadline = time.monotonic() + 2
    while not handle._has_observed_running() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert handle._has_observed_running()
    assert handle.cancel()
    assert handle.wait(2) == "CANCELLED"
    assert calls == [HandAction.kHandOpen, HandAction.kHandClose]

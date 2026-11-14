"""Health replay tests exercise the real adapter callback without ROS imports."""

from collections import deque
import threading
import time
from types import SimpleNamespace

import pytest

from orbo_runtime.contracts import Rejected
from orbo_runtime.ros_adapter import OrboAdapter


def test_health_does_not_refresh_on_replay_and_rejects_retired_boot():
    adapter = object.__new__(OrboAdapter)
    adapter.lock = threading.Lock()
    adapter.health = None
    adapter.health_at = 0
    adapter.retired_boots = deque(maxlen=8)
    adapter.statuses = {}

    def message(boot=1, mono=1, epoch=2, age=0):
        return SimpleNamespace(
            interface_version=1,
            boot_id=boot,
            mono_us=mono,
            epoch=epoch,
            epoch_ms=time.time_ns() // 1_000_000 - age,
            clock_synced=True,
        )

    first = message()
    adapter._health(first)
    initial = adapter.health_at
    adapter._health(first)
    adapter._health(message(mono=2, epoch=1))
    assert adapter.health_at == initial and adapter._snapshot() is first
    adapter.statuses[1] = "previous boot"
    adapter._health(message(boot=2))
    assert not adapter.statuses
    adapter._health(message(boot=1, mono=999, epoch=9))
    assert adapter.health.boot_id == 2
    adapter._health(message(boot=2, mono=2, age=1000))
    with pytest.raises(Rejected, match="fresh"):
        adapter._snapshot()
    adapter._health(message(boot=2, mono=3, age=-5000))
    with pytest.raises(Rejected, match="fresh"):
        adapter._snapshot()

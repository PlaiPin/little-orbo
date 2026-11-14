"""Shared action/evidence semantics; no vendor, ROS or inference dependency."""

from dataclasses import dataclass, field
from typing import Protocol
import hashlib
import json
import math
import time
import uuid


class Rejected(ValueError):
    pass


def identity() -> str:
    return str(uuid.uuid4())


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def finite(value, minimum: float, maximum: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Rejected("expected number")
    if not math.isfinite(value) or not minimum <= value <= maximum:
        raise Rejected("number outside bounds")
    return float(value)


class Clock(Protocol):
    def utc(self) -> float: ...
    def mono(self) -> float: ...


class SystemClock:
    utc = staticmethod(time.time)
    mono = staticmethod(time.monotonic)


@dataclass
class ManualClock:
    now: float = 1800000000.0
    elapsed: float = 0.0

    def utc(self):
        return self.now

    def mono(self):
        return self.elapsed

    def advance(self, seconds):
        self.now += seconds
        self.elapsed += seconds


@dataclass(frozen=True)
class Capability:
    name: str
    resources: tuple[str, ...]
    max_duration: float
    evidence: tuple[str, ...]
    qualified: bool = False


@dataclass(frozen=True)
class Action:
    command_id: str
    episode_id: str
    robot_id: str
    capability: str
    parameters: dict
    epoch: int
    deadline: float
    duration: float


@dataclass(frozen=True)
class ActionResult:
    command_id: str
    state: str
    evidence: str
    detail: dict = field(default_factory=dict)


class RobotAdapter(Protocol):
    robot_id: str
    simulated: bool
    capabilities: dict[str, Capability]

    async def dispatch(self, action: Action) -> ActionResult: ...
    async def cancel(self, command_id: str) -> ActionResult: ...
    async def controlled_stop(self, reason: str) -> None: ...
    async def reconcile(self, command_id: str) -> ActionResult: ...

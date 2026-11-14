#!/usr/bin/env python3
"""Resolve only the checked-in micro-ROS Agent source lock."""

import importlib.util
import json
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "firmware_bootstrap", ROOT / "tools/bootstrap-firmware.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
lock = json.loads((ROOT / "deploy/agent.source-lock.json").read_text())
base = ROOT / ".sdk/agent-locked"
for name, record in lock.items():
    module.checkout(base / name, record["url"], record["commit"])
agent = base / "micro_ros_agent/micro_ros_agent/cmake/SuperBuild.cmake"
s = agent.read_text()
s = re.sub(
    r"GIT_REPOSITORY\s+https://github.com/eProsima/Micro-XRCE-DDS-Agent.git\s+GIT_TAG\s+v2.4.3",
    'SOURCE_DIR\n                "' + str(base / "xrce_agent") + '"',
    s,
)
agent.write_text(s)
xrce = base / "xrce_agent/cmake/SuperBuild.cmake"
s = xrce.read_text()
s = re.sub(
    r"GIT_REPOSITORY\s+https://github.com/gabime/spdlog.git\s+GIT_TAG\s+\$\{_spdlog_tag\}",
    'SOURCE_DIR\n                "' + str(base / "spdlog") + '"',
    s,
)
xrce.write_text(s)
print(
    "Agent, XRCE transport, messages and compatible logger pinned; Fast DDS/CDR use Jazzy packages."
)

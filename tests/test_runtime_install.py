"""The installed application must work outside the checkout without ROS/extras."""

import os
import subprocess
import sys


def test_installed_modules_entry_points_and_cli(tmp_path):
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    probe = """
import importlib
import importlib.metadata
import pkgutil
import orbo_runtime

for module in pkgutil.iter_modules(orbo_runtime.__path__):
    importlib.import_module('orbo_runtime.' + module.name)
entries = importlib.metadata.distribution('orbo-runtime').entry_points
names = {entry.name for entry in entries}
assert {
    'orbo', 'orbo-audio-check', 'orbo-camera-monitor',
    'orbo-teleop-keyboard', 'orbo-velocity-gate',
} <= names or {
    'runtime', 'audio_check', 'base_bench', 'wheel_bench',
    'teleop_keyboard', 'velocity_gate', 'camera_monitor',
} <= names
for entry in entries:
    if entry.name not in {"base_bench", "wheel_bench"}:
        entry.load()
from orbo_runtime.cli import main
main()
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, "demo", "--database", str(tmp_path / "demo.db")],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "demo.db").is_file()

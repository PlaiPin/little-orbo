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
assert {entry.name for entry in entries} == {
    'orbo', 'orbo-audio-check', 'orbo-camera-monitor',
    'orbo-teleop-keyboard', 'orbo-velocity-gate',
}
for entry in entries:
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

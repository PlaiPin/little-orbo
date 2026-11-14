"""Exercise installer refusal/copy and overlay behavior without root or hardware."""

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['ORBO_TEST_CALLS'], 'a') as out:
    out.write(json.dumps([name, *args]) + '\n')
if name == 'id':
    print('0')
elif name == 'uname':
    print('aarch64')
elif name == 'apt-get' and '-s' in args:
    print(os.environ.get('ORBO_TEST_APT_PLAN', ''))
elif name == 'python3.12':
    # Stop before virtualenv creation; no actual software is installed.
    sys.exit(42)
'''


@pytest.fixture
def installer(tmp_path):
    source = tmp_path / 'source'
    (source / 'deploy').mkdir(parents=True)
    target = tmp_path / 'target'
    target.mkdir()
    (tmp_path / 'os-release').write_text('ID=ubuntu\nVERSION_ID=24.04\n')
    (tmp_path / 'ros-setup.bash').touch()
    script = (ROOT / 'deploy/install-pi.sh').read_text()
    for old, new in {
        '/etc/os-release': tmp_path / 'os-release',
        '/opt/ros/jazzy/setup.bash': tmp_path / 'ros-setup.bash',
        '/opt/orbo': target,
        '/etc/orbo': tmp_path / 'config',
        '/var/lib/orbo': tmp_path / 'state',
    }.items():
        script = script.replace(old, str(new))
    path = source / 'deploy/install-pi.sh'
    path.write_text(script)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('id', 'uname', 'apt-get', 'install', 'useradd', 'usermod', 'python3.12'):
        tool = bin_dir / name
        tool.write_text(MOCK)
        tool.chmod(0o755)
    calls = tmp_path / 'calls.jsonl'

    def run(*args, plan=''):
        result = subprocess.run(
            ['bash', str(path), *args], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
                     ORBO_TEST_CALLS=str(calls), ORBO_TEST_APT_PLAN=plan),
        )
        invoked = [json.loads(line) for line in calls.read_text().splitlines()]
        return result, invoked

    return source, target, run


@pytest.mark.parametrize('plan', [
    'Inst ros-jazzy-libcamera (0.7 Ubuntu)',
    'Inst libcamera0.3:arm64 (0.3 Ubuntu)',
    'Remv rpicam-apps [1.0]',
])
def test_existing_camera_change_refused_before_install(installer, plan):
    _, _, run = installer
    result, calls = run('--camera-stack', 'existing', plan=plan)
    assert result.returncode == 1
    assert 'would modify the existing camera stack' in result.stderr
    assert not any(call[:2] == ['apt-get', 'install'] for call in calls)


def test_default_preserves_camera_and_does_not_copy_private_or_generated_files(installer):
    source, target, run = installer
    for name in ('.live-validation/credentials.env', 'deploy/local/credentials.env',
                 '.pixi/envs/default/local', 'artifacts/release/old.tar.gz',
                 '.env', 'journal.db', '.sdk/source', 'ros_ws/install/stale'):
        private = source / name
        private.parent.mkdir(parents=True, exist_ok=True)
        private.write_text('LOCAL_ONLY')
    (source / 'deploy/public.toml').write_text('public = true')
    result, calls = run()
    assert result.returncode == 42, result.stderr
    apt = next(call for call in calls if call[:2] == ['apt-get', 'install'])
    assert 'ros-jazzy-camera-ros' not in apt
    assert (target / 'deploy/public.toml').is_file()
    assert not any(p.read_bytes() == b'LOCAL_ONLY' for p in target.rglob('*') if p.is_file())


def test_ros_camera_stack_requires_explicit_selection(installer):
    _, _, run = installer
    result, calls = run('--camera-stack', 'ros')
    assert result.returncode == 42, result.stderr
    apt = next(call for call in calls if call[:2] == ['apt-get', 'install'])
    assert 'ros-jazzy-camera-ros' in apt


@pytest.mark.parametrize('valid_overlay', [True, False])
def test_ros_wrapper_uses_selected_overlay_or_refuses_missing_one(tmp_path, valid_overlay):
    base = tmp_path / 'base.bash'
    base.write_text('export ORBO_TEST_OVERLAY=base\n')
    overlay = tmp_path / 'camera install'
    if valid_overlay:
        overlay.mkdir()
        (overlay / 'local_setup.bash').write_text('export ORBO_TEST_OVERLAY=camera\n')
    probe = tmp_path / 'probe'
    probe.write_text('#!/bin/sh\nprintf "%s\\n" "$ORBO_TEST_OVERLAY"\n')
    probe.chmod(0o755)
    script = (ROOT / 'deploy/run-ros.sh').read_text()
    script = script.replace('/opt/ros/jazzy/setup.bash', str(base))
    script = script.replace('/opt/orbo/ros_ws/install/local_setup.bash', str(base))
    script = script.replace('/opt/orbo/.venv/bin/python', str(probe))
    path = tmp_path / 'run-ros.sh'
    path.write_text(script)
    result = subprocess.run(
        ['bash', str(path), 'pkg', 'prefix', 'camera_ros'],
        env=dict(os.environ, ORBO_CAMERA_OVERLAY=str(overlay)),
        text=True, capture_output=True, timeout=10,
    )
    if valid_overlay:
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == 'camera'
    else:
        assert result.returncode != 0
        assert 'readable absolute camera install directory' in result.stderr

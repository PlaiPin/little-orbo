"""Exercise installer refusal/copy and overlay behavior without root or hardware."""

import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MOCK = r'''#!/usr/bin/env python3
import json, os, pathlib, shutil, sys
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
    if not os.getenv('ORBO_TEST_FINISH'):
        sys.exit(42)
    # Fake only the environment/toolchain, keeping real copy/config decisions.
    directory = pathlib.Path('.venv/bin')
    directory.mkdir(parents=True, exist_ok=True)
    for tool in ('pip', 'uv', 'python'):
        target = directory / tool
        if not target.exists():
            target.symlink_to(pathlib.Path(sys.argv[0]).resolve())
elif name == 'install':
    paths = []
    mode = 0o755
    index = 0
    while index < len(args):
        value = args[index]
        if value in ('-m', '-o', '-g'):
            if value == '-m': mode = int(args[index + 1], 8)
            index += 2
        elif value == '-d': index += 1
        else:
            paths.append(pathlib.Path(value))
            index += 1
    if '-d' in args:
        for path in paths: path.mkdir(parents=True, exist_ok=True)
    else:
        for source in paths[:-1]:
            target = paths[-1] / source.name if paths[-1].is_dir() else paths[-1]
            shutil.copyfile(source, target)
            target.chmod(mode)
elif name == 'systemctl':
    if args[0] == 'is-active':
        sys.exit(0 if os.getenv('ORBO_TEST_ACTIVE') == args[-1] else 3)
'''


@pytest.fixture
def installer(tmp_path):
    source = tmp_path / 'source'
    (source / 'deploy').mkdir(parents=True)
    target = tmp_path / 'target'
    target.mkdir()
    (tmp_path / 'os-release').write_text('ID=ubuntu\nVERSION_ID=24.04\n')
    (tmp_path / 'ros-setup.bash').touch()
    (tmp_path / 'systemd-running').mkdir()
    (tmp_path / 'systemd-units').mkdir()
    script = (ROOT / 'deploy/install-pi.sh').read_text()
    for old, new in {
        '/etc/os-release': tmp_path / 'os-release',
        '/opt/ros/jazzy/setup.bash': tmp_path / 'ros-setup.bash',
        '/opt/orbo': target,
        '/etc/orbo': tmp_path / 'config',
        '/var/lib/orbo': tmp_path / 'state',
        '/run/systemd/system': tmp_path / 'systemd-running',
        '/etc/systemd/system': tmp_path / 'systemd-units',
        '/tmp/orbo-runtime-requirements.txt': tmp_path / 'requirements.txt',
    }.items():
        script = script.replace(old, str(new))
    path = source / 'deploy/install-pi.sh'
    path.write_text(script)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('id', 'uname', 'apt-get', 'install', 'useradd', 'usermod', 'chown', 'systemctl', 'python3.12'):
        tool = bin_dir / name
        tool.write_text(MOCK)
        tool.chmod(0o755)
    calls = tmp_path / 'calls.jsonl'

    def run(*args, plan='', finish=False, active=''):
        result = subprocess.run(
            ['bash', str(path), *args], capture_output=True, text=True, timeout=20,
            env=dict(os.environ, PATH=f'{bin_dir}:{os.environ["PATH"]}',
                     ORBO_TEST_CALLS=str(calls), ORBO_TEST_APT_PLAN=plan,
                     ORBO_TEST_FINISH='1' if finish else '', ORBO_TEST_ACTIVE=active),
        )
        invoked = [json.loads(line) for line in calls.read_text().splitlines()]
        return result, invoked

    return source, target, run


def test_active_service_refuses_upgrade_before_apt_or_copy(installer):
    source, target, run = installer
    result, calls = run(active='orbo-control')
    assert result.returncode == 1
    assert 'Stop orbo-control' in result.stderr
    assert not any(call[0] == 'apt-get' for call in calls)
    assert not (target / 'deploy').exists()


@pytest.mark.parametrize('systemd', [False, True])
def test_upgrade_preserves_configuration_and_data_and_systemd_is_opt_in(installer, systemd):
    source, target, run = installer
    root = source.parent
    # Public deployment files plus a harmless stand-in for the network bootstrap.
    for name in ('deploy/profiles/pi.toml', 'deploy/profiles/booster-k1.toml', 'deploy/profiles/embodied.toml',
                 'ros_ws/src/orbo_bringup/config/camera.template.yaml',
                 'deploy/systemd/orbo-runtime.service'):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('NEW_PUBLIC_TEMPLATE')
    (source / 'tools').mkdir()
    (source / 'tools/bootstrap-agent.py').write_text('')
    (root / 'config').mkdir()
    (root / 'state').mkdir()
    preserved = [root / 'config' / name for name in
                 ('runtime.toml', 'booster-k1.toml', 'embodied.toml', 'camera.yaml', 'calibration.yaml', 'agent.env', 'camera.env')]
    preserved += [root / 'state/journal.db', root / 'config/credentials/NEARAI_API_KEY',
                  root / 'config/credentials.env']
    for path in preserved:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('EXISTING_OWNER_DATA')
    result, calls = run(*(['--install-systemd'] if systemd else []), finish=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(p.read_text() == 'EXISTING_OWNER_DATA' for p in preserved)
    assert (root / 'systemd-units/orbo-runtime.service').exists() is systemd
    assert (['systemctl', 'daemon-reload'] in calls) is systemd
    assert not any(c[:2] in (['systemctl', 'start'], ['systemctl', 'enable']) for c in calls)
    builds = [c for c in calls if c[:3] == ['python', '-m', 'colcon']]
    assert len(builds) == 1


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
    script = script.replace('exec ros2', 'exec ' + str(probe))
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

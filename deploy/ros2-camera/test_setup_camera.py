"""Failure-path tests using fake Pi tools; never runs APT or accesses a camera.

Run: python3 -m unittest discover -s deploy/ros2-camera -p 'test_*.py' -v
The production script is copied into a temporary fixture; only absolute host
paths and the root-user guard are replaced to permit portable, isolated tests.
"""

import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


SCRIPT = Path(__file__).with_name("setup-camera.sh")
COMMIT = "9e94b5b6d6cacbe77dc893d54735f06b4a2fd16f"
MOCK_TOOL = r'''#!/usr/bin/env python3
import json, os, pathlib, shlex, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
root = pathlib.Path(os.environ['FIXTURE'])
with (root / 'calls.jsonl').open('a') as out:
    out.write(json.dumps([name, *args]) + '\n')
if name == 'uname':
    print('Linux' if args == ['-s'] else 'aarch64')
elif name in ('realpath', 'readlink'):
    print(pathlib.Path(args[-1]).resolve())
elif name == 'rpicam-hello':
    print('libcamera v0.7.1' if args == ['--version'] else
          ('No cameras available!' if os.getenv('NO_CAMERA') else '0 : ov5647 [2592x1944]'))
elif name == 'timeout':
    sys.exit(subprocess.call(args[1:]))
elif name == 'sudo':
    sys.exit(subprocess.call(args))
elif name == 'apt-get':
    if '-s' in args and os.getenv('APT_CAMERA_CHANGE'):
        print('Inst ros-jazzy-libcamera (0.2 Ubuntu)')
elif name == 'pkg-config':
    if '--exists' in args:
        sys.exit(1 if os.getenv('MISSING_DEV') else 0)
    elif '--modversion' in args:
        print('0.7.1')
    elif '--variable=libdir' in args or '--variable=pcfiledir' in args:
        print(root / 'lib')
    elif '--cflags' in args:
        print(shlex.quote('-I' + str(root / 'include')) + ' -lcamera')
elif name == 'g++':
    probe = pathlib.Path(args[args.index('-o') + 1])
    probe.write_text('#!/bin/sh\nprintf "%s\\n" "${PROBE_VERSION:-0.7.1}"\n')
    probe.chmod(0o755)
elif name == 'ldd':
    mismatch = (os.getenv('PROBE_MISMATCH') and args[0].endswith('/probe')) or (
        os.getenv('COMPONENT_MISMATCH') and args[0].endswith('libcamera_component.so'))
    lib = root / ('wrong-lib' if mismatch else 'lib')
    for stem in ('libcamera', 'libcamera-base'):
        print(f'{stem}.so.0.7 => {lib}/{stem}.so.0.7 (0x1234)')
elif name == 'git':
    if args[0] == 'clone':
        (pathlib.Path(args[-1]) / '.git').mkdir(parents=True)
    elif args[2:4] == ['rev-parse', 'HEAD']:
        print(os.getenv('GIT_COMMIT', '9e94b5b6d6cacbe77dc893d54735f06b4a2fd16f'))
    elif args[2] == 'status':
        if os.getenv('DIRTY_SOURCE'):
            print(' M src/CameraNode.cpp')
elif name == 'colcon':
    install = pathlib.Path.cwd() / 'install'
    (install / 'camera_ros' / 'lib').mkdir(parents=True)
    (install / 'camera_ros' / 'lib' / 'libcamera_component.so').touch()
    (install / 'local_setup.bash').write_text('export ROS_DISTRO=jazzy\n')
elif name == 'ros2':
    if args[:2] == ['pkg', 'prefix']:
        print(root / 'work space' / 'install' / 'camera_ros')
    elif args[:1] == ['run']:
        print('MOCK publisher launched')
else:
    sys.exit('Unhandled mock tool: ' + name)
'''


class SetupCameraTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="camera-script-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.workspace = self.root / "work space"
        for directory in ("bin", "lib", "wrong-lib"):
            (self.root / directory).mkdir()
        for directory in ("lib", "wrong-lib"):
            for name in ("libcamera.so.0.7", "libcamera-base.so.0.7"):
                (self.root / directory / name).touch()
        (self.root / "os-release").write_text('ID=ubuntu\nVERSION_ID="24.04"\n')
        (self.root / "model").write_bytes(b"Raspberry Pi 5 Model B\0")
        (self.root / "ros-setup.bash").write_text("export ROS_DISTRO=jazzy\n")
        source = SCRIPT.read_text()
        for original, replacement in (
            ("/etc/os-release", self.root / "os-release"),
            ("/proc/device-tree/model", self.root / "model"),
            ("/opt/ros/jazzy/setup.bash", self.root / "ros-setup.bash"),
        ):
            source = source.replace(original, str(replacement))
        source = source.replace('[[ "$EUID" != 0 ]]', "[[ 1 != 0 ]]")
        self.script = self.root / "setup-camera.sh"
        self.script.write_text(source)
        for name in ("uname", "realpath", "readlink", "rpicam-hello", "timeout",
                     "sudo", "apt-get", "pkg-config", "g++", "ldd", "git", "colcon", "ros2"):
            tool = self.root / "bin" / name
            tool.write_text(MOCK_TOOL)
            tool.chmod(0o755)
        self.env = dict(os.environ, FIXTURE=str(self.root),
                        PATH=f"{self.root / 'bin'}:{os.environ['PATH']}", ROS_DISTRO="jazzy")

    def run_script(self, *args, **environment):
        result = subprocess.run(
            ["bash", str(self.script), "--workspace", str(self.workspace), *args],
            env=dict(self.env, **environment), text=True, capture_output=True, timeout=30)
        calls = self.root / "calls.jsonl"
        self.calls = [json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []
        self.output = result.stdout + result.stderr
        return result.returncode

    def assert_no_build_or_start(self):
        self.assertFalse(any(call[0] == "colcon" or call[:2] == ["ros2", "run"] for call in self.calls))

    def test_setup_builds_and_starts(self):
        self.assertEqual(self.run_script(), 0, self.output)
        self.assertEqual((self.workspace / "camera_ros.commit").read_text().strip(), COMMIT)
        launch = next(call for call in self.calls if call[:2] == ["ros2", "run"])
        self.assertIn("width:=1280", launch)
        self.assertIn("frame_id:=camera_optical_frame", launch)

    def test_check_only_does_not_install_build_or_start(self):
        self.assertEqual(self.run_script("--check-only"), 0, self.output)
        self.assertFalse(any(call[0] in ("apt-get", "sudo", "git") for call in self.calls))
        self.assert_no_build_or_start()

    def test_build_only_then_start_only(self):
        self.assertEqual(self.run_script("--build-only"), 0, self.output)
        self.assertFalse(any(call[:2] == ["ros2", "run"] for call in self.calls))
        (self.root / "calls.jsonl").unlink()
        self.assertEqual(self.run_script("--start-only", "--width", "640", "--height", "480"), 0, self.output)
        self.assertFalse(any(call[0] in ("apt-get", "git", "colcon", "g++", "pkg-config") for call in self.calls))
        launch = next(call for call in self.calls if call[:2] == ["ros2", "run"])
        self.assertIn("width:=640", launch)

    def test_camera_dependency_change_is_blocked(self):
        self.assertNotEqual(self.run_script(APT_CAMERA_CHANGE="1"), 0)
        self.assertIn("APT would modify", self.output)
        self.assertFalse(any(call[:2] == ["apt-get", "install"] for call in self.calls))
        self.assert_no_build_or_start()

    def test_missing_camera_stops_before_apt(self):
        self.assertNotEqual(self.run_script(NO_CAMERA="1"), 0)
        self.assertIn("OV5647 was not detected", self.output)
        self.assertFalse(any(call[0] == "apt-get" for call in self.calls))

    def test_missing_development_files(self):
        self.assertNotEqual(self.run_script("--check-only", MISSING_DEV="1"), 0)
        self.assertIn("development files are missing", self.output)
        self.assert_no_build_or_start()

    def test_probe_library_mismatch(self):
        self.assertNotEqual(self.run_script(PROBE_MISMATCH="1"), 0)
        self.assertIn("libcamera mismatch", self.output)
        self.assert_no_build_or_start()

    def test_metadata_runtime_version_mismatch(self):
        self.assertNotEqual(self.run_script(PROBE_VERSION="0.2.0"), 0)
        self.assertIn("versions differ", self.output)
        self.assert_no_build_or_start()

    def test_built_component_library_mismatch(self):
        self.assertNotEqual(self.run_script(COMPONENT_MISMATCH="1"), 0)
        self.assertIn("libcamera mismatch", self.output)
        self.assertFalse(any(call[:2] == ["ros2", "run"] for call in self.calls))

    def test_existing_different_revision_is_preserved(self):
        (self.workspace / "src" / "camera_ros" / ".git").mkdir(parents=True)
        self.assertNotEqual(self.run_script(GIT_COMMIT="another-commit"), 0)
        self.assertIn("differs from the pinned commit", self.output)
        self.assert_no_build_or_start()
        self.assertFalse(any(call[:2] == ["git", "clone"] for call in self.calls))

    def test_dirty_source_is_preserved(self):
        self.assertNotEqual(self.run_script(DIRTY_SOURCE="1"), 0)
        self.assertIn("contains changes", self.output)
        self.assert_no_build_or_start()

    def test_start_without_build(self):
        self.assertNotEqual(self.run_script("--start-only"), 0)
        self.assertIn("No workspace installation found", self.output)
        self.assert_no_build_or_start()

    def test_invalid_arguments(self):
        for arguments in (("--width", "0"), ("--camera", "-1"), ("--width",),
                          ("--unknown",), ("--check-only", "--build-only")):
            with self.subTest(arguments=arguments):
                self.assertNotEqual(self.run_script(*arguments), 0)
                self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()

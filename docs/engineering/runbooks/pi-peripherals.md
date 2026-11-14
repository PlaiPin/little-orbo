# Pi teleoperation, ArduCam and WaveShare audio

For foreground ROS commands, start with [PI5_QUICKSTART.md](../../../PI5_QUICKSTART.md).
The systemd examples below are optional and require `--install-systemd`.

The software supports ROS 2 teleoperation, local camera topics, and microphone/speaker operation on the Pi 5 / Ubuntu 24.04 / Jazzy target. Physical commissioning is still pending. The original `Software/Teleoperate` program remains a separate legacy UDP/text application; it must not run alongside the production ROS firmware. The new operator path is `Joy → brokered teleop session → stamped velocity gate → ros2_control → micro-ROS → XL430`.

## Confirmed hardware

- **Camera:** ArduCam **B0033 + B0087**, owner label `x0017USFBJ`; 5 MP **OV5647**, fixed focus, CSI. This is identified by the [manufacturer's product entry](https://www.arducam.com/b0033b0087-arducam-5mp-ov5647-mini-camera-video-module-with-15-pin-1-0mm-pitch-to-22-pin-0-5mm-and-15pin-to-15pin-1-0mm-ribbon-cable-for-raspberry-pi-model-a-b-b-pi-2-pi-3-pi-4-and-pi-zero.html). The kit's 15-to-22-pin cable is the Pi 5 connection; inspect cable orientation and selected CAM/DISP connector with power removed.
- **Audio:** WaveShare **USB TO AUDIO** with an **8Ω 5W speaker**, supplied [product reference](https://www.amazon.com/USB-Sound-Card-Speaker-Raspberry/dp/B0CXJ2RTWJ/). The corresponding [WaveShare board documentation](https://www.waveshare.com/wiki/USB_TO_AUDIO) confirms an onboard microphone and speaker header. Select its actual enumerated USB input/output device names; no I²S driver is needed for this USB device.

No sensor identity or connection type needs to be requested again. Remaining device-specific inputs are measured enumeration, CAM0/CAM1 choice, camera intrinsics/mount transform, audio levels and actual teleop control mapping/stop measurements.

## Installation and ownership

`deploy/install-pi.sh --camera-stack existing` is the default for a Pi whose camera already works with `rpicam-hello`. It installs `joy`, `v4l2_camera`, image transport plugins, `v4l-utils` and `alsa-utils`, but does not request ROS `camera_ros`/libcamera packages. It checks APT's proposed changes and refuses changes to camera-stack packages. It gives the `orbo` service account audio/video/input/dialout access, optionally installs **seven** disabled systemd units with `--install-systemd` and copies an initially disabled `/etc/orbo/camera.yaml`. Existing runtime/camera configurations are preserved on reinstall; merge the new template fields when upgrading. The Pi uses native Jazzy, colcon and a Python virtual environment; it does not use Pixi.

For a working rpicam camera, follow the [camera build guide](../../../deploy/ros2-camera/README.md) to compile the pinned camera_ros against the existing libraries. Use a shared workspace so the `orbo` service can read its symlink install:

```bash
# From ~/little-orbo, as your normal Pi user, with other camera capture stopped:
sudo install -d -m 0755 -o "$USER" -g "$(id -gn)" /opt/orbo-camera
(umask 022; bash deploy/ros2-camera/setup-camera.sh --build-only --workspace /opt/orbo-camera)
printf '%s\n' 'ORBO_CAMERA_OVERLAY=/opt/orbo-camera/install' | sudo tee /etc/orbo/camera.env >/dev/null
sudo chmod 0644 /etc/orbo/camera.env
sudo -u orbo env ROS_DOMAIN_ID=42 ORBO_CAMERA_OVERLAY=/opt/orbo-camera/install \
  /opt/orbo/deploy/run-ros.sh pkg prefix camera_ros
```

The last command must print `/opt/orbo-camera/install/camera_ros`. Matching headers and pkg-config metadata must come from the working rpicam installation; if the script refuses missing or mismatched development files, resolve that provenance instead of installing a different libcamera. The camera unit reads `/etc/orbo/camera.env`; only that service adds the camera overlay. The core installation can be tested independently if matching camera headers are not yet available.

For a clean target deliberately using ROS-distributed libcamera, `--camera-stack ros` requests the apt camera stack. Historical ARM64 staging used camera_ros 0.7.0 and ROS libcamera 0.7.2; those versions and PiSP tuning files do not establish compatibility with a different working rpicam installation. Do not change an already-working camera stack to reproduce that historical staging environment.

Run only one velocity gate. `orbo-teleop.service` and `orbo-navigation.service` conflict in systemd. The runtime rejects simultaneous `teleop_enabled` and `navigation_enabled`. Separate `control/teleop_lease` and `control/lease` topics prevent a Nav2 lease from granting teleop authority. ROS DDS remains the trusted robot network boundary; do not expose it to untrusted clients. Camera topics are visible to participants in that ROS domain, not protected by the owner Unix socket.

## Teleoperation

First commission wheel geometry, polarity, speed bounds and physical stopping as described in `operator.md`. Keep the controller inactive until those steps are complete. In `/etc/orbo/runtime.toml`:

```toml
[robot]
# Retain the existing id/calibration/flipper fields.
teleop_enabled = true
navigation_enabled = false

[robot.teleop]
qualified = true
linear_axis = 0
angular_axis = 1
deadman_button = 0
linear_scale = 0.25
angular_scale = 0.25
evidence_record = "YOUR_MEASURED_TELEOP_QUALIFICATION_RECORD"
```

These axis/button indices are the bundled **keyboard** mapping. For a gamepad, observe `/orbo/teleop/joy`, measure its actual axis directions/button index and replace these values. Negative scale reverses an axis; magnitude is limited to one. Linear/angular speeds are additionally limited by the measured per-wheel speed and geometry, including combined turn/translation demands.

Start the commissioned control stack and runtime, then explicitly activate the hardware/controller per `operator.md`. For keyboard control:

```sh
sudo systemctl start orbo-teleop
# In a real interactive terminal on the Pi (SSH -t also works):
sudo -u orbo bash -c 'source /opt/ros/jazzy/setup.bash; source /opt/orbo/ros_ws/install/local_setup.bash; export ROS_DOMAIN_ID=42; exec /opt/orbo/.venv/bin/orbo-teleop-keyboard'
```

Alternatively, from an authorized interactive shell:

```sh
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
export ROS_DOMAIN_ID=42
/opt/orbo/.venv/bin/orbo-teleop-keyboard
```

Use `W/S` for forward/reverse, `A/D` for rotation, `SPACE` for neutral and `Q` to exit. A motion key lasts **150 ms** and requires keyboard repeat to continue; the initial keyboard-repeat delay can produce a brief pause. A neutral heartbeat continues while the terminal is running. Exiting or losing the terminal stops those heartbeats. This terminal only publishes input; it cannot arm the robot by itself.

For a gamepad, stop the keyboard/service and launch the single gate plus `joy_node` explicitly from the sourced environment:

```sh
sudo systemctl stop orbo-teleop orbo-navigation
ros2 run joy joy_enumerate_devices
/opt/orbo/deploy/run-ros.sh launch orbo_bringup teleop.launch.py \
  calibration:=/etc/orbo/calibration.yaml input:=joystick \
  device_name:='THE_ACTUAL_ENUMERATED_DEVICE_NAME'
```

The driver uses nonsticky buttons and 50 Hz repeats. This preserves a held deadman without requiring axis movement. Device-disconnect behavior still needs physical qualification. Never run keyboard and gamepad publishers together: session admission and monitoring require exactly one Joy publisher.

From another terminal, use the private owner CLI to start an episode and grant `robot.command` to `orbo` with a future expiry (see `operator.md`). Keep the deadman released at session admission. Then request a bounded session:

```sh
sudo -u orbo /opt/orbo/.venv/bin/orbo request robot.teleop \
  --args '{"episode_id":"EPISODE_ID","key":"UNIQUE_REQUEST_KEY","duration":20}'
# In another terminal, whenever needed:
sudo -u orbo /opt/orbo/.venv/bin/orbo request stop
```

The request remains active for up to its **1–30 second** budget, including a stop margin. Deadman release commands zero velocity; it does not revoke the remaining owner session. A new press can move again within that explicitly authorized session. Missing/stale/reordered/malformed input, owner stop, grant expiry, controller change or session expiry ends authority and requests disarm. Old stopped epochs cannot be renewed. There is no automatic session restart or model-accessible raw-velocity tool. Expiry remains enforced by the gate/controller/MCU if the process disappears.

A normal result requires a fresh MCU disarmed acknowledgment. This is controller evidence, not an independent measurement of physical cessation. Unknown outcomes remain subject to the existing broker recovery rules. Manual teleop does not run Nav2's collision monitor; the operator must retain line of sight and use the independently qualified cutoff. The camera is an aid, not an obstacle/stop guarantee.

## OV5647 camera

Use the Pi CSI connector and OV5647 kernel overlay, not the USB V4L2 profile. The Raspberry Pi [camera installation documentation](https://www.raspberrypi.com/documentation/accessories/camera-software.html) identifies `dtoverlay=ov5647`; for connector CAM0, its explicit form is `dtoverlay=ov5647,cam0`. On Ubuntu inspect `/boot/firmware/config.txt` and the actual CAM0/CAM1 connection before editing; do not blindly append duplicate overlays. This project does not modify boot firmware/configuration automatically or enable legacy `start_x` camera mode.

After the correct sensor is detected, source Jazzy and the camera overlay (if used), then run `ros2 run camera_ros camera_node --ros-args -p camera:=0` in domain 42 to inspect the driver's enumerated name and image capability. Stop it before starting the service. This explicit diagnostic can start local capture; it does not record or upload frames. Set `/etc/orbo/camera.yaml`:

```yaml
enabled: true
driver: libcamera
camera: "EXACT_ENUMERATED_OV5647_NAME"
width: 640
height: 480
frame_id: camera_optical_frame
calibration_file: "" # Set to an existing absolute calibration YAML after calibration.
```

The initial 640×480 request is a processing profile, not an assertion about full sensor field of view. The driver requests RGB888; inspect actual dimensions, crop and frame rate. The monitor rejects oversized/invalid layouts rather than treating them as qualified. No optical mount transform is fabricated. Add the template's `mount` block only after measuring all six optical-frame pose values and recording evidence; it publishes `base_link → camera_optical_frame` on `/tf_static`.

```sh
sudo systemctl start orbo-camera
sudo -u orbo env ROS_DOMAIN_ID=42 /opt/orbo/deploy/run-ros.sh topic hz /orbo/camera/image_raw
sudo -u orbo env ROS_DOMAIN_ID=42 /opt/orbo/deploy/run-ros.sh topic echo /orbo/camera/diagnostics
# To stop capture:
sudo systemctl stop orbo-camera
```

For viewing, use `rqt_image_view` on the trusted ROS network (install `ros-jazzy-rqt-image-view` on the viewer). Topics are `/orbo/camera/image_raw`, `/orbo/camera/camera_info` and `/orbo/camera/diagnostics`; image_transport supplies compressed transport when its plugins are installed. Diagnostics distinguish absent/stale frames, missing/mismatched intrinsics and valid incoming frame/CameraInfo metadata. A green diagnostic does not establish physical calibration or navigation suitability.

The shipped integration does **not** save images, send them to NEAR, add them to Commons exports or infer depth. OV5647 is monocular RGB; the existing Nav2 profile still requires its separately qualified LaserScan source. If the camera is not enumerated, record `uname -r`, boot firmware/overlay, ROS/libcamera versions and driver logs. An Ubuntu 24.04 kernel/firmware limitation must be resolved and tested on the intended Pi, not hidden by a fixture or an automatic OS change.

## WaveShare microphone and speaker

The runtime already uses PortAudio/ALSA with separately selectable input/output devices, 16 kHz mono WAV capture, 24 kHz mono PCM speech, bounded buffers and interruption. Audio is separate from ROS image/motion traffic. No raw microphone ROS topic is broadcast. The new local check executable needs no provider keys and does not save or upload audio:

```sh
sudo -u orbo /opt/orbo/deploy/run-ros.sh run orbo_runtime audio_check list
arecord -l
aplay -l
sudo -u orbo /opt/orbo/deploy/run-ros.sh run orbo_runtime audio_check check \
  --input 'ACTUAL_UNIQUE_USB_INPUT_NAME' --output 'ACTUAL_UNIQUE_USB_OUTPUT_NAME'
sudo -u orbo /opt/orbo/deploy/run-ros.sh run orbo_runtime audio_check tone \
  --output 'ACTUAL_UNIQUE_USB_OUTPUT_NAME' --seconds 2
sudo -u orbo /opt/orbo/deploy/run-ros.sh run orbo_runtime audio_check record \
  --input 'ACTUAL_UNIQUE_USB_INPUT_NAME' --seconds 3
```

`check` tests sample-format support without capturing/playing. `tone` plays a low-amplitude 440 Hz tone; start with low hardware/mixer volume. `record` captures transiently and reports sample count, RMS/peak dBFS and clipping, then discards the bytes. Neither claims the speaker was heard or a person understood speech. Resolve ambiguous names and validate after unplug/replug; card indices can change. If direct hardware access does not support 16/24 kHz, qualify an ALSA `plug` device for sample conversion and select that named endpoint explicitly.

Put the actual unique names in `[live].input_device` and `[live].output_device`; keep `sample_rate = 24000`. OpenAI Live requires the embodied profile, NEAR delegation, provider credentials and explicit grants. Follow the [Live audio guide](../../../deploy/audio/README.md) for simultaneous input/output checks. This board is not assumed to provide acoustic echo cancellation; keep `aec_qualified = false` until the actual audio path is qualified. Test clipping, feedback and interruption at the intended volume.

## Verification and acceptance

Host tests cover deadman/combined wheel bounds, invalid/stale inputs, stopped-epoch fencing, camera configuration/freshness/intrinsics and audio-level reporting. The ARM64 lane uses real ROS Joy/TwistStamped/ControlSession and Image/CameraInfo/DiagnosticArray traffic with synthetic inputs; it tests wrong-source lease refusal, deadman release, input loss and camera stream loss. A real pseudo-terminal drives the keyboard node to check motion-key expiry and clean exit. Installed drivers and launch configuration are checked without a physical sensor. Logs are under `artifacts/test-evidence/`.

Remaining hardware checks: OV5647 enumeration/optics/fps/mount/intrinsics/unplug behavior; WaveShare mic/speaker levels/echo/device reconnection; actual keyboard/gamepad mapping and measured motion/stop/deadman/network-loss behavior. Repeat the Live audio checks with camera, ROS control and logging active. Live provider/Commons checks remain a separate lane in `../pending-validation.md`.

# Orbo Pi / ESP32 operator runbook

For the primary foreground `ros2 run`/`ros2 launch` workflow and migration from
the old installer, use [PI5_QUICKSTART.md](../../../PI5_QUICKSTART.md). This
runbook also covers optional systemd supervision; its service commands require
installation with `--install-systemd`.

Target: Raspberry Pi 5 (8 GB), Ubuntu 24.04 ARM64, ROS 2 Jazzy; ESP32-S3 with two XL430-W250-T wheels and two independently calibrated PWM flippers. Read `implementation-status.md` for what has actually been tested. Compilation and synthetic DDS tests do not establish physical safety, acoustic performance or navigation accuracy.

## 1. Inventory and deployment inputs

Record the Pi serial/OS, ESP32-S3 board/module/flash size, native USB wiring, power supply, motor IDs and bus voltage, wheel sign and mechanical dimensions, microphone/speaker identifiers, and independent emergency power cutoff. Keep wheels clear of the surface for initial checks. Native USB CDC interface 0 carries micro-ROS; interface 1 carries logs. USB-UART bridges do not substitute for the native USB connection. The legacy Dynamixel UART remains separate from USB/logging.

Supply measured values in `ros_ws/src/orbo_description/config/calibration.template.yaml`. Do not set a qualification flag to make a launch error disappear. Record measurement method, person, date, firmware hash, encoder signs, stop latency, lost-feedback behavior and limits in the referenced evidence record. The template intentionally has no wheel dimensions or invented sensor specifications. Navigation additionally needs an actual LaserScan driver, calibrated sensor transform, surveyed map and named waypoints, localization, obstacle and edge controls appropriate to the operating surface. A table needs physical edge containment; a planar lidar alone does not establish cliff detection.

The owner selected Booster K1. Its real official-SDK adapter and on-robot worker are described in `integrations/booster_k1/README.md`; the Pi installer supplies a separate disabled K1 runtime service/profile. Follow that commissioning procedure and preserve vendor balance/motion control. Actual firmware, serial, device access and physical outcomes still require qualification. The `RobotAdapter` contract remains shared across both robots.

## 2. Build firmware without flashing

Install the ESP-IDF v6.0.3 release with its submodules and ESP32-S3 tools from the official release. `dependencies.source-lock.json` records the release commit and every resolved micro-ROS source commit. `dependencies.lock` pins managed IDF components. Use the IDF environment's Python, colcon and ROS build Python dependencies; the successful host build used the IDF Python 3.14 environment, colcon-common-extensions, catkin-pkg, lark, empy 3.3.4 and the versions in `host-build-requirements.lock`. Install the lock into that dedicated IDF Python environment with `python -m pip install --require-hashes -r firmware/orbo/host-build-requirements.lock`.

From the repository root:

```bash
source /path/to/esp-idf-v6.0.3/export.sh
./tools/build-firmware.sh
```

The bootstrap fetches exact commits, applies the checked-in build patch and copies the authoritative `orbo_embedded_interfaces` package. It refuses an existing checkout at another revision. It never flashes. Outputs are in `build-locked/`; `flash_args` describes offsets. The checked-in sdkconfig now permits lifted-bench wheel arming at 1.2 rad/s with the 115200-baud XL430 bus; flippers remain disabled. This differs from the generic wheel-disabled defaults and historical uncommissioned artifacts. Follow the current firmware/orbo/README.md for the actual board/flash profile and flashing procedure. Save a separate commissioned sdkconfig and its hash; rebuild after changes. Preserve the no-UART-console/two-USB-CDC configuration so logs do not drive the servo UART.

After confirming physical wiring, power and board settings, an operator can flash explicitly:

```bash
idf.py -C firmware/orbo -B "$PWD/build-locked" -p /dev/serial/by-id/ACTUAL_BOOTLOADER_DEVICE flash
```

Bootloader USB and application CDC identities can differ. Re-identify both CDC interfaces after flashing. No firmware in this repository arms wheels at boot, reconnect, clock sync or watchdog recovery. Arm requires a fresh explicit owner epoch. Do not treat a successful serial write as encoder feedback.

## 3. Install the Pi runtime and agent

On the Pi with Ubuntu 24.04 ARM64 and ROS 2 Jazzy already installed, use the current source checkout after the deployment files are committed and pushed. Historical archives under `artifacts/` are not the current source. This workflow uses native apt ROS, colcon and a Python virtual environment; no Pixi is required. Run as your normal user:

```bash
git clone https://github.com/PlaiPin/little-orbo.git ~/little-orbo
cd ~/little-orbo
# For an existing clone, use git pull --ff-only instead of cloning.
sudo ./deploy/install-pi.sh --camera-stack existing --install-systemd
```

This installs `/opt/orbo`, a service user, the frozen Python environment, the Orbo ROS packages and the micro-ROS Agent from `deploy/agent.source-lock.json`. Builds use two compiler jobs and sequential colcon packages to bound Pi memory use. The default camera mode preserves working camera packages and refuses an APT plan that would modify them; build the camera overlay separately as described in the peripherals runbook. `--camera-stack ros` is an explicit alternative for a target deliberately selecting apt's ROS camera stack. The Agent uses a pinned compatible spdlog rather than Ubuntu's incompatible newer fmt interface. Jazzy Fast DDS/CDR and OS packages come from apt; preserve `dpkg-query -W` output with each deployment. `uv.lock` is a Python dependency lock, not an apt snapshot. Private validation directories, credentials, SDK/build directories, Pixi environments and generated artifacts are excluded from source copying. Reinstallation preserves existing configuration and does not restart running services; stop the robot services before updating an active installation.

With `--install-systemd`, services are installed but not started or enabled. The
default installer does not install units. Existing units remain on upgrade; stop
and disable them before switching to foreground launches. Set `/etc/orbo/agent.env` to the actual application transport interface:

```text
ORBO_SERIAL_DEVICE=/dev/serial/by-id/ACTUAL_CDC0_DEVICE
```

Keep this file root-owned and mode 0600. Copy the measured calibration to `/etc/orbo/calibration.yaml` and set its read access for `orbo`. Copy a surveyed map and its referenced image when navigation is commissioned. Edit `/etc/orbo/runtime.toml`: attention pulses must match the independently tested firmware bounds; navigation and audio remain disabled until configured. A runtime restart revokes robot command grants and leases, requiring explicit reauthorization.

### Native Pi checks before hardware

Run the installed package checks and owner IPC smoke first:

```bash
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
export ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY
ros2 run orbo_runtime runtime diagnostics
orbo_demo_dir=$(mktemp -d /tmp/orbo-demo-XXXXXX)
ros2 run orbo_runtime runtime demo --database "$orbo_demo_dir/demo.db"
sudo -u orbo bash -c 'source /opt/ros/jazzy/setup.bash; source /opt/orbo/ros_ws/install/local_setup.bash; exec ros2 run orbo_runtime installed_smoke'
```

The smoke helper uses isolated domain 94, creates a temporary journal/socket, and does not arm hardware or call cloud providers. `diagnostics` is a package/environment inventory, not a device health check.

For Python and synthetic ROS tests, create a separate test environment using the checked-in uv lock. Do not replace the production virtual environment with a development sync:

```bash
cd ~/little-orbo
python3.12 -m venv --system-site-packages ~/orbo-test-venv
/opt/orbo/.venv/bin/uv export --frozen --all-extras --group dev --no-emit-project \
  -o /tmp/orbo-test-requirements.txt
/opt/orbo/.venv/bin/uv pip install --python ~/orbo-test-venv/bin/python \
  --require-hashes -r /tmp/orbo-test-requirements.txt
/opt/orbo/.venv/bin/uv pip install --python ~/orbo-test-venv/bin/python --no-deps .
source ~/orbo-test-venv/bin/activate
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
# After building the camera overlay, include it for installed launch tests:
source /opt/orbo-camera/install/local_setup.bash
python -m pytest -q tests --ignore=tests/ros --ignore=tests/k1
ROS_DOMAIN_ID=96 ORBO_ROS_TESTS=1 python -m pytest -q tests/ros
bash firmware/orbo/host_tests/run.sh
deactivate
```

The ROS lane is software-only domain 96 and can take several minutes. Keep it separate from domain 42 hardware sessions. A passing synthetic lane does not qualify motors, camera, audio or navigation on the Pi.

### Lifted wheels: test firmware, then the complete base controller

These tests assume the current wheel-enabled firmware is already flashed. Keep the base lifted and the motor supply on. First stop application motion owners, then start only the agent:

```bash
sudo systemctl stop orbo-runtime orbo-control orbo-teleop orbo-navigation
sudo systemctl start orbo-micro-ros-agent
source /opt/ros/jazzy/setup.bash
source /opt/orbo/ros_ws/install/local_setup.bash
export ROS_DOMAIN_ID=42 RMW_IMPLEMENTATION=rmw_fastrtps_cpp
export ROS_AUTOMATIC_DISCOVERY_RANGE=SUBNET
unset ROS_LOCALHOST_ONLY
ros2 topic echo /orbo/embedded/health --once --qos-reliability best_effort
ros2 topic echo /orbo/embedded/wheel_state --once --qos-reliability best_effort
ros2 run orbo_runtime wheel_bench \
  drive --left 0.6 --right 0.6 --seconds 3
# Optional interactive direct-wheel test, after the drive exits:
ros2 run orbo_runtime wheel_bench keyboard --speed 0.6
```

Wait for stopped/disarmed encoder feedback before switching paths. For the complete base path, terminal A runs the bench control launch in the same sourced domain-42 environment:

```bash
ros2 launch orbo_bringup bench.launch.py
```

Terminal B sources the same ROS/workspace setup and domain, then runs:

```bash
ros2 run orbo_runtime base_bench drive --linear 0.02 --seconds 3
# Only after the short test passes:
ros2 run orbo_runtime base_bench drive --linear 0.02 --seconds 30
ros2 run orbo_runtime base_bench keyboard --speed 0.6
```

This exercises stamped velocity, the velocity gate, ros2_control, the agent and real MCU encoder feedback. The helper activates controllers and requests a fresh arm. `bench-cad.yaml` is nominal geometry for lifted testing; it does not qualify floor driving. Stop the helper before Ctrl-C in terminal A. Do not start the application runtime/teleop gate alongside this bench path. Once measured calibration and teleop qualification exist, follow the [application teleop sequence](../../../ros_ws/README.md#2-pi-configure-and-start-the-robot) for the brokered full application path.

## Pi teleoperation, camera and USB audio

The [peripherals runbook](pi-peripherals.md) covers the completed ROS keyboard/gamepad teleop path, confirmed ArduCam B0033+B0087 OV5647 camera, and WaveShare USB TO AUDIO onboard mic/8Ω 5W speaker. It includes local camera/audio checks, calibration, exclusive control modes, and the two new disabled services. The original `Software/Teleoperate` is a separate legacy path.

## 4. Credentials and voice

The runtime reads credentials from the Git-ignored project `.env`. Copy the root
`.env.example`, fill `NEARAI_API_KEY` and `OPENAI_API_KEY`, and keep the real file
mode 0600. On deployed hosts use `/opt/orbo/.env` with the supplied systemd
credential drop-in. See [deployment](../../../deploy/README.md) for optional
Commons and reporting credentials. No credentials belong in TOML or argv.

OpenAI Live is the sole voice integration. Text-only profiles enable `[near]`
without opening audio devices. For voice use the embodied profile with `[near]`,
`[embodied]` and `[live]` enabled. The model is `gpt-live-1`, with verified NEAR
reasoning and vision delegated through the application. NEAR defaults to
`z-ai/glm-5.3-flash` and verifies model attestation and complete-response proofs.

Select devices and commission simultaneous 24 kHz mono PCM16 audio using the
[Live audio guide](../../../deploy/audio/README.md). The software does not provide
an acoustic echo canceller. Qualify the actual input/output path before setting
`aec_qualified = true`; test clipping, interruptions and unplug/replug behavior.

## 5. Start and explicitly authorize

```bash
sudo systemctl start orbo-micro-ros-agent
sudo systemctl start orbo-runtime
sudo -u orbo ros2 run orbo_runtime runtime diagnostics
```

Inspect `journalctl -u orbo-runtime -u orbo-micro-ros-agent` and CDC1 logs. `ROS_DOMAIN_ID=42` is the deployment domain. Use `sudo -u orbo env ROS_DOMAIN_ID=42 /opt/orbo/deploy/run-ros.sh topic echo /orbo/embedded/health` to verify fresh boot ID, epoch, time sync and unarmed state. Confirm measured per-motor feedback with torque disabled before arm. ROS DDS is a trusted local robot-control network boundary; do not expose this domain on an untrusted network. The owner API is a private mode-0600 Unix socket under a mode-0700 directory.

`orbo request --help` documents the socket client syntax. Each request is a method plus a JSON argument object. Useful methods:

| Method | Required arguments / effect |
|---|---|
| `episode.start` | Optional `profile`: `attention.v1` or `physical_task.v1`; returns `episode_id` |
| `permission.set` | `scope`, `destination`, `enabled`, `expires` (UTC Unix seconds) |
| `conversation.text` | `episode_id`, `text`; verified reasoning, no automatic playback |
| `live.start` | Optional `episode_id`; starts the configured OpenAI Live session |
| `live.close` | Closes the Live session; durable tasks remain separate |
| `live.interrupt` | Interrupts speech without cancelling a durable task |
| `stop` | Cancels the turn/capture/playback and requests controller stop; does not claim measured cessation |
| `reminder.create` | `episode_id`, unique `key`, `text`, `due_utc` |
| `reminder.list` | Current owner reminders and acknowledgment state |
| `robot.teleop` | `episode_id`, unique `key`, `duration` 1–30 seconds; owner-only deadman teleop session, no model velocity interface |
| `robot.navigate` | `episode_id`, unique `key`, surveyed `waypoint`, optional `duration` ≤30 seconds |
| `episode.end` | `episode_id`, `outcome`: completed/failed/cancelled/unknown |
| `journal` | Optional `after` sequence; bounded pages, no raw audio |

Grant only the required scopes with explicit future expiration: `robot.command` to `orbo`; `cloud.reasoning` to `near`; `cloud.voice` to `openai`; `audio.capture`, `speech.play`, `tools.execute` to `local`; `preferences.write` to `local` for preference changes. A `stop` request needs no positive motion grant. Commands have monotonic leases and MCU expiry; loss of any layer does not create authority. Unknown physical command outcomes remain unreconciled and block conflicting commands.

## 6. Commission control and navigation

Start `orbo-control` only with measured wheel calibration. Hardware and drive controller initially remain inactive. With the robot supported and an operator at the cutoff, activate hardware and the drive controller using the installed Jazzy controller-manager commands, then request a bounded action through the runtime. Activation does not arm the MCU. Observe raw encoder validity, position and velocity, wheel odometry and TF; do not infer motion from a command echo. Record sign, distance and yaw errors, per-motor read loss, controller restart and USB/agent disconnect results. Measure stop latency independently.

For Nav2, start the actual sensor driver and its calibrated `base_link` transform; verify `/tf` and `/tf_static`, `map → odom → base_link → sensor`, the scan timestamp/frame, map and localized pose. `orbo-navigation` starts Nav2 with autostart disabled. Explicitly activate localization/navigation lifecycle managers after diagnostics. Only named surveyed poses can be requested by the task coordinator. Nav2 outputs stamped velocity through the exclusive gate and ros2_control; the gate preserves source age and earlier lease expiry. The adapter waits for a fresh post-planning command before arming and records a bounded sample of Nav2 localization feedback. A Nav2 success result is controller/localization evidence, not independent proof of arrival.

Test with a contained area, external stop, obstacles, missing/stale scan, failed localization, missed goal, cancellation, process kill and MCU reboot. Do not set `navigation_enabled=true` until that profile is qualified. This repository does not claim a supplied sensor model, a cliff sensor or tested autonomy.

## 7. Measure actual Pi voice performance

Use the [Live E2E runner](../../../deploy/live-e2e/README.md) for recorded-input
provider/delegation checks and the [audio guide](../../../deploy/audio/README.md)
for the physical conversation. Test with camera, ROS control and logging active.
Retain failures and inspect Live session usage/delivery evidence. Software audio
frames do not establish acoustic latency or that the intended words were heard;
measure those independently on the device. Exercise interruption, disconnect and
permission revocation with the current deployment.

## 8. Commons review, submission and analysis

The patched contributor revision and patch order are in `integrations/trace_commons`. Build with `./tools/build-commons.sh` or extract the macOS ARM64 development artifact. The executable has not been enrolled or notarized. Use an isolated owner-local daemon configured to read only a chosen trajectory staging directory, manual review mode, and the intended account/recipient. Do not point it at a user's global agent history. Enrollment, actual upload and server deployment require owner authorization; none occurred during implementation.

The Pi runtime uses a private Unix socket. If the contributor runs on the Mac, forward only that socket over authenticated SSH. Prepare `/var/lib/orbo/commons` on the Pi as `orbo:orbo`, mode 0700; use an explicitly authorized SSH identity for that same service user and verify the Pi host key. On the Mac, substitute the actual daemon socket and host:

```bash
ssh -N -T -o ExitOnForwardFailure=yes \
  -R /var/lib/orbo/commons/daemon.sock:/ABSOLUTE/MAC/DAEMON.sock orbo@PI_HOST
```

The Pi SSH server must allow this owner-authorized stream-local forward and create the endpoint with mode 0600. Verify ownership/mode; the bridge refuses a permissive socket. Configure `[commons].socket` to that Pi endpoint. Keep the tunnel alive during review/status/submission. A dropped approval response remains unknown and is never retried automatically. This forwarding recipe has not been exercised against an actual Pi/Mac pair. Never expose the daemon as a public TCP listener.

See [minimization and recovery](commons-recovery.md) for immutable derivatives, durable revocation and targeted retrieval.

Close a task, call `export.prepare` for `attention_timing` or `physical_task_outcomes` with the intended recipient, then `export.preview`. Inspect all text and disclosure; there is no anonymity claim. `export.approve` binds the exact SHA-256, owner grant revision, recipient and expiry. Export preparation does not upload. After exact approval, `export.trajectory` returns the bounded JSONL in `trajectory_utf8`; write it mode 0600 to the isolated manual-review staging directory. Do not write it into an automatically uploading watch directory. The trajectory projection carries truthful tool calls/results and marked observations; the server's feedback carrier is not a human evaluation label.

The daemon bridge requires `commons.review` permission for preview because the daemon's privacy pipeline may contact its configured service. `commons.review` takes the owned `export_id` and daemon `entry_id`, reads every redacted preview page and verifies exact reconstruction. If redaction removes required evidence, the package is unusable for this lossless profile; do not relabel it complete. `commons.submit_reviewed` requires both reviewed body/envelope digests and a current export approval. It calls the distinct patched `approve_reviewed` method, so an old daemon refuses rather than ignoring the digest. A lost response stays `approval_unknown`; do not retry automatically. Reconcile via status and the daemon UI. `commons.revoke` invalidates local approval and requests cancellation; it cannot recall already delivered copies.

The six local patches add authorized `/v1/datasets/task-evidence` delivery. Set `[commons].origin` only to an owner-authorized server where that extension and its policy have been qualified. Give the runtime a separate `COMMONS_EXPORT_TOKEN`, and grant `commons.retrieve` to that exact origin. `consumer.retrieve` takes `purpose`, `source_digests` (approved package SHA-256 values), matching `submission_ids` (one UUID per digest), `privacy_risk` (`low` or `medium`) and optional `limit` 1–10. It returns the delivered package and reproducible analysis, explicit missing digests, and the server export identity. Guarded servers may refuse medium-risk evidence; do not weaken their policy or use a private sidecar to bypass it. Local synthetic round-trip tests do not establish live delivery.

For local export methods, `export.prepare` takes `episode_id`, `purpose`, `recipient`; `export.preview` and `export.trajectory` take `export_id`; `export.approve` also requires `sha256` (the preview's digest) and UTC `expires`. Grant `contribute` to the exact recipient first. `commons.status` takes `export_id` and `entry_id`, correlates exact trajectory hashes, and returns individual `submission_ids`, including cache timestamps. It follows source-filtered, snapshot-bound history pages; absence stays unknown. `commons.recovery_queue` lists unresolved entries and `commons.recover` reconciles them without replaying approval.

For accounting, grant `accounting.read` to `near` or `commons`. Configure `[reporting].near_organization` and its separate reporting token for `accounting.near.sync {start,end}` (timezone-aware ISO timestamps, bounded window). Configure `[commons].origin`, `[commons].account` and `COMMONS_CONTRIBUTOR_TOKEN` for `accounting.commons.sync {submission_ids}` using IDs returned by the linked history. This reads actual status, credit and ledger APIs without initiating transfers. `accounting.summary` separates providers, instruments, stages and provenance. Account snapshots and balances are non-additive. Recipient receipt and applied-usage allocation remain unknown unless independently supplied by their authority; a pipeline payout label cannot establish either.

`accounting.record` records owner assertions explicitly as such. Actual provider records must retain original IDs/provenance. Submitted, admitted, delivered, award, received credit and credit applied to usage are separate facts. OpenAI usage is billed to its separate owner account; a Commons score or grant does not establish payment or cost coverage.

## 9. Failure, restart and recovery

On any unexpected motion use the physical cutoff. Runtime `stop` is a bounded software request, not an emergency-stop certification. Restarting the Pi/runtime/agent/MCU never restores motion authority. Review `unknown` commands against actual controller/measured evidence before releasing their resources. A new owner grant, fresh lease epoch and explicit controller arm are required.

If audio/network/verification fails, inspect the failure event and provider status without logging credentials or replaying a billed request. Unacknowledged reminders silently defer; speech failure is not delivery. Distinguish MCU completed status, software audio frames and independent observations in operator notes.

For backup, stop the runtime, retain the SQLite database together with WAL state using SQLite's backup API (or checkpoint after shutdown), calibration/configuration and exact artifact hashes. Store credentials separately. Restore into a stopped runtime; recovery revokes robot command grants and invalidates leases. Verify local ownership/modes before start. Do not restore an old MCU epoch as authority.

For rollback, keep the prior source/binaries/configuration and migration schema version. Stop services, restore the known build and configuration, inspect journal compatibility, then perform explicit reauthorization. Never overwrite a live database with a file copy.

# Booster K1 vendor adapter (H-01–03)

This integration targets **Booster K1** using `boosteros==1.2.5` and
`booster-robotics-sdk-python==1.6.3`. The ARM64 dependency lock includes
distribution SHA-256 hashes. Vendor SDK tests use a synthetic controller
boundary; hardware commissioning is required before operation.

References: [official SDK overview](https://docs.booster.tech/docs/developer-guide/booster-os-python-sdk/sdk-overview/), [quick start](https://docs.booster.tech/docs/developer-guide/booster-os-python-sdk/quick-start/), [task API](https://docs.booster.tech/docs/developer-guide/booster-os-python-sdk/booster-robot-client-reference/advanced-task-apis/overview/), [native SDK](https://github.com/BoosterRobotics/booster_robotics_sdk). The documented BoosterOS environment is on a Booster robot or Booster Studio virtual robot, with firmware at least v1.7. Exact installed firmware/API compatibility still requires qualification; no firmware upgrade is performed here.

## Architecture and controller ownership

The Pi runtime keeps the durable coordinator, permissions, broker and task journal. `ros_ws/src/orbo_runtime/orbo_runtime/k1_adapter.py` talks to an owner-private Unix socket, forwarded over authenticated SSH to `worker.py` running on the K1. The worker uses the K1's existing ROS/Python environment (Python 3.10+); it does not require upgrading the vendor OS to the Pi's Python version. It calls `BoosterRobot.do_action`, reads joint/fall/mode feedback, and uses the returned task handle's cancellation/status. Vendor gait, balance, motor gains, joint trajectories and recovery remain untouched.

Only explicitly commissioned actions that `list_actions()` identifies as interruptible `upper_body` actions are admitted. The bundled `hand_wave` runtime profile is a candidate, disabled by default. It supports bounded upper-body actions; object manipulation and gripper control are not included.

The Pi renews a 500 ms software lease. The on-K1 worker independently requests stop on lease loss, stale joint/fall feedback, owner stop or action deadline. A normal bounded hold ends through the vendor's stop operation. The SDK may refuse cancellation before observing action start; the worker retries **stop only**, never start. SQL intent precedes the SDK call, identities are idempotent, late starts are stopped, and unknown outcomes survive process restart. Feedback requires the complete vendor joint-name inventory and is bounded to 60 samples. SDK header timestamps are checked as UTC seconds; their physical capture-versus-receipt semantics remain to be qualified on the robot. Controller completion remains separate from independently observed success.

This software lease is not a vendor motor watchdog. SIGKILL, power loss or a hung native RPC can defeat the worker's stop loop; physical qualification must establish the K1 controller's behavior and available emergency stop in those cases. Do not mark the profile qualified until that behavior and stop latency are acceptable. The service does not automatically restart, change modes, get up, enter damping, or resume a motion after failure. A user-facing `stop` is a request, not measured cessation.

## Install on the intended K1

Preserve its vendor ROS environment. Copy this integration directory to an owner-controlled location, for example `$HOME/.local/share/orbo-k1`. Install the pinned dependencies into a separate environment using the actual vendor Python; `--system-site-packages` retains its installed ROS Python bindings:

```sh
cd "$HOME/.local/share/orbo-k1"
python3 -m venv --system-site-packages .venv
.venv/bin/python -m pip install --require-hashes -r requirements.lock
mkdir -p "$HOME/.config/orbo-k1" "$HOME/.local/state/orbo-k1"
chmod 700 "$HOME/.config/orbo-k1" "$HOME/.local/state/orbo-k1"
cp worker.template.json "$HOME/.local/state/orbo-k1/worker.json"
chmod 600 "$HOME/.local/state/orbo-k1/worker.json"
```

Set `K1_ROS_SETUP` to the actual installed ROS setup script, `K1_INSTALL` to the installation directory and `K1_STATE` to the persistent private state directory in `$HOME/.config/orbo-k1/environment`. Use full absolute values. The optional user-service template expects `run-worker.sh` under `$HOME/.local/share/orbo-k1`; inspect it before installation. Neither this repository nor its Pi installer enables the K1 service.

With the correct ROS environment sourced, the official read-only inventory calls are `BoosterRobot().robot_info`, `get_mode()`, `list_actions()`, `get_joint_states()` and `get_fall_down_state()`. Record the actual serial number, firmware, SDK versions, operating mode, joint feedback freshness and selected action. Populate `worker.json` with those exact values and an evidence-record reference. Keep `qualified=false` until stop, interruption, network/process loss and recovery have been tested with an operator at the vendor emergency stop. Use the vendor procedure to enter the required upright `walk` balance mode; this worker does not select it.

Both socket and persistent database directories must be private and owner-controlled. Start explicitly with `run-worker.sh` after exporting the three environment values (or use the reviewed user service). It refuses an existing socket: after a crash, verify the old process is gone before removing only the stale socket; preserve `commands.db` and its WAL.

## Connect the Pi runtime

Copy `deploy/profiles/booster-k1.toml` to a separate runtime configuration. Keep the Orbo and K1 runtime databases and socket paths distinct. The K1 profile supports verified NEAR text reasoning. OpenAI Live voice requires an explicitly configured embodied runtime as described in [deployment](../../deploy/README.md); the base K1 profile does not enable voice.

On the K1, an authorized SSH connection can forward the worker socket to the Pi service user:

```sh
ssh -N -T -o ExitOnForwardFailure=yes \
  -R /var/lib/orbo/k1-bridge/worker.sock:/ABSOLUTE/K1/STATE/worker.sock orbo@PI_HOST
```

Prepare the Pi directory as `orbo:orbo`, mode 0700, verify the Pi host key, and require the forwarded socket to be owned by `orbo`, mode 0600. The adapter rejects permissive sockets. Keep both hosts' UTC clocks synchronized; a future or expired lease is refused. No TCP daemon port is exposed. This cross-device forwarding path remains untested without access to the actual devices.

The Pi installer installs the disabled `orbo-k1-runtime.service` and `/etc/orbo/booster-k1.toml`, with a separate `/run/orbo-k1/runtime.sock`. Add any provider credential drop-in explicitly for this service as well. Enable voice on only one shared audio endpoint at a time, or use separately qualified devices. Run the runtime with the K1 profile; call `robot.inspect` before acquiring a command grant so the broker sees the worker's durable epoch. Grant `robot.command` to `booster-k1` with an explicit expiry. Create a `physical_task.v1` episode. `robot.action` takes `episode_id`, `key`, a commissioned `action_id`, `hold_seconds` (0.1–20) and `duration` (hold plus stop margin, at most 30). The verified model's `perform_humanoid_action` tool uses the same broker. `request_attention` can use the explicitly configured attention action.

`stop` requests cancellation. `robot.reconcile {command_id}` reads the worker's preserved outcome. If the result remains unknown, an owner who has independently checked the robot stopped can use `robot.recover {command_id, observed_stopped:true, evidence_record}`. Recovery also requires fresh upright feedback and no active vendor task; it is recorded as **owner_assertion**, not a controller or physical measurement. It never reruns the original action.

## Reproduce software checks

```sh
uv run pytest -q tests/test_k1.py
# In a Linux environment with the pinned vendor SDK and its ROS bindings:
ORBO_K1_SDK_TESTS=1 python -m pytest -q tests/k1
```

Worker fixtures cover cancellation before dispatch, delayed start with lost lease, stop during admission, stale feedback, vendor stop retries, restart uncertainty, owner recovery, action/model qualification and real private Unix IPC. The vendor lane uses the actual task class and native enums with a synthetic controller; it is not a hardware test. Verify firmware, network access, SDK task tracking and physical motion/stop behavior on the actual robot before enabling actions.

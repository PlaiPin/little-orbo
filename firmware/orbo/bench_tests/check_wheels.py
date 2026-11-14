#!/usr/bin/env python3
"""Exercise both wheels on a lifted base; use --observe-only to inspect feedback without motion."""
import argparse
import json
import math
import tempfile
import time
from pathlib import Path

import rclpy
from rclpy.qos import QoSProfile, ReliabilityPolicy
from rclpy.signals import SignalHandlerOptions
from orbo_embedded_interfaces.msg import ActuatorStatus, ControlSession, Health, WheelCommand, WheelState


TEST_SPEED_RAD_S = 1.2

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path, nargs="?",
                        help="Optional JSON result path; defaults to a new temporary directory")
    parser.add_argument("--observe-only", action="store_true", help="Inspect feedback without arming or motion")
    parser.add_argument("--observe-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if not math.isfinite(args.observe_seconds) or args.observe_seconds < 5:
        parser.error("--observe-seconds must be finite and at least 5")
    if args.output is None:
        args.output = Path(tempfile.mkdtemp(prefix="orbo-wheels-")) / "result.json"
    if args.output.exists():
        parser.error("output already exists; choose a new path to preserve earlier evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    print("Results:", args.output, flush=True)
    # Keep the ROS context alive on Ctrl-C so the finally block can send stop.
    rclpy.init(domain_id=42, signal_handler_options=SignalHandlerOptions.NO)
    node = rclpy.create_node("orbo_lifted_wheel_check")
    samples = {"wheel": [], "health": [], "status": []}
    started = time.monotonic()
    checks, phases = {}, []
    boot, epoch, sequence = None, None, 0
    error = None

    def capture(kind, msg):
        record = {"received_s": time.monotonic() - started,
                  "host_epoch_ms": int(time.time() * 1000)}
        for name in msg.get_fields_and_field_types():
            value = getattr(msg, name)
            record[name] = value.tolist() if hasattr(value, "tolist") else value
        samples[kind].append(record)

    qos = QoSProfile(depth=100, reliability=ReliabilityPolicy.BEST_EFFORT)
    node.create_subscription(WheelState, "/orbo/embedded/wheel_state",
                             lambda msg: capture("wheel", msg), qos)
    node.create_subscription(Health, "/orbo/embedded/health",
                             lambda msg: capture("health", msg), qos)
    node.create_subscription(ActuatorStatus, "/orbo/embedded/actuator_status",
                             lambda msg: capture("status", msg), qos)
    sessions = node.create_publisher(ControlSession, "/orbo/embedded/control_session", 10)
    wheels = node.create_publisher(WheelCommand, "/orbo/embedded/wheel_command", 10)

    def spin(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            rclpy.spin_once(node, timeout_sec=min(0.02, max(0.0, end - time.monotonic())))

    def require(condition, name):
        checks[name] = bool(condition)
        if not condition:
            raise RuntimeError(name)

    def valid_snapshot(sample):
        ages = [sample["sample_mono_us"] - stamp for stamp in sample["motor_sample_mono_us"]]
        return (all(sample["valid"]) and sample["hardware_error"] == [0, 0]
                and all(0 <= age < 150000 for age in ages)
                and abs(sample["motor_sample_mono_us"][0] - sample["motor_sample_mono_us"][1]) < 100000
                and all(math.isfinite(v) for v in sample["velocity_rad_s"] + sample["position_rad"]))

    def fresh_wheel():
        if not samples["wheel"]:
            raise RuntimeError("No wheel feedback")
        sample = samples["wheel"][-1]
        if time.monotonic() - started - sample["received_s"] > 0.15:
            raise RuntimeError("Wheel feedback delivery gap exceeded 150 ms")
        if sample["boot_id"] != boot or not valid_snapshot(sample):
            raise RuntimeError("Wheel feedback invalid, timestamp inconsistent, or board rebooted")
        return sample

    def publish_stop():
        sessions.publish(ControlSession(interface_version=1, operation=0))

    def arm(label):
        nonlocal epoch, sequence
        current = fresh_wheel()
        require(not current["armed"], label + "_starts_disarmed")
        epoch = current["epoch"] + 1
        sequence = 0
        sessions.publish(ControlSession(interface_version=1, boot_id=boot, epoch=epoch,
                         sequence=1, issued_epoch_ms=int(time.time() * 1000) - 20,
                         valid_for_ms=500, operation=1, enable_wheels=True))
        end = time.monotonic() + 2
        accepted = False
        while time.monotonic() < end:
            spin(0.02)
            current = fresh_wheel()
            if current["armed"] and current["epoch"] == epoch:
                accepted = True
                break
        require(accepted, label + "_arm_accepted")

    def drive(label, velocity, seconds=8.0):
        nonlocal sequence
        print(f"Moving {label}: {velocity} rad/s for {seconds:g} seconds", flush=True)
        first = fresh_wheel()
        begin = len(samples["wheel"])
        phase_start = time.monotonic()
        while time.monotonic() - phase_start < seconds:
            current = fresh_wheel()
            if not current["armed"] or current["epoch"] != epoch:
                raise RuntimeError("Unexpected disarm/session change during " + label)
            sequence += 1
            wheels.publish(WheelCommand(boot_id=boot, epoch=epoch, sequence=sequence,
                           command_id=sequence, issued_epoch_ms=int(time.time() * 1000) - 20,
                           valid_for_ms=300, velocity_rad_s=velocity))
            spin(0.05)
        observed = samples["wheel"][begin:]
        require(len(observed) >= 10, label + "_feedback_received")
        # Ignore the first 0.4 s when evaluating velocity so a reversal can settle.
        settled = [s for s in observed if s["received_s"] >= observed[0]["received_s"] + 0.4]
        require(bool(settled), label + "_settled_feedback_received")
        require(any(s["last_command_sequence"] > 0 for s in observed), label + "_command_admitted")
        for index, target in enumerate(velocity):
            measured = [s["velocity_rad_s"][index] for s in settled]
            matching = (sum(abs(target) * 0.5 <= v * (1 if target > 0 else -1) <= abs(target) * 1.5 for v in measured)
                        if target else sum(abs(v) < 0.05 for v in measured))
            require(matching >= len(measured) * 0.75, label + f"_wheel_{index}_velocity")
            if target:
                delta = observed[-1]["position_rad"][index] - first["position_rad"][index]
                require(delta * (1 if target > 0 else -1) > 0.05,
                        label + f"_wheel_{index}_encoder_direction")
        phases.append({"name": label, "requested_rad_s": velocity,
                       "samples": len(observed), "epoch": epoch,
                       "start_position_rad": first["position_rad"],
                       "end_position_rad": observed[-1]["position_rad"],
                       "mean_settled_velocity_rad_s": [sum(s["velocity_rad_s"][i] for s in settled) / len(settled) for i in range(2)]})
        print(json.dumps(phases[-1]), flush=True)

    def stopped(label, explicit):
        begin = len(samples["wheel"])
        if explicit:
            publish_stop()
        end = time.monotonic() + 2
        consecutive = 0
        while time.monotonic() < end:
            spin(0.05)
            current = fresh_wheel()
            recent = samples["wheel"][begin:]
            begin = len(samples["wheel"])
            for sample in recent:
                if not sample["armed"] and all(abs(v) < 0.05 for v in sample["velocity_rad_s"]):
                    consecutive += 1
                else:
                    consecutive = 0
            if consecutive >= 5:
                break
        require(consecutive >= 5, label + "_disarmed_and_encoder_stopped")
        if not explicit:
            # Health is published more slowly than wheel feedback.
            spin(0.4)
            require(bool(samples["health"]) and samples["health"][-1]["fault"] == 2,
                    label + "_expired_fault")

    try:
        end = time.monotonic() + 12
        while time.monotonic() < end:
            spin(0.1)
            if samples["wheel"] and samples["health"] and sessions.get_subscription_count() and wheels.get_subscription_count():
                break
        require(bool(samples["wheel"]) and bool(samples["health"]), "initial_telemetry")
        boot = samples["wheel"][-1]["boot_id"]
        require(not samples["wheel"][-1]["armed"], "initially_disarmed")
        spin(args.observe_seconds)
        observed = samples["wheel"]
        require(len(observed) >= 100, "observation_sample_count")
        require(all(valid_snapshot(s) for s in observed), "feedback_timestamp_order_and_health")
        require(all(s["boot_id"] == boot and not s["armed"] for s in observed), "observation_disarmed_same_boot")
        require(all(s["read_errors"] == observed[0]["read_errors"] for s in observed), "observation_no_new_read_errors")
        latest_health = samples["health"][-1]
        require(latest_health["clock_synced"] and abs(latest_health["host_epoch_ms"] - latest_health["epoch_ms"]) < 100,
                "clock_aligned_before_motion")
        print("Read-only feedback and timestamp checks passed", flush=True)
        if not args.observe_only:
            require(sessions.get_subscription_count() == 1 and wheels.get_subscription_count() == 1,
                    "one_firmware_command_subscriber")
            require(node.count_publishers("/orbo/embedded/control_session") == 1
                    and node.count_publishers("/orbo/embedded/wheel_command") == 1,
                    "exclusive_test_command_publishers")
            for index, label in enumerate(("left", "right")):
                arm(label)
                for sign, direction in ((1, "positive"), (-1, "negative")):
                    velocity = [0.0, 0.0]
                    velocity[index] = sign * TEST_SPEED_RAD_S
                    drive(label + "_" + direction, velocity)
                stopped(label + "_explicit_stop", explicit=True)
            arm("both")
            drive("both_positive", [TEST_SPEED_RAD_S, TEST_SPEED_RAD_S])
            stopped("command_expiry", explicit=False)
    except (Exception, KeyboardInterrupt) as exc:
        error = str(exc) or type(exc).__name__
        print("FAILED:", error, flush=True)
    finally:
        if not args.observe_only:
            # Stop is always accepted, including after an unexpected disarm.
            for _ in range(3):
                publish_stop()
                spin(0.1)
        report = {"exercise_wheels": not args.observe_only, "checks": checks,
                  "error": error, "phases": phases, "samples": samples,
                  "limits": f"Lifted base; {TEST_SPEED_RAD_S:g} rad/s commands for 8 seconds per direction; 300 ms validity. Encoder evidence, not independently measured physical stop latency."}
        args.output.write_text(json.dumps(report, indent=2) + "\n")
        node.destroy_node()
        rclpy.shutdown()
    print(json.dumps(checks, indent=2))
    return 1 if error or not checks or not all(checks.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())

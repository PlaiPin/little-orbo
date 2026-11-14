"""Real controllers, gate and plugin with synthetic encoders; isolated DDS only."""
import importlib.util
import math
import os
from pathlib import Path
import signal
import subprocess
import threading
import time
from types import SimpleNamespace

import pytest

pytestmark = pytest.mark.skipif(
    os.getenv('ORBO_ROS_TESTS') != '1', reason='explicit ROS integration lane')
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def stack(tmp_path):
    import rclpy
    from rclpy.executors import SingleThreadedExecutor
    from rclpy.qos import qos_profile_sensor_data
    from orbo_embedded_interfaces.msg import WheelState, WheelCommand, Health, ControlSession

    assert os.environ.get('ROS_DOMAIN_ID') == '96', 'Never test on the board domain'
    spec = importlib.util.spec_from_file_location('base_bench', ROOT/'tools/control-base.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    rclpy.init()
    firmware = rclpy.create_node('synthetic_firmware')
    wp = firmware.create_publisher(WheelState, '/orbo/embedded/wheel_state', qos_profile_sensor_data)
    hp = firmware.create_publisher(Health, '/orbo/embedded/health', qos_profile_sensor_data)
    state = dict(boot=42, epoch=0, armed=False, arms=0, sequence=0,
                 position=[100., 150.], measured_velocity=[.4, .4],
                 started=None, received=[], deadline=None, feedback=True, stops=0)

    def session(msg):
        if msg.operation == 0:
            state['armed'] = False
            state['stops'] += 1
        elif msg.operation == 1:
            state.update(epoch=msg.epoch, armed=True, started=time.monotonic(), deadline=None)
            state['arms'] += 1

    def command(msg):
        state['received'].append((time.monotonic(), msg))
        if msg.boot_id == state['boot'] and msg.epoch == state['epoch'] and state['armed']:
            state['deadline'] = (msg.issued_epoch_ms + msg.valid_for_ms)/1000

    subscriptions = [
        firmware.create_subscription(ControlSession, '/orbo/embedded/control_session', session, 10),
        firmware.create_subscription(WheelCommand, '/orbo/embedded/wheel_command', command, 10)]
    previous_tick = time.monotonic()

    def feedback():
        nonlocal previous_tick
        now_s = time.monotonic()
        dt, previous_tick = now_s - previous_tick, now_s
        if state['deadline'] is not None and time.time() >= state['deadline']:
            state['armed'] = False
        moving = state['armed'] and now_s-state['started'] > 1
        if moving:
            for i in range(2):
                state['position'][i] += state['measured_velocity'][i]*dt
        if not state['feedback']:
            return
        state['sequence'] += 1
        now = time.monotonic_ns()//1000
        wp.publish(WheelState(boot_id=state['boot'], epoch=state['epoch'], sequence=state['sequence'],
            sample_mono_us=now, motor_sample_mono_us=[now, now], epoch_ms=time.time_ns()//10**6,
            armed=state['armed'], valid=[True, True], hardware_error=[0, 0],
            position_rad=list(state['position']),
            velocity_rad_s=state['measured_velocity'] if moving else [0., 0.]))
        if state['sequence'] % 10 == 0:
            hp.publish(Health(interface_version=1, boot_id=state['boot'], epoch=state['epoch'],
                armed=state['armed'], clock_synced=True, epoch_ms=time.time_ns()//10**6))

    firmware.create_timer(.02, feedback)
    executor = SingleThreadedExecutor()
    executor.add_node(firmware)
    thread = threading.Thread(target=executor.spin, daemon=True)
    thread.start()
    log = (tmp_path/'stack.log').open('w')
    process = subprocess.Popen(['ros2', 'launch', 'orbo_bringup', 'control.launch.py',
        'calibration:='+str(ROOT/'ros_ws/src/orbo_description/config/bench-cad.yaml')],
        stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
    node = rclpy.create_node('synthetic_owner')
    controller = module.BasePublisher(node, .033, .1018)
    try:
        controller.connect_and_arm()
        yield SimpleNamespace(controller=controller, state=state, module=module)
    finally:
        controller.stop()
        os.killpg(process.pid, signal.SIGINT)
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        executor.shutdown(timeout_sec=2)
        thread.join(timeout=2)
        node.destroy_node()
        firmware.destroy_node()
        rclpy.shutdown()
        log.close()


def drive(stack, seconds, velocity):
    start = time.monotonic()
    while time.monotonic()-start < seconds:
        stack.controller.publish(velocity)
        stack.controller.spin(.05)


def yaw(msg):
    q = msg.pose.pose.orientation
    return math.atan2(2*(q.w*q.z+q.x*q.y), 1-2*(q.y*q.y+q.z*q.z))


def test_30_second_odometry_uses_feedback_not_command(stack):
    drive(stack, 30, [.6, .6])
    assert stack.controller.stop()
    a, b = stack.controller.samples[0], stack.controller.samples[-1]
    dx = b[1].pose.pose.position.x-a[1].pose.pose.position.x
    expected = .033*(b[2][0]-a[2][0])
    assert abs(dx-expected) < .002
    assert .36 < dx < .42  # Requested-speed integration would be ~0.594 m.
    assert abs(a[1].pose.pose.position.x) < .001
    assert abs(yaw(a[1])) < .001  # Different absolute counts must not create initial yaw.
    received = stack.state['received']
    assert len(received) > 500
    times = [t for t, _ in received]
    assert 15 < (len(times)-1)/(times[-1]-times[0]) < 25
    assert stack.state['arms'] == 1
    for _, msg in received:
        assert msg.boot_id == 42 and msg.epoch == stack.state['epoch']
        assert 0 < msg.valid_for_ms <= 200
    assert max(m.velocity_rad_s[0] for _, m in received) > .5


@pytest.mark.parametrize('measured,requested', [([-.4, -.4], [-.6, -.6]), ([-.4, .4], [-.6, .6])])
def test_reverse_and_turn_odometry(stack, measured, requested):
    stack.state['measured_velocity'] = measured
    drive(stack, 3, requested)
    assert stack.controller.stop()
    a, b = stack.controller.samples[0], stack.controller.samples[-1]
    delta = [b[2][i]-a[2][i] for i in range(2)]
    expected_distance = .033*sum(delta)/2
    expected_yaw = .033*(delta[1]-delta[0])/.1018
    assert abs(b[1].pose.pose.position.x-a[1].pose.pose.position.x-expected_distance) < .002
    assert abs(math.remainder(yaw(b[1])-yaw(a[1])-expected_yaw, 2*math.pi)) < .02


def test_source_loss_stops_refresh_and_does_not_rearm(stack):
    drive(stack, 1.3, [.6, .6])
    lost_at = time.monotonic()
    stack.controller.spin(.7)
    assert not stack.state['armed']
    assert stack.state['received'][-1][0] < lost_at+.3
    count = len(stack.state['received'])
    with pytest.raises(RuntimeError, match='disarmed'):
        stack.controller.publish([.6, .6])
    stack.controller.spin(.3)
    assert len(stack.state['received']) == count
    assert stack.state['arms'] == 1


@pytest.mark.parametrize('failure', ['feedback_loss', 'reboot'])
def test_feedback_loss_and_reboot_stop_hardware_without_rearm(stack, failure):
    drive(stack, 1.3, [.6, .6])
    stops = stack.state['stops']
    if failure == 'feedback_loss':
        stack.state['feedback'] = False
    else:
        stack.state.update(boot=43, epoch=0, sequence=0, armed=False, position=[0., 0.])
    stack.controller.spin(.5)
    assert stack.state['stops'] > stops
    assert not stack.state['armed']
    count = len(stack.state['received'])
    stack.state['feedback'] = True
    stack.controller.spin(.3)
    assert len(stack.state['received']) == count
    assert stack.state['arms'] == 1
    with pytest.raises(RuntimeError):
        stack.controller.publish([.6, .6])

#!/usr/bin/env python3
"""Lifted-bench ROS controller input: TwistStamped -> gate -> ros2_control -> MCU.

This is a local bench session owner, not the production broker. No direct wheel
publisher is created. CAD geometry is nominal; odometry uses measured encoders.
"""
import argparse
from collections import deque
import math
import os
from pathlib import Path
import signal
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'firmware/orbo/bench_tests'))
from control_wheels import WheelPublisher, keyboard_loop, PERIOD, LIMIT, SESSION_TOPIC, WHEEL_TOPIC
import rclpy
from rclpy.signals import SignalHandlerOptions
from rclpy.qos import qos_profile_sensor_data
from controller_manager_msgs.srv import SetHardwareComponentState, SwitchController, ListControllers, ListHardwareComponents
from geometry_msgs.msg import TwistStamped
from nav_msgs.msg import Odometry
from sensor_msgs.msg import JointState
from orbo_embedded_interfaces.msg import ControlSession
import yaml


class BasePublisher(WheelPublisher):
    def __init__(self, node, radius, separation):
        super().__init__(node, direct_wheels=False)
        self.radius, self.separation = radius, separation
        self.lease = node.create_publisher(ControlSession, '/orbo/control/teleop_lease', 1)
        self.twist = node.create_publisher(TwistStamped, '/orbo/teleop/cmd_vel', 1)
        self.odom = self.joints = None
        self.odom_received = self.joints_received = -math.inf
        # Keep keyboard sessions bounded in memory (last two minutes at 50 Hz).
        self.samples = deque(maxlen=6000)
        self.subscriptions += [
            node.create_subscription(Odometry, '/orbo/drive_controller/odom', self.on_odom, qos_profile_sensor_data),
            node.create_subscription(JointState, '/orbo/joint_states', self.on_joints, qos_profile_sensor_data)]

    def on_odom(self, msg):
        self.odom, self.odom_received = msg, time.monotonic()
        if self.wheel is not None:
            self.samples.append((self.odom_received, msg, list(self.wheel.position_rad)))

    def on_joints(self, msg):
        self.joints, self.joints_received = msg, time.monotonic()

    def check_graph(self):
        expected = {SESSION_TOPIC: 3, WHEEL_TOPIC: 1,
                    '/orbo/control/teleop_lease': 1, '/orbo/teleop/cmd_vel': 1,
                    '/orbo/drive_controller/cmd_vel': 1, '/orbo/control/authority': 1}
        for topic, count in expected.items():
            actual = self.node.count_publishers(topic)
            if actual != count:
                raise RuntimeError(f'{topic}: expected {count} publishers, found {actual}; run one stack and one controller')
        if (self.sessions.get_subscription_count()!=2 or self.lease.get_subscription_count()!=1
                or self.twist.get_subscription_count()!=1 or self.node.count_subscribers(WHEEL_TOPIC)!=1):
            raise RuntimeError('Firmware, hardware plugin or velocity gate not connected')

    def call(self, kind, name, request):
        client=self.node.create_client(kind, '/orbo/controller_manager/'+name)
        try:
            if not client.wait_for_service(timeout_sec=10): raise RuntimeError('Controller service unavailable: '+name)
            future=client.call_async(request); deadline=time.monotonic()+10
            while not future.done() and time.monotonic()<deadline: self.spin(.02)
            if not future.done(): raise RuntimeError('Controller service timed out: '+name)
            return future.result()
        finally: self.node.destroy_client(client)

    def connect_and_arm(self):
        # Activation is separate from MCU arming and requires real feedback.
        deadline=time.monotonic()+15
        while time.monotonic()<deadline:
            self.spin(.05)
            if self.wheel and self.health:
                try: self.check();break
                except RuntimeError: pass
        self.check()
        hardware=self.call(ListHardwareComponents,'list_hardware_components',ListHardwareComponents.Request())
        state=next((h.state.id for h in hardware.component if h.name=='OrboWheels'),None)
        if state!=3:
            req=SetHardwareComponentState.Request();req.name='OrboWheels';req.target_state.id=2;req.target_state.label='inactive'
            if not self.call(SetHardwareComponentState,'set_hardware_component_state',req).ok:
                raise RuntimeError('Hardware configuration refused')
            self.spin(.3)  # A newly configured transport must receive fresh feedback.
            req.target_state.id=3;req.target_state.label='active'
            if not self.call(SetHardwareComponentState,'set_hardware_component_state',req).ok:
                raise RuntimeError('Hardware activation refused')
        deadline=time.monotonic()+10
        while True:
            listing=self.call(ListControllers,'list_controllers',ListControllers.Request())
            states={c.name:c.state for c in listing.controller}
            if all(states.get(n) in ('inactive','active') for n in ('joint_state_broadcaster','drive_controller')):
                break
            if time.monotonic()>=deadline:
                raise RuntimeError(f'Controllers did not finish loading: {states}')
            self.spin(.1)
        missing=[n for n in ('joint_state_broadcaster','drive_controller') if states.get(n)!='active']
        if missing:
            req=SwitchController.Request();req.activate_controllers=missing;req.strictness=2;req.timeout.sec=5
            if not self.call(SwitchController,'switch_controller',req).ok:
                raise RuntimeError('Controller activation refused')
        deadline=time.monotonic()+2
        while True:
            self.spin(.02)
            try:
                self.check_odometry()
                break
            except RuntimeError:
                if time.monotonic()>=deadline:
                    raise
        super().connect_and_arm()
        self.samples.clear()

    def check_odometry(self):
        now=time.monotonic()
        if self.odom is None or now-self.odom_received>.25:
            raise RuntimeError('Measured odometry missing or older than 250 ms')
        if self.joints is None or now-self.joints_received>.25:
            raise RuntimeError('Measured joint states missing or older than 250 ms')
        if self.odom.header.frame_id!='odom' or self.odom.child_frame_id!='base_link':
            raise RuntimeError('Odometry frames do not match odom -> base_link')
        if not {'left_wheel_joint','right_wheel_joint'} <= set(self.joints.name):
            raise RuntimeError('Wheel joints missing from joint-state broadcaster')
        pose=self.odom.pose.pose
        if not all(math.isfinite(v) for v in (pose.position.x, pose.position.y,
                pose.position.z, pose.orientation.x, pose.orientation.y,
                pose.orientation.z, pose.orientation.w)):
            raise RuntimeError('Odometry contains non-finite values')
        for name in ('left_wheel_joint','right_wheel_joint'):
            i=self.joints.name.index(name)
            if (i>=len(self.joints.position) or i>=len(self.joints.velocity) or
                    not all(math.isfinite(v) for v in (self.joints.position[i],self.joints.velocity[i]))):
                raise RuntimeError('Wheel joint measurements missing or non-finite')

    def publish(self, velocity):
        self.check(armed=True); self.check_odometry()
        if len(velocity)!=2 or not all(math.isfinite(v) and abs(v)<=LIMIT for v in velocity):
            raise ValueError('Wheel-equivalent speed exceeds firmware limit')
        self.sequence+=1
        now=time.monotonic()
        if self.last_publish_time is not None:self.max_publish_gap=max(self.max_publish_gap,now-self.last_publish_time)
        self.last_publish_time=now
        stamp_ns=time.time_ns()-20_000_000
        self.lease.publish(ControlSession(interface_version=1,operation=2,enable_wheels=True,
            boot_id=self.boot,epoch=self.epoch,sequence=self.sequence,issued_epoch_ms=stamp_ns//1_000_000,valid_for_ms=300))
        command=TwistStamped();command.header.frame_id='base_link'
        command.header.stamp.sec=stamp_ns//1_000_000_000;command.header.stamp.nanosec=stamp_ns%1_000_000_000
        command.twist.linear.x=self.radius*(velocity[0]+velocity[1])/2
        command.twist.angular.z=self.radius*(velocity[1]-velocity[0])/self.separation
        self.twist.publish(command)

    def report(self):
        if len(self.samples)<2:return
        a,b=self.samples[0],self.samples[-1]
        delta=[b[2][i]-a[2][i] for i in range(2)]
        distance=self.radius*sum(delta)/2
        angle=self.radius*(delta[1]-delta[0])/self.separation
        dx=b[1].pose.pose.position.x-a[1].pose.pose.position.x
        dy=b[1].pose.pose.position.y-a[1].pose.pose.position.y
        def yaw(msg):
            q=msg.pose.pose.orientation
            return math.atan2(2*(q.w*q.z+q.x*q.y),1-2*(q.y*q.y+q.z*q.z))
        odom_yaw=math.remainder(yaw(b[1])-yaw(a[1]),2*math.pi)
        print(f'Measured odometry: {len(self.samples)} samples, {b[0]-a[0]:.2f} s; '
              f'encoder travel={distance:.5f} m, encoder yaw={angle:.5f} rad; '
              f'odom delta=({dx:.5f}, {dy:.5f}) m, yaw={odom_yaw:.5f} rad; '
              f'read_errors={[int(v) for v in self.wheel.read_errors]}; max publish gap={1000*self.max_publish_gap:.1f} ms')
        print('Lifted bench: encoder-derived travel using CAD geometry, not physical floor displacement.')


def main():
    p=argparse.ArgumentParser(description=__doc__);sub=p.add_subparsers(dest='mode',required=True)
    k=sub.add_parser('keyboard');k.add_argument('--speed',type=float,default=LIMIT)
    d=sub.add_parser('drive');d.add_argument('--linear',type=float,default=.02);d.add_argument('--angular',type=float,default=0);d.add_argument('--seconds',type=float,default=30)
    args=p.parse_args();c=yaml.safe_load((ROOT/'ros_ws/src/orbo_description/config/bench-cad.yaml').read_text())
    radius,separation=c['wheel_radius'],c['wheel_separation']
    if args.mode=='drive':
        velocities=[(args.linear-args.angular*separation/2)/radius,(args.linear+args.angular*separation/2)/radius]
        if not all(math.isfinite(v) and abs(v)<=LIMIT for v in velocities) or not 0<args.seconds<=60:p.error('Command exceeds 1.2 rad/s wheel or 60-second bench bound')
    elif not math.isfinite(args.speed) or not 0<args.speed<=LIMIT:p.error('--speed must be positive and at most 1.2 rad/s')
    rclpy.init(domain_id=int(os.environ.get('ROS_DOMAIN_ID', '42')),signal_handler_options=SignalHandlerOptions.NO)
    node=rclpy.create_node('orbo_base_bench');controller=BasePublisher(node,radius,separation)
    def interrupt(*_):raise KeyboardInterrupt
    old={s:signal.signal(s,interrupt) for s in (signal.SIGTERM,signal.SIGTSTP)}
    result=0
    try:
        if args.mode=='keyboard': keyboard_loop(controller,args.speed)
        else:
            controller.connect_and_arm();start=time.monotonic()
            while time.monotonic()-start<args.seconds:controller.publish(velocities);controller.spin(PERIOD)
            print(f'Drive completed: {time.monotonic()-start:.2f} s through ros2_control.')
    except KeyboardInterrupt:print('Stopping...')
    except Exception as exc:
        controller.failure_wheel=controller.wheel;print(f'Controller stopped: {exc}',file=sys.stderr);result=1
    finally:
        try:
            if not controller.stop():result=1
            controller.report()
            if result:controller.describe_failure()
        finally:
            for s,h in old.items():signal.signal(s,h)
            controller.close()
            node.destroy_node();rclpy.shutdown()
    return result

if __name__=='__main__':raise SystemExit(main())

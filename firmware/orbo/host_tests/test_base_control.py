"""ROS message/description checks with mocks; never accesses hardware."""
import math
from pathlib import Path
import time
import unittest
from unittest.mock import Mock
import xml.etree.ElementTree as ET
import yaml

ROOT=Path(__file__).resolve().parents[3]
from orbo_runtime.bench import base
from orbo_runtime.bench.wheels import WheelState, Health
from orbo_runtime.commissioning import validate_calibration
from orbo_runtime.contracts import Rejected


class BaseTests(unittest.TestCase):
    def setUp(self):
        self.node=Mock();self.pubs={}
        def publisher(kind,topic,depth):
            self.pubs[topic]=Mock();return self.pubs[topic]
        self.node.create_publisher.side_effect=publisher
        self.controller=base.BasePublisher(self.node,.033,.1018)
        self.node.count_publishers.side_effect=lambda t:3 if t==base.SESSION_TOPIC else 1
        self.node.count_subscribers.return_value=1
        for t,p in self.pubs.items():p.get_subscription_count.return_value=2 if t==base.SESSION_TOPIC else 1
        self.controller.on_wheel(WheelState(boot_id=7,epoch=3,armed=True,valid=[True,True],
            hardware_error=[0,0],sample_mono_us=1000,motor_sample_mono_us=[900,900],
            position_rad=[10.,11.],velocity_rad_s=[0.,0.]))
        self.controller.on_health(Health(boot_id=7,clock_synced=True,epoch_ms=int(time.time()*1000)))
        self.controller.boot,self.controller.epoch=7,3
        odom=base.Odometry();odom.header.frame_id='odom';odom.child_frame_id='base_link'
        self.controller.on_odom(odom)
        self.controller.on_joints(base.JointState(name=['left_wheel_joint','right_wheel_joint'],position=[0.,0.],velocity=[0.,0.]))

    def test_body_commands_use_gate_never_direct_wheel_topic(self):
        self.assertNotIn(base.WHEEL_TOPIC,self.pubs)
        for speeds,linear,angular in [([1.,1.],.033,0.),([-1.,1.],0.,.066/.1018)]:
            self.controller.publish(speeds)
            msg=self.pubs['/orbo/teleop/cmd_vel'].publish.call_args.args[0]
            lease=self.pubs['/orbo/control/teleop_lease'].publish.call_args.args[0]
            self.assertAlmostEqual(msg.twist.linear.x,linear)
            self.assertAlmostEqual(msg.twist.angular.z,angular)
            self.assertEqual((lease.boot_id,lease.epoch,lease.valid_for_ms),(7,3,300))
            self.assertEqual(lease.issued_epoch_ms,(msg.header.stamp.sec*10**9+msg.header.stamp.nanosec)//10**6)

    def test_stale_odometry_competing_sources_and_limits_refused(self):
        for change in [lambda:setattr(self.controller,'odom_received',-math.inf),
                       lambda:setattr(self.controller,'joints_received',-math.inf),
                       lambda:setattr(self.controller.odom,'child_frame_id','wrong'),
                       lambda:setattr(self.controller.odom.pose.pose.position,'x',math.nan),
                       lambda:setattr(self.controller.joints,'position',[]),
                       lambda:setattr(self.controller.joints,'velocity',[math.inf,0.]),
                       lambda:setattr(self.node.count_publishers,'side_effect',lambda t:9)]:
            self.setUp();change()
            with self.assertRaises(RuntimeError):self.controller.publish([0.,0.])
            self.pubs['/orbo/teleop/cmd_vel'].publish.assert_not_called()
        self.setUp()
        for speeds in [[math.nan,0.],[1.21,0.]]:
            with self.assertRaises(ValueError):self.controller.publish(speeds)

    def test_bench_profile_is_not_navigation_or_ground_qualification(self):
        c=yaml.safe_load((ROOT/'ros_ws/src/orbo_description/config/bench-cad.yaml').read_text())
        self.assertFalse(c['wheels_qualified'])
        validate_calibration(c,allow_bench=True)
        for options in ({},{'navigation':True,'allow_bench':True}):
            with self.assertRaises(Rejected):validate_calibration(c,**options)
        c['max_wheel_rad_s']=2
        with self.assertRaises(Rejected):validate_calibration(c,allow_bench=True)

    def test_v9_meshes_and_static_flippers_are_present(self):
        import hashlib,json
        root=ROOT/'ros_ws/src/orbo_description'
        manifest=json.loads((ROOT/'cad/meshes/v9/manifest.json').read_text())
        self.assertEqual(len(manifest['parts']),176)
        self.assertEqual(hashlib.sha256((ROOT/manifest['source']).read_bytes()).hexdigest(),manifest['source_sha256'])
        for g in manifest['groups']:
            mesh=ROOT/'cad/meshes/v9'/(g['group']+'.stl')
            self.assertEqual(hashlib.sha256(mesh.read_bytes()).hexdigest(),g['sha256'])
        full=ET.parse(root/'config/orbo-preview.urdf').getroot()
        internal=ET.parse(root/'config/orbo-preview-internal.urdf').getroot()
        names={l.get('name') for l in full.findall('link')}
        self.assertTrue({'cad_flipper_covers','cad_chassis_and_equipment','cad_head_shell','cad_torso_shell','cad_display_mount'}<=names)
        self.assertNotIn('cad_head_shell',{l.get('name') for l in internal.findall('link')})
        for robot in (full,internal):
            for mesh in robot.findall('.//mesh'):
                self.assertTrue((ROOT/'cad'/mesh.get('filename').split('package://orbo_description/')[1]).is_file())

if __name__=='__main__':unittest.main()

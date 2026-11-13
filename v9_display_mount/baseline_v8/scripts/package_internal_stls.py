import json,shutil,hashlib
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
DEST=ROOT/'desktop_print_package'
DEST.mkdir(exist_ok=True)
source=json.loads((ROOT/'review/print_parts.json').read_text())
checks={x['file']:x for x in json.loads((ROOT/'review/delivery_checks.json').read_text())['stl']}
selection=[
 ('V8 Motor plate','01_Motor_plate.stl'),
 ('V8 Lower front','02_Lower_front_frame.stl'),
 ('V8 Lower rear','03_Lower_rear_frame.stl'),
 ('V8 Battery shelf','04_Battery_shelf.stl'),
 ('V8 Electronics cage','05_Electronics_cage.stl'),
 ('V8 Servo frame bracket L','06_Left_servo_holder.stl'),
 ('V8 Servo frame bracket R','07_Right_servo_holder.stl'),
 ('V8 Internal neck carrier','08_Internal_neck_carrier.stl'),
 ('V8 Front caster cup','09_Front_caster_cup.stl'),
 ('V8 Rear anti-tip ball cup left','10_Rear_caster_cup_left.stl'),
 ('V8 Rear anti-tip ball cup right','11_Rear_caster_cup_right.stl'),
 ('V8 Bottom square locating','12_Mast_locating_bushing.stl'),
 ('V8 Arm L keyed hub','13_Left_arm_hub.stl'),
 ('V8 Arm R keyed hub','14_Right_arm_hub.stl'),
]
manifest=[]
for prefix,name in selection:
 matches=[p for p in source if p['component'].startswith(prefix)]
 assert len(matches)==1
 part=matches[0]
 assert 'Torso' not in part['component'] and 'smooth removable' not in part['component']
 assert checks[part['file']]['watertight']
 src=ROOT/'STL'/part['file'];dst=DEST/name
 shutil.copy2(src,dst)
 digest=hashlib.sha256(dst.read_bytes()).hexdigest()
 assert digest==hashlib.sha256(src.read_bytes()).hexdigest()
 manifest.append(dict(file=name,component=part['component'],sha256=digest,watertight=True,bounds_mm=checks[part['file']]['bounds_mm']))
(DEST/'FILE_MANIFEST.json').write_text(json.dumps(manifest,indent=2))
print(json.dumps(dict(folder=str(DEST),stl_count=len(manifest),total_stl_bytes=sum((DEST/p['file']).stat().st_size for p in manifest),files=[p['file'] for p in manifest]),indent=2))

import sys,json,hashlib
from pathlib import Path
sys.path.insert(0,r'C:\Users\sahlu\Desktop\conversion files\cad_cleanup_tools')
import trimesh
ROOT=Path(__file__).resolve().parents[1]
parts=json.loads((ROOT/'review/step_validation.json').read_text())
report={'step_parts':len(parts),'invalid_step_parts':[x for x in parts if x['solids']!=1 or not x['topology_valid']],'stl':[]}
for p in sorted((ROOT/'STL').glob('*.stl')):
 m=trimesh.load_mesh(p)
 report['stl'].append(dict(file=p.name,watertight=bool(m.is_watertight),bounds_mm=m.bounds.tolist()))
manifest=json.loads((ROOT.parent/'v5_hidden_mounts/review/source_manifest.json').read_text())
report['source_files_checked']=len(manifest)
report['changed_source_files']=[x['path'] for x in manifest if hashlib.sha256((ROOT.parent/x['path']).read_bytes()).hexdigest()!=x['sha256']]
report['native_joint_count']=len(json.loads((ROOT/'review/joints.json').read_text())['joints'])
(ROOT/'review/delivery_checks.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='stl'},indent=2))
retained=json.loads((ROOT/'review/retained_geometry_checks.json').read_text())
report['retained_parts_checked']=len(retained)
report['retained_part_mismatches']=[x for x in retained if not x['geometry_and_position_match']]
(ROOT/'review/delivery_checks.json').write_text(json.dumps(report,indent=2))
print('STLs',len(report['stl']),'all watertight',all(x['watertight'] for x in report['stl']))
assert not report['invalid_step_parts'] and not report['changed_source_files']
assert len(report['stl'])==17 and all(x['watertight'] for x in report['stl'])

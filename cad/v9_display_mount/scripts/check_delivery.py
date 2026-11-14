"""Verify copied source hashes, printable meshes and exported assembly solids."""
import json,pathlib,hashlib,sys
ROOT=pathlib.Path(__file__).resolve().parents[1]
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
original=ROOT.parent/'v8_rigid_frame';copied=ROOT/'baseline_v8'
source=[]
for p in original.rglob('*'):
 if p.is_file():
  other=copied/p.relative_to(original);source.append(dict(file=p.relative_to(original).as_posix(),sha256=sha(p),copy_matches=other.exists() and sha(other)==sha(p)))
import trimesh
meshes=[]
for p in sorted((ROOT/'STL').glob('*.stl')):
 m=trimesh.load_mesh(p);meshes.append(dict(file=p.name,watertight=bool(m.is_watertight),winding_consistent=bool(m.is_winding_consistent),connected_components=len(m.split()),volume_mm3=float(m.volume),triangles=len(m.faces),bounds=m.bounds.tolist()))
report=dict(source_files=len(source),all_v8_copies_identical=all(p['copy_matches'] for p in source),source_manifest=source,meshes=meshes)
(ROOT/'review/delivery_checks.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='source_manifest'},indent=2))
assert len(meshes)==3 and all(m['watertight'] and m['winding_consistent'] and m['connected_components']==1 for m in meshes)
assert report['all_v8_copies_identical']

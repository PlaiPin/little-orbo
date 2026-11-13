"""Independent static check in the actual finished native Fusion document."""
import adsk.core,adsk.fusion,os,json,traceback
ROOT=r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v9_display_mount'
try:
 app=adsk.core.Application.get();assert app.activeDocument.name.startswith('Orbo V9')
 des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;tm=adsk.fusion.TemporaryBRepManager.get()
 oldnames={r['name'] for r in json.load(open(os.path.join(ROOT,'review','fusion_baseline_signature.json')))}
 old=[(o.component.name,b) for o in root.occurrences if o.component.name in oldnames for b in o.bRepBodies]
 new=[(o.component.name,b) for o in root.allOccurrences if o.component.name.startswith('V9 ') for b in o.bRepBodies]
 def volume(a,b):
  aa=a.boundingBox;bb=b.boundingBox
  if any(min(getattr(aa.maxPoint,k),getattr(bb.maxPoint,k))-max(getattr(aa.minPoint,k),getattr(bb.minPoint,k))<1e-7 for k in ('x','y','z')):return 0
  s=tm.copy(a)
  if not tm.booleanOperation(s,b,adsk.fusion.BooleanTypes.IntersectionBooleanType):raise RuntimeError('Native intersection operation failed')
  return s.volume*1000 if s.isValid else 0
 report=dict(original_bodies=len(old),new_bodies=len(new),interference_with_v8=[],new_part_interference=[],contact_area_mm2={})
 for n,a in new:
  for m,b in old:
   v=volume(a,b)
   if v>.02:report['interference_with_v8'].append([n,m,v])
 for i,(n,a) in enumerate(new):
  for m,b in new[i+1:]:
   if 'manufacturer STEP reference' in n and 'manufacturer STEP reference' in m:continue
   v=volume(a,b)
   if v>.02:report['new_part_interference'].append([n,m,v])
 carrier=next(b for n,b in old if 'Internal neck carrier' in n)
 for label,match,dz in [('saddle_on_carrier','pedestal',-.01),('jaw_under_carrier','lower clamp',.01)]:
  s=tm.copy(next(b for n,b in new if match in n));t=adsk.core.Matrix3D.create();t.translation=adsk.core.Vector3D.create(0,0,dz/10);tm.transform(s,t)
  report['contact_area_mm2'][label]=volume(s,carrier)/abs(dz)
 json.dump(report,open(os.path.join(ROOT,'review','native_assembly_checks.json'),'w'),indent=2)
 assert len(new)==41 and not report['interference_with_v8'] and not report['new_part_interference'],report
 open(os.path.join(ROOT,'review','fusion_status.txt'),'a').write('NATIVE STATIC AND CONTACT CHECKS PASSED\n')
except:open(os.path.join(ROOT,'review','fusion_status.txt'),'a').write(traceback.format_exc())

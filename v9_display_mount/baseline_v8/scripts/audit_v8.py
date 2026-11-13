exec(open(r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame\scripts\build_v8.py').read().split('try:\n open(')[0])
def overlap(a,b):
 aa=a.boundingBox;bb=b.boundingBox
 return all(min(getattr(aa.maxPoint,k),getattr(bb.maxPoint,k))-max(getattr(aa.minPoint,k),getattr(bb.minPoint,k))>1e-6 for k in ('x','y','z'))
def volume(a,b):
 if not overlap(a,b):return 0
 s=tm.copy(a)
 if not tm.booleanOperation(s,b,adsk.fusion.BooleanTypes.IntersectionBooleanType):raise RuntimeError('Intersection failure')
 return s.volume*1000 if s.isValid else 0
def moved(b,x=0,y=0,z=0):
 s=tm.copy(b);m=adsk.core.Matrix3D.create();m.translation=vec(x/10,y/10,z/10);tm.transform(s,m);return s
def save():json.dump(report,open(os.path.join(ROOT,'review','audit.json'),'w'),indent=2)
try:
 open(os.path.join(ROOT,'review','audit_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;tm=adsk.fusion.TemporaryBRepManager.get();assert app.activeDocument.name.startswith('Orbo V8')
 parts=[(o.component.name,b) for o in root.occurrences for b in o.bRepBodies]
 changed=[(n,b) for n,b in parts if n.startswith(('V8 Motor','V8 Electronics','V8 Battery shelf','V8 Servo frame','V8 Internal','V8 Lower'))]
 struct=[(n,b) for n,b in parts if not n.startswith('Hardware')]
 report={'static':[],'contact':[],'errors':[]};done=set()
 for n,a in changed:
  for on,b in struct:
   key=tuple(sorted((n,on)))
   if n==on or key in done:continue
   done.add(key);v=volume(a,b)
   if v>.05:report['static'].append([n,on,v])
  save()
  with open(os.path.join(ROOT,'review','audit_status.txt'),'a') as f:f.write(n+' checked\n')
 def body(prefix):return next(b for n,b in parts if n.startswith(prefix))
 deck=body('V8 Electronics');plate=body('V8 Motor');shelf=body('V8 Battery shelf');carrier=body('V8 Internal')
 for side,sx in [('L',-1),('R',1)]:
  b=body('V8 Servo frame bracket '+side);area=volume(moved(b,z=-.01),deck)/.01;report['contact'].append(dict(joint='Servo '+side+' foot to deck',area_mm2=area))
  for y in (-24,24):
   c=moved(carrier,z=-.01);common(c,box(sx*53-6,y-6,123.98,sx*53+6,y+6,124.01));area=volume(c,b)/.01;report['contact'].append(dict(joint='Carrier '+side+' '+str(y),area_mm2=area))
 for label,y in [('front',-43),('rear',49)]:
  b=body('V8 Lower '+label)
  report['contact'].append(dict(joint=label+' frame to plate',area_mm2=volume(moved(b,z=-.01),plate)/.01))
  report['contact'].append(dict(joint=label+' frame to shelf',area_mm2=volume(moved(b,z=.01),shelf)/.01))
 report['contact'].append(dict(joint='Upper cage to shelf',area_mm2=volume(moved(deck,z=-.01),shelf)/.01))
 save()
 # Representative views for review before motion testing.
 for o in root.occurrences:o.isLightBulbOn=not o.name.startswith(('R2','V8 Torso','V8 Head','V8 Neck'))
 cam=app.activeViewport.camera;cam.isSmoothTransition=False;cam.eye=pt(350,-500,310);cam.target=pt(0,0,80);cam.upVector=vec(0,0,1);app.activeViewport.camera=cam;app.activeViewport.fit();app.activeViewport.refresh();adsk.doEvents();app.activeViewport.saveAsImageFile(os.path.join(ROOT,'renders','frame_draft.png'),1600,1400)
 with open(os.path.join(ROOT,'review','audit_status.txt'),'a') as f:f.write('COMPLETE\n')
except:
 with open(os.path.join(ROOT,'review','audit_status.txt'),'a') as f:f.write(traceback.format_exc())

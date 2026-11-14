exec(open(r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame\scripts\build_v8.py').read().split('try:\n open(')[0])
try:
 open(os.path.join(ROOT,'review','refine_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;tm=adsk.fusion.TemporaryBRepManager.get();assert app.activeDocument.name.startswith('Orbo V8')
 prevdoc=next(d for d in app.documents if d.name.startswith('Orbo V7'));prev=adsk.fusion.Design.cast(prevdoc.products.itemByProductType('DesignProductType'))
 for o in root.occurrences:
  n=o.component.name;b=o.component.bRepBodies.item(0);s=tm.copy(b);changed=True
  if n.startswith('V8 Motor plate'):
   for x in (-30,30):
    for y in (-43,49):
     union(s,box(x-6,y-4,14,x+6,y+4,19));cut(s,cyl((x,y,9.9),(x,y,19.1),1.7))
   head_paths(s)
  elif n.startswith('V8 Battery shelf'):
   source=next(p.component.bRepBodies.item(0) for p in prev.rootComponent.occurrences if p.name.startswith('V7 Battery shelf'));s=tm.copy(source)
   for x in (-30,30):cut(s,cyl((x,-12,48.9),(x,-12,52.1),1.7))
  elif n.startswith('V8 Electronics'):
   for sx in (-1,1):
    xa,xb=sorted((sx*37.6,sx*40.6));cut(s,box(xa,-7.2,51.9,xb,23.2,55.3))
   for za,zb in ((52,56),(78,82.1)):union(s,box(-15,45,za,15,53,zb))
   for xa,xb in ((-10,-6),(6,10)):union(s,box(xa,49,52,xb,53,82.1))
   cut(s,box(-5.6,48.4,51.9,5.6,59.6,85.1));head_paths(s)
  elif n.startswith('V8 Lower rear'):
   for za,zb in ((19,23),(45,49)):union(s,box(-15,45,za,15,53,zb))
   for xa,xb in ((-10,-6),(6,10)):union(s,box(xa,49,19,xb,53,49))
   union(s,box(-15,45,32,15,53,36));cut(s,box(-5.6,48.4,18.9,5.6,59.6,49.1));head_paths(s)
  elif n.startswith('V8 Lower front'):
   cut(s,box(-24,-47.1,18.9,24,-38.9,23));union(s,box(-30,-47,23,30,-43,27));head_paths(s)
  else:changed=False
  if changed:
   appearance=b.appearance;b.deleteMe();new=o.component.bRepBodies.add(s);new.name=n;new.appearance=appearance
 exec(compile(open(os.path.join(ROOT,'scripts','audit_v8.py'),encoding='utf-8').read(),'audit_v8.py','exec'))
 em=des.exportManager;em.execute(em.createFusionArchiveExportOptions(os.path.join(ROOT,'CAD','Orbo_V8_RigidFrame.f3d')));em.execute(em.createSTEPExportOptions(os.path.join(ROOT,'CAD','Orbo_V8_RigidFrame.step'),root))
 open(os.path.join(ROOT,'review','refine_status.txt'),'a').write('COMPLETE\n')
except:open(os.path.join(ROOT,'review','refine_status.txt'),'a').write(traceback.format_exc())

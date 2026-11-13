import adsk.core,adsk.fusion,os,traceback
ROOT=r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame'
try:
 open(os.path.join(ROOT,'review','clearance_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;assert app.activeDocument.name.startswith('Orbo V8')
 def bore(prefix,centres,z,height,radius,label):
  o=next(o for o in root.occurrences if o.name.startswith(prefix));c=o.component
  pi=c.constructionPlanes.createInput();pi.setByOffset(c.xYConstructionPlane,adsk.core.ValueInput.createByReal(z/10));plane=c.constructionPlanes.add(pi)
  sk=c.sketches.add(plane);sk.name=label
  for x,y in centres:sk.sketchCurves.sketchCircles.addByCenterRadius(adsk.core.Point3D.create(x/10,y/10,0),radius/10)
  profiles=adsk.core.ObjectCollection.create()
  for p in sk.profiles:profiles.add(p)
  ei=c.features.extrudeFeatures.createInput(profiles,adsk.fusion.FeatureOperations.CutFeatureOperation);ei.setDistanceExtent(False,adsk.core.ValueInput.createByReal(height/10));ei.participantBodies=[c.bRepBodies.item(0)]
  feature=c.features.extrudeFeatures.add(ei);feature.name=label;sk.isVisible=False;plane.isLightBulbOn=False
 bore('V8 Lower front',[(-30,-43),(30,-43)],18.9,30.2,1.7,'Open through-bolt bores across reinforced front crossbar')
 for sx,side in [(-1,'L'),(1,'R')]:bore('V8 Servo frame bracket '+side,[(sx*33,-20),(sx*33,20)],91,3.5,3.2,'Clear M3 socket heads beside tower gussets')
 exec(compile(open(os.path.join(ROOT,'scripts','check_fasteners.py'),encoding='utf-8').read(),'check_fasteners.py','exec'))
 exec(compile(open(os.path.join(ROOT,'scripts','save_exports.py'),encoding='utf-8').read(),'save_exports.py','exec'))
 open(os.path.join(ROOT,'review','clearance_status.txt'),'a').write('COMPLETE\n')
except:open(os.path.join(ROOT,'review','clearance_status.txt'),'a').write(traceback.format_exc())

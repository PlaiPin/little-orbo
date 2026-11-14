import adsk.core,adsk.fusion,os,json,traceback,math,re
ROOT=r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame'
def pt(x,y,z):return adsk.core.Point3D.create(x/10,y/10,z/10)
def vec(x,y,z):return adsk.core.Vector3D.create(x,y,z)
def cyl(a,b,r):return tm.createCylinderOrCone(pt(*a),r/10,pt(*b),r/10)
def overlap(a,b):
 aa=a.boundingBox;bb=b.boundingBox
 return all(min(getattr(aa.maxPoint,k),getattr(bb.maxPoint,k))-max(getattr(aa.minPoint,k),getattr(bb.minPoint,k))>1e-6 for k in ('x','y','z'))
def volume(a,b):
 if not overlap(a,b):return 0
 s=tm.copy(a)
 try:
  tm.booleanOperation(s,b,adsk.fusion.BooleanTypes.IntersectionBooleanType);return s.volume*1000 if s.isValid else 0
 except:return -1
try:
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent
 assert app.activeDocument.name.startswith('Orbo V8')
 des.activateRootComponent();app.userInterface.activeSelections.clear()
 em=des.exportManager
 def render(name,eye,target,up=(0,0,1)):
  cam=app.activeViewport.camera;cam.isSmoothTransition=False;cam.eye=pt(*eye);cam.target=pt(*target);cam.upVector=vec(*up);app.activeViewport.camera=cam;app.activeViewport.fit();app.activeViewport.refresh();adsk.doEvents();app.activeViewport.saveAsImageFile(os.path.join(ROOT,'renders',name+'.png'),1600,1400)
 for o in root.occurrences:o.isLightBulbOn=not o.name.startswith(('R2','V8 Torso','V8 Head','V8 Neck'))
 render('reinforced_frame',(350,-500,310),(0,0,80))
 for o in root.occurrences:o.isLightBulbOn=o.name.startswith(('V8 Motor','V8 Lower','V8 Battery shelf','V8 Electronics','V8 Servo frame','V8 Internal','Hardware frame','Hardware carrier','Hardware front frame','Hardware M3 insert'))
 render('structure_and_connections',(350,-500,310),(0,0,80))
 for o in root.occurrences:o.isLightBulbOn=o.name.startswith(('V8 Electronics','V8 Servo frame bracket R','07R','V8 Internal','Hardware frame bracket R','Hardware carrier to servo tower R'))
 render('servo_tower_connections',(300,-350,270),(30,0,100))
 for o in root.occurrences:o.isLightBulbOn=o.name.startswith(('V8 Torso','V8 Electronics','V8 Servo frame','07','DDv3_PH_Raspberry','DDv3_PH_Pi5'))
 render('open_neck_assembly',(250,-300,420),(0,0,80))
 for o in root.occurrences:o.isLightBulbOn=True
 render('assembled',(380,-520,350),(0,0,145))
 json.dump([dict(name=o.component.name,box=[[v*10 for v in p.asArray()] for p in (b.boundingBox.minPoint,b.boundingBox.maxPoint)]) for o in root.occurrences for b in o.bRepBodies],open(os.path.join(ROOT,'review','inventory.json'),'w'),indent=2)
 json.dump(dict(joints=[j.name for j in root.asBuiltJoints]),open(os.path.join(ROOT,'review','joints.json'),'w'),indent=2)
 open(os.path.join(ROOT,'review','polish_status.txt'),'a').write('COMPLETE\n')
except:open(os.path.join(ROOT,'review','polish_status.txt'),'a').write(traceback.format_exc())

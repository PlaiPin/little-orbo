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
 open(os.path.join(ROOT,'review','final_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;tm=adsk.fusion.TemporaryBRepManager.get()
 assert app.activeDocument.name.startswith('Orbo V8')

 for x,y,z,label in [(x,y,79.5,'deck') for x in (-33,33) for y in (-20,20)]+[(x,y,118.5,'carrier seat') for x in (-53,53) for y in (-24,24)]+[(x,-12,43.5,'front brace') for x in (-30,30)]:
  outer=cyl((x,y,z),(x,y,z+5),2.1);tm.booleanOperation(outer,cyl((x,y,z-.1),(x,y,z+5.1),1.25),adsk.fusion.BooleanTypes.DifferenceBooleanType)
  o=root.occurrences.addNewComponent(adsk.core.Matrix3D.create());o.component.name=f'Hardware M3 insert nominal occupied volume {label} X{x} Y{y}';o.isGrounded=True;o.component.bRepBodies.add(outer)
 # Verify all retained non-frame geometry remains in the same location.
 prevdoc=next(d for d in app.documents if d.name.startswith('Orbo V7'));prev=adsk.fusion.Design.cast(prevdoc.products.itemByProductType('DesignProductType'))
 current={o.component.name:o.bRepBodies.item(0) for o in root.occurrences};checks=[]
 changed=('V7 Motor','V7 Electronics','V7 Battery shelf','V7 Servo frame','V7 Internal','Hardware M3x30','Hardware frame bracket')
 for o in prev.rootComponent.occurrences:
  n=o.component.name
  if n.startswith(changed):continue
  target='V8 '+n[3:] if n.startswith('V7 ') else n;a=o.bRepBodies.item(0);b=current[target]
  pa=a.physicalProperties;pb=b.physicalProperties
  aa=sorted(f.area for f in a.faces);ba=sorted(f.area for f in b.faces)
  bounds_a=list(a.boundingBox.minPoint.asArray())+list(a.boundingBox.maxPoint.asArray());bounds_b=list(b.boundingBox.minPoint.asArray())+list(b.boundingBox.maxPoint.asArray())
  same=len(aa)==len(ba) and max([abs(x-y) for x,y in zip(aa,ba)]+[0])<1e-6 and max(abs(x-y) for x,y in zip(bounds_a,bounds_b))<1e-6 and abs(pa.volume-pb.volume)<1e-6 and pa.centerOfMass.distanceTo(pb.centerOfMass)<1e-6
  checks.append(dict(component=target,geometry_and_position_match=same,volume_delta_mm3=(pb.volume-pa.volume)*1000,bounds_delta_mm=max(abs(x-y) for x,y in zip(bounds_a,bounds_b))*10))
 json.dump(checks,open(os.path.join(ROOT,'review','retained_geometry_checks.json'),'w'),indent=2)
 assert all(x['geometry_and_position_match'] for x in checks)
 points={}
 for side in ('L','R'):
  ci=root.constructionPoints.createInput();ci.setByPoint(pt(-85 if side=='L' else 85,5.5,117));cp=root.constructionPoints.add(ci);cp.isLightBulbOn=False;points[side]=cp
 des.designType=adsk.fusion.DesignTypes.ParametricDesignType
 deck=next(o for o in root.occurrences if o.name.startswith('V8 Electronics'))
 def rigid(a,b,name):
  a.isGrounded=False;inp=root.asBuiltJoints.createInput(a,b,None);inp.setAsRigidJointMotion();j=root.asBuiltJoints.add(inp);j.name=name
 for side in ('L','R'):
  bracket=next(o for o in root.occurrences if o.name.startswith('V8 Servo frame bracket '+side));hub=next(o for o in root.occurrences if o.name.startswith('V8 Arm '+side+' keyed'))
  rigid(bracket,deck,'Frame bracket '+side+' to deck')
  servo=next(o for o in root.occurrences if o.name.startswith('07'+side) and '9g servo envelope' in o.name);rigid(servo,bracket,'Servo '+side+' to frame bracket')
  hub.isGrounded=False;inp=root.asBuiltJoints.createInput(hub,bracket,adsk.fusion.JointGeometry.createByPoint(points[side]));inp.setAsRevoluteJointMotion(adsk.fusion.JointDirections.XAxisJointDirection);j=root.asBuiltJoints.add(inp);j.name='Frame driven arm '+side+' -30 to +10 deg';lim=j.jointMotion.rotationLimits;lim.minimumValue=math.radians(-30);lim.maximumValue=math.radians(10);lim.isMinimumValueEnabled=True;lim.isMaximumValueEnabled=True
  for o in root.occurrences:
   if o.name.startswith(('V8 Arm '+side+' smooth','07'+side+' servo supplied horn','Hardware arm '+side)):rigid(o,hub,'Arm '+side+' assembly - '+o.component.name)
 plate=next(o for o in root.occurrences if o.name.startswith('V8 Motor'))
 rigid(deck,plate,'Braced cage clamped to motor plate by four through bolts')
 for o in root.occurrences:
  if o.name.startswith(('V8 Lower','V8 Battery shelf')):rigid(o,plate,'Frame stack - '+o.component.name)
 carrier=next(o for o in root.occurrences if o.name.startswith('V8 Internal'));rigid(carrier,deck,'Neck carrier tied to servo towers and central mast')
 root.isJointsFolderLightBulbOn=False;root.isConstructionFolderLightBulbOn=False;root.isOriginFolderLightBulbOn=False
 em=des.exportManager;files=[]
 for o in root.occurrences:
  n=o.component.name
  if not n.startswith(('V8 Torso','V8 Internal','V8 Motor plate','V8 Electronics','V8 Front','V8 Rear','V8 Battery shelf','V8 Bottom','V8 Servo frame','V8 Arm','V8 Lower')):continue
  fn=re.sub('[^A-Za-z0-9_-]+','_',n)[:110]+'.stl';options=em.createSTLExportOptions(o.component.bRepBodies.item(0),os.path.join(ROOT,'STL',fn));options.unitType=adsk.fusion.DistanceUnits.MillimeterDistanceUnits;options.meshRefinement=adsk.fusion.MeshRefinementSettings.MeshRefinementHigh;em.execute(options);files.append(dict(component=n,file=fn))
 json.dump(files,open(os.path.join(ROOT,'review','print_parts.json'),'w'),indent=2)
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
 em.execute(em.createFusionArchiveExportOptions(os.path.join(ROOT,'CAD','Orbo_V8_RigidFrame.f3d')));em.execute(em.createSTEPExportOptions(os.path.join(ROOT,'CAD','Orbo_V8_RigidFrame.step'),root))
 json.dump([dict(name=o.component.name,box=[[v*10 for v in p.asArray()] for p in (b.boundingBox.minPoint,b.boundingBox.maxPoint)]) for o in root.occurrences for b in o.bRepBodies],open(os.path.join(ROOT,'review','inventory.json'),'w'),indent=2)
 json.dump(dict(joints=[j.name for j in root.asBuiltJoints]),open(os.path.join(ROOT,'review','joints.json'),'w'),indent=2)
 open(os.path.join(ROOT,'review','final_status.txt'),'a').write('COMPLETE\n')
except:open(os.path.join(ROOT,'review','final_status.txt'),'a').write(traceback.format_exc())

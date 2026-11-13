"""Run in Fusion after fusion_open_v9.py and the OCP build/export."""
import adsk.core,adsk.fusion,json,os,traceback
ROOT=r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v9_display_mount'
def pt(x,y,z):return adsk.core.Point3D.create(x/10,y/10,z/10)
try:
 app=adsk.core.Application.get();doc=app.activeDocument
 assert doc.name.startswith('Orbo V9'),doc.name
 des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent
 assert not any(o.component.name.startswith('Orbo V9') for o in root.occurrences),'Additions already imported'
 before=json.load(open(os.path.join(ROOT,'review','fusion_baseline_signature.json')))
 old=list(root.occurrences)
 app.importManager.importToTarget(app.importManager.createSTEPImportOptions(os.path.join(ROOT,'CAD','V9_Display_Mount_Additions.step')),root)
 added=[o for o in root.occurrences if o not in old]
 for o in added:o.isGrounded=True
 after=[dict(name=o.component.name,bodies=[dict(name=b.name,volume=b.volume,area=b.area,bounds=[list(p.asArray()) for p in [b.boundingBox.minPoint,b.boundingBox.maxPoint]],faces=b.faces.count) for b in o.bRepBodies]) for o in old]
 checks=dict(original_occurrences=len(old),original_bodies=sum(len(p['bodies']) for p in before),unchanged=before==after,as_built_joints=root.asBuiltJoints.count,added_root_occurrences=[o.component.name for o in added])
 json.dump(checks,open(os.path.join(ROOT,'review','fusion_retained_geometry.json'),'w'),indent=2)
 assert checks['unchanged'],'V8 signature changed'
 def render(n,eye,target):
  cam=app.activeViewport.camera;cam.isSmoothTransition=False;cam.cameraType=adsk.core.CameraTypes.OrthographicCameraType;cam.eye=pt(*eye);cam.target=pt(*target);cam.upVector=adsk.core.Vector3D.create(0,0,1)
  app.activeViewport.camera=cam;app.activeViewport.fit();app.activeViewport.refresh();adsk.doEvents();app.activeViewport.saveAsImageFile(os.path.join(ROOT,'renders',n+'.png'),1600,1400)
 for o in old:o.isLightBulbOn=o.component.name.startswith('V8 Internal neck carrier')
 render('display_mount_on_carrier',(230,-420,260),(0,-35,157))
 render('display_mount_rear',(230,350,260),(0,-35,157))
 for o in old:o.isLightBulbOn=not o.component.name.startswith(('R2 ','V8 Head','V8 Neck','V8 Torso','V8 Arm'))
 render('open_frame_with_display',(300,-450,270),(0,0,110))
 for o in old:o.isLightBulbOn=True
 render('unchanged_exterior',(280,-460,270),(0,0,140))
 em=des.exportManager;em.execute(em.createFusionArchiveExportOptions(os.path.join(ROOT,'CAD','Orbo_V9_DisplayMount.f3d')))
 em.execute(em.createSTEPExportOptions(os.path.join(ROOT,'CAD','Orbo_V9_DisplayMount.step'),root))
 open(os.path.join(ROOT,'review','fusion_status.txt'),'a').write('NATIVE F3D EXPORTED; ALL V8 BODIES UNCHANGED\n')
 # Leave an informative inspection view after saving the complete assembly.
 for o in old:o.isLightBulbOn=o.component.name.startswith('V8 Internal neck carrier')
 render('display_mount_on_carrier',(230,-420,260),(0,-35,157))
except:open(os.path.join(ROOT,'review','fusion_status.txt'),'a').write(traceback.format_exc())

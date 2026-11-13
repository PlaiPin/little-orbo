exec(open(r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame\scripts\audit_v8.py').read().split('try:\n open(')[0])
def record(group,pose,movers,fixed):
 hits=[]
 for n,a in movers:
  for on,b in fixed:
   v=volume(a,b)
   if v>.05:hits.append([n,on,v])
 report[group].append(dict(pose=pose,hits=hits))
 json.dump(report,open(os.path.join(ROOT,'review','assembly_checks.json'),'w'),indent=2)
 with open(os.path.join(ROOT,'review','assembly_status.txt'),'a') as f:f.write(group+' '+str(pose)+' '+str(hits)+'\n')
try:
 open(os.path.join(ROOT,'review','assembly_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;tm=adsk.fusion.TemporaryBRepManager.get();assert app.activeDocument.name.startswith('Orbo V8')
 parts=[(o.component.name,b) for o in root.occurrences for b in o.bRepBodies];struct=[(n,b) for n,b in parts if not n.startswith('Hardware')]
 report={k:[] for k in ('shell_entry','servo_entry','pi_entry','carrier_entry','cage_entry','shelf_entry','tools')}
 def body(prefix):return next(b for n,b in parts if n.startswith(prefix))
 torso=body('V8 Torso');cage=body('V8 Electronics');carrier=body('V8 Internal');shelf=body('V8 Battery shelf')
 core=[(n,b) for n,b in parts if n.startswith(('V8 Motor','V8 Electronics','V8 Battery shelf','V8 Lower','Hardware frame through','Hardware frame washer','Hardware frame top nut','Hardware front frame shelf'))]
 report['shell_check_scope']=[n for n,b in core]
 for dz in list(range(-135,0,5))+[-2,-1,-.5,0]:record('shell_entry',{'z':dz},[('shell',moved(torso,z=dz))],core)
 for sx,side in [(-1,'L'),(1,'R')]:
  pod=[(n,b) for n,b in parts if n.startswith(('07'+side,'V8 Servo frame bracket '+side))]
  fixed=[(n,b) for n,b in struct if n.startswith(('V8 Torso','V8 Electronics','V8 Battery','V8 Lower','V8 Motor','V3 Battery'))]
  for dx,dz in [(-sx*6,z) for z in (80,60,40,20,10,5,0)]+[(sx*x,0) for x in (-4,-2,0)]:record('servo_entry',dict(side=side,dx=dx,dz=dz),[(n,moved(b,x=dx,z=dz)) for n,b in pod],fixed)
 fixed=[(n,b) for n,b in struct if n.startswith(('V8 Torso','V8 Electronics','V8 Servo frame','07'))]
 pis=[(n,b) for n,b in parts if 'RaspberryPi5' in n or 'ActiveCooler' in n]
 for dz in (100,80,60,40,20,10,5,0):record('pi_entry',{'z':dz},[(n,moved(b,z=dz)) for n,b in pis],fixed)
 fixed=[(n,b) for n,b in struct if n.startswith(('V8 Torso','V8 Electronics','V8 Servo frame','07','DDv3_PH','V8 Central'))]
 for dz in (100,60,30,10,5,2,1,0):record('carrier_entry',{'z':dz},[('carrier',moved(carrier,z=dz))],fixed)
 fixed=[(n,b) for n,b in parts if n.startswith(('V8 Motor','V8 Lower','V8 Battery shelf','V3 Battery','DDv3_DC','DDv3_TB3','Hardware frame through'))]
 cage_set=[('cage',cage)]+[(n,b) for n,b in parts if n.startswith(('DDv3_PH_U2D2','DDv3_PH_Pololu','DDv3_Spacer_M2x6'))]
 for dz in (100,60,30,15,5,1,0):record('cage_entry',{'z':dz},[(n,moved(b,z=dz)) for n,b in cage_set],fixed)
 fixed=[(n,b) for n,b in parts if n.startswith(('V8 Motor','V8 Lower','DDv3_DC','DDv3_TB3','Hardware frame through'))]
 for dz in (80,40,20,5,1,0):record('shelf_entry',{'z':dz},[('shelf',moved(shelf,z=dz))],fixed)
 # Fasten the core before the Pi, servo modules and head are installed.
 core_fixed=[(n,b) for n,b in struct if not n.startswith(('V8 Head','V8 Neck','V8 Internal','V8 Servo','V8 Arm','07','R2','DDv3_PH_Raspberry','DDv3_PH_Pi5'))]
 for x in (-30,30):
  for y in (-43,49):
   record('tools',dict(kind='frame top nut socket',x=x,y=y),[('socket',cyl((x,y,88),(x,y,160),4))],core_fixed)
   record('tools',dict(kind='frame bolt underside driver',x=x,y=y),[('driver',cyl((x,y,-60),(x,y,7),2))],struct)
  fixed=[(n,b) for n,b in core_fixed if not n.startswith(('V8 Electronics','DDv3_PH_U2D2','DDv3_PH_Pololu'))]
  record('tools',dict(kind='shelf to front brace driver',x=x),[('driver',cyl((x,-12,55),(x,-12,130),2))],fixed)
 for sx,side in [(-1,'L'),(1,'R')]:
  fixed=[(n,b) for n,b in struct if not n.startswith(('V8 Head','V8 Neck','V8 Internal','R2','DDv3_PH_Raspberry','DDv3_PH_Pi5'))]
  for y in (-20,20):record('tools',dict(kind='servo foot driver',side=side,y=y),[('driver',cyl((sx*33,y,94),(sx*33,y,175),2))],fixed)
  fixed=[(n,b) for n,b in struct if not n.startswith(('V8 Head','V8 Neck','R2'))]
  for y in (-24,24):record('tools',dict(kind='carrier tie driver',side=side,y=y),[('driver',cyl((sx*53,y,131),(sx*53,y,185),2))],fixed)
  for y in (-42,42):record('tools',dict(kind='concealed head driver',side=side,y=y),[('driver',cyl((sx*(-5 if y<0 else 12),5 if y<0 else 61,-70),(sx*62,y,124),2))],struct)
 with open(os.path.join(ROOT,'review','assembly_status.txt'),'a') as f:f.write('COMPLETE\n')
except:
 with open(os.path.join(ROOT,'review','assembly_status.txt'),'a') as f:f.write(traceback.format_exc())

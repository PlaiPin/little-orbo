import adsk.core,adsk.fusion,os,json,math,traceback
ROOT=r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame'
def pt(x,y,z):return adsk.core.Point3D.create(x/10,y/10,z/10)
def vec(x,y,z):return adsk.core.Vector3D.create(x,y,z)
def box(x0,y0,z0,x1,y1,z1):return tm.createBox(adsk.core.OrientedBoundingBox3D.create(pt((x0+x1)/2,(y0+y1)/2,(z0+z1)/2),vec(1,0,0),vec(0,1,0),(x1-x0)/10,(y1-y0)/10,(z1-z0)/10))
def cyl(a,b,r):return tm.createCylinderOrCone(pt(*a),r/10,pt(*b),r/10)
def op(a,b,k):
 if not tm.booleanOperation(a,b,k):raise RuntimeError('Boolean failure')
 return a
def cut(a,b):return op(a,b,adsk.fusion.BooleanTypes.DifferenceBooleanType)
def union(a,b):return op(a,b,adsk.fusion.BooleanTypes.UnionBooleanType)
def common(a,b):return op(a,b,adsk.fusion.BooleanTypes.IntersectionBooleanType)
def beam(a,b,width,depth,side=(0,1,0)):
 d=vec(*[b[i]-a[i] for i in range(3)]);length=d.length;d.normalize()
 return tm.createBox(adsk.core.OrientedBoundingBox3D.create(pt(*[(a[i]+b[i])/2 for i in range(3)]),d,vec(*side),length/10,depth/10,width/10))
def mirror(s):
 m=adsk.core.Matrix3D.create();m.setCell(0,0,-1);tm.transform(s,m);return s
def hexagon(x,y,z0,z1,af):
 s=cyl((x,y,z0),(x,y,z1),af/math.sqrt(3))
 for angle in (0,60,120):
  mask=box(x-af/2,y-af,z0-1,x+af/2,y+af,z1+1);m=adsk.core.Matrix3D.create();m.setToRotation(math.radians(angle),vec(0,0,1),pt(x,y,0));tm.transform(mask,m);common(s,mask)
 return s
def head_paths(s):
 for sx in (-1,1):
  for y in (-42,42):cut(s,cyl((sx*(-5 if y<0 else 12),5 if y<0 else 61,-70),(sx*62,y,124),2.6))
 return s
def add(n,s,source=None):
 o=root.occurrences.addNewComponent(adsk.core.Matrix3D.create());o.component.name=n;o.isGrounded=True;b=o.component.bRepBodies.add(s);b.name=n
 if source:source_map.append((b,source))
 return o,b
def socket_screw(n,x,y,seat,length,r=1.5,headr=2.75,headh=3,up=False):
 end=seat+length if up else seat-length
 s=cyl((x,y,seat),(x,y,end),r);union(s,cyl((x,y,seat),(x,y,seat-headh if up else seat+headh),headr));add(n,s)
def log(s):
 with open(os.path.join(ROOT,'review','build_status.txt'),'a') as f:f.write(str(s)+'\n')
try:
 open(os.path.join(ROOT,'review','build_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();tm=adsk.fusion.TemporaryBRepManager.get()
 prevdoc=next(d for d in app.documents if d.name.startswith('Orbo V7'));prev=adsk.fusion.Design.cast(prevdoc.products.itemByProductType('DesignProductType'))
 old=[(o.component.name,tm.copy(b),b) for o in prev.rootComponent.occurrences for b in o.bRepBodies]
 doc=app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType);doc.name='Orbo V8 - braced internal frame and supported servo towers';des=adsk.fusion.Design.cast(app.activeProduct);des.designType=adsk.fusion.DesignTypes.DirectDesignType;des.unitsManager.distanceDisplayUnits=adsk.fusion.DistanceUnits.MillimeterDistanceUnits;root=des.rootComponent;source_map=[]
 changed=[]
 for oldname,s,sb in old:
  if oldname.startswith(('Hardware M3x30','Hardware frame bracket')):continue
  n='V8 '+oldname[3:] if oldname.startswith('V7 ') else oldname
  if n.startswith('V8 Motor plate'):
   for x in (-30,30):
    for y in (-43,49):cut(s,cyl((x,y,6),(x,y,19.1),1.7))
   changed.append(n)
  elif n.startswith('V8 Battery shelf'):
   for x in (-30,30):cut(s,cyl((x,-12,48.9),(x,-12,52.1),1.7))
   # Stiff edge rails remain below the battery and clear the wheel envelopes.
   for xa,xb in [(-40.4,-37.6),(37.6,40.4)]:union(s,box(xa,-43,49,xb,49,55))
   head_paths(s);changed.append(n)
  elif n.startswith('V8 Electronics'):
   # Full bearing beneath each servo foot, and deeper longitudinal beams.
   for sx in (-1,1):
    wing=box(29,-26,78,49,26,85)
    if sx<0:mirror(wing)
    union(s,wing)
    rail=box(29,-47,78,37.5,53,85)
    if sx<0:mirror(rail)
    union(s,rail)
   # One-piece upper cage: four columns and triangulated front/rear/side walls.
   for y in (-43,49):
    ya,yb=(y-4,y+4);panel_y=y-2 if y<0 else y+2
    for x in (-30,30):union(s,box(x-6,ya,52,x+6,yb,82.1))
    for z0,z1 in ((52,56),(78,82.1)):union(s,box(-40.8,panel_y-2,z0,40.8,panel_y+2,z1))
    for a,b in [((-30,panel_y,54),(30,panel_y,80)),((30,panel_y,54),(-30,panel_y,80))]:
     br=beam(a,b,6,4);common(br,box(-36,ya,52,36,yb,82));union(s,br)
   for sx in (-1,1):
    xc=sx*39.6
    for z0,z1 in ((52,56),(78,82.1)):union(s,box(min(sx*38.4,sx*40.8),-45,z0,max(sx*38.4,sx*40.8),51,z1))
    for a,b in [((xc,-43,54),(xc,49,80)),((xc,49,54),(xc,-43,80))]:
     br=beam(a,b,6,2.4,(1,0,0));common(br,box(-41,-47,52,41,53,82));union(s,br)
   for x in (-30,30):
    for y in (-43,49):cut(s,cyl((x,y,51.9),(x,y,85.1),1.7))
   for x in (-33,33):
    for y in (-20,20):cut(s,cyl((x,y,78.5),(x,y,85.1),2.1))
   head_paths(s);changed.append(n)
   n='V8 Electronics cage - triangulated walls and full servo foot supports'
  elif n.startswith('V8 Servo frame bracket'):
   side='L' if 'bracket L' in n else 'R';sx=-1 if side=='L' else 1
   extra=box(29.5,-24,85,49,24,91)
   union(extra,box(43,-19,90.9,49.2,19,110.7))
   for yc in (-15.5,15.5):
    union(extra,beam((38,yc,91),(52,yc,109.5),9,7))
    union(extra,box(43,yc-3.5,105,56,yc+3.5,110.7))
   # Upright neck-carrier seats bridge the old gap with four removable joints.
   for ya,yb in [(-29,-18.5),(18.5,29)]:union(extra,box(48,ya,109,58,yb,124))
   common(extra,box(29.5,-29,85,58,29,124))
   if sx<0:mirror(extra)
   union(s,extra)
   for y in (-20,20):cut(s,cyl((sx*33,y,84.9),(sx*33,y,92),1.7))
   for y in (-24,24):cut(s,cyl((sx*53,y,117.5),(sx*53,y,124.1),2.1))
   head_paths(s);changed.append(n)
  elif n.startswith('V8 Internal neck carrier'):
   for sx in (-1,1):
    for y in (-24,24):
     tab=box(45.8,y-5,124,58,y+5,128)
     if sx<0:mirror(tab)
     union(s,tab);cut(s,cyl((sx*53,y,123.9),(sx*53,y,128.1),1.7))
   changed.append(n)
  add(n,s,sb)
 # Two lower end frames replace the slender lower spacers; the front frame
 # has fore-aft diagonal supports tied to two new shelf fasteners.
 for y,label in [(-43,'front'),(49,'rear')]:
  py=y-2 if y<0 else y+2;frame=box(-36,py-2,19,36,py+2,23)
  union(frame,box(-36,py-2,45,36,py+2,49))
  for x in (-30,30):union(frame,box(x-6,y-4,19,x+6,y+4,49))
  for a,b in [((-30,py,21),(30,py,47)),((30,py,21),(-30,py,47))]:
   br=beam(a,b,6,4);common(br,box(-36,y-4,19,36,y+4,49));union(frame,br)
  if y<0:
   for x in (-30,30):
    br=beam((x,-43,22),(x,-12,46),8,12,(1,0,0));common(br,box(x-6,-47,19,x+6,-7,49));union(frame,br)
    union(frame,box(x-6,-43,45,x+6,-7,49));union(frame,box(x-6,-17,42,x+6,-7,49));cut(frame,cyl((x,-12,41.9),(x,-12,49.1),2.1))
  for x in (-30,30):cut(frame,cyl((x,y,18.9),(x,y,49.1),1.7))
  head_paths(frame);add('V8 Lower '+label+' frame - braced compression supports',frame)
 for x in (-30,30):
  for y in (-43,49):
   socket_screw(f'Hardware frame through bolt M3x80 X{x} Y{y}',x,y,10,80,up=True)
   washer=cyl((x,y,85),(x,y,85.5),3.5);cut(washer,cyl((x,y,84.9),(x,y,85.6),1.6));add(f'Hardware frame washer M3 X{x} Y{y}',washer)
   nut=hexagon(x,y,85.5,87.9,5.5);cut(nut,cyl((x,y,85.4),(x,y,88),1.25));add(f'Hardware frame top nut M3 X{x} Y{y}',nut)
  socket_screw(f'Hardware front frame shelf M3x10 X{x}',x,-12,52,10)
 for sx,side in [(-1,'L'),(1,'R')]:
  for y in (-20,20):socket_screw(f'Hardware frame bracket {side} M3x12 Y{y}',sx*33,y,91,12)
  for y in (-24,24):socket_screw(f'Hardware carrier to servo tower {side} M3x10 Y{y}',sx*53,y,128,10)
 cache={}
 def appearance(a):
  if a.name not in cache:cache[a.name]=des.appearances.itemByName(a.name) or des.appearances.addByCopy(a,a.name)
  return cache[a.name]
 def key(f):
  p=f.pointOnFace;return(round(f.area,5),round(p.x,4),round(p.y,4),round(p.z,4))
 for b,sb in source_map:
  b.appearance=appearance(sb.appearance);faces={key(f):f.appearance for f in sb.faces}
  for f in b.faces:
   a=faces.get(key(f))
   if a:
    try:f.appearance=appearance(a)
    except:pass
 em=des.exportManager;em.execute(em.createFusionArchiveExportOptions(os.path.join(ROOT,'CAD','Orbo_V8_RigidFrame.f3d')));em.execute(em.createSTEPExportOptions(os.path.join(ROOT,'CAD','Orbo_V8_RigidFrame.step'),root))
 json.dump(dict(changed_components=changed),open(os.path.join(ROOT,'review','changes.json'),'w'),indent=2)
 json.dump([dict(name=o.component.name,box=[[v*10 for v in p.asArray()] for p in (b.boundingBox.minPoint,b.boundingBox.maxPoint)]) for o in root.occurrences for b in o.bRepBodies],open(os.path.join(ROOT,'review','inventory.json'),'w'),indent=2)
 log('COMPLETE')
except:log(traceback.format_exc())

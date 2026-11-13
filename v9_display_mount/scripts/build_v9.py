"""Add-only V9 mechanical design. Units mm; +Z up; face looks toward -Y.
Requires OCP 8 (installed in the existing local CAD tooling folder).
No V8 geometry is edited. Manufacturer STEP is rigidly transformed only.
"""
import sys,json,pathlib,hashlib,math,functools
sys.path.insert(0,r'C:\Users\sahlu\Desktop\conversion files\cad_cleanup_tools')
from inspect_geometry import ROOT,bbox,load_brep
from OCP.gp import gp_Pnt,gp_Dir,gp_Ax2,gp_Trsf,gp_Vec
from OCP.BRepPrimAPI import BRepPrimAPI_MakeBox,BRepPrimAPI_MakeCylinder,BRepPrimAPI_MakePrism
from OCP.BRepBuilderAPI import BRepBuilderAPI_MakePolygon,BRepBuilderAPI_MakeFace,BRepBuilderAPI_Transform
from OCP.BRepAlgoAPI import BRepAlgoAPI_Fuse,BRepAlgoAPI_Cut,BRepAlgoAPI_Common
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID
from OCP.TopoDS import TopoDS_Compound
from OCP.BRep import BRep_Builder
from OCP.BRepTools import BRepTools
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer
from OCP.STEPControl import STEPControl_Writer,STEPControl_AsIs,STEPControl_Reader
from OCP.STEPCAFControl import STEPCAFControl_Reader,STEPCAFControl_Writer
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool,XCAFDoc_ColorType
from OCP.TDataStd import TDataStd_Name
from OCP.TopLoc import TopLoc_Location
from OCP.Quantity import Quantity_Color,Quantity_TOC_RGB
from OCP.ShapeUpgrade import ShapeUpgrade_UnifySameDomain
from OCP.collections import Sequence_TDF_Label

def box(x0,y0,z0,x1,y1,z1):return BRepPrimAPI_MakeBox(gp_Pnt(x0,y0,z0),x1-x0,y1-y0,z1-z0).Shape()
def cyl(x,y,z0,z1,r):return BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(x,y,z0),gp_Dir(0,0,1)),r,z1-z0).Shape()
def boolean(cls,a,b):
 op=cls(a,b);op.Build()
 if not op.IsDone():raise RuntimeError('Boolean failed')
 return op.Shape()
def fuse(a,b):return boolean(BRepAlgoAPI_Fuse,a,b)
def cut(a,b):return boolean(BRepAlgoAPI_Cut,a,b)
def vol(s):
 p=GProp_GProps();BRepGProp.VolumeProperties_s(s,p);return abs(p.Mass())
@functools.lru_cache(maxsize=4096)
def solids(s):
 e=TopExp_Explorer(s,TopAbs_SOLID);out=[]
 while e.More():out.append(e.Current());e.Next()
 return out
def intersects(a,b):
 aa=bbox(a);bb=bbox(b)
 if any(min(aa[i+3],bb[i+3])-max(aa[i],bb[i])<1e-6 for i in range(3)):return 0.
 if len(solids(a))>1:
  # A zero intersection of an enclosing box proves its contained body clear.
  return sum(intersects(s,b) for s in solids(a) if intersects(box(*bbox(s)),b)>.000001)
 if len(solids(b))>1:return intersects(b,a)
 return vol(boolean(BRepAlgoAPI_Common,a,b))
def moved(s,dx=0,dy=0,dz=0):
 t=gp_Trsf();t.SetTranslation(gp_Vec(dx,dy,dz));return BRepBuilderAPI_Transform(s,t,True).Shape()
def prism(points,v):
 p=BRepBuilderAPI_MakePolygon()
 for xyz in points:p.Add(gp_Pnt(*xyz))
 p.Close();return BRepPrimAPI_MakePrism(BRepBuilderAPI_MakeFace(p.Wire()).Face(),gp_Vec(*v)).Shape()
def hexprism(x,y,z0,z1,af):
 r=af/math.sqrt(3)
 return prism([(x+r*math.cos(math.radians(a)),y+r*math.sin(math.radians(a)),z0) for a in range(0,360,60)],(0,0,z1-z0))
def compound(shapes):
 c=TopoDS_Compound();b=BRep_Builder();b.MakeCompound(c)
 for s in shapes:b.Add(c,s)
 return c
def step(s,p):
 w=STEPControl_Writer();w.Transfer(s,STEPControl_AsIs);w.Write(str(p))
def named_step(parts,path,source=None):
 doc=TDocStd_Document(TCollection_ExtendedString('V9'));st=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
 if source:
  r=STEPCAFControl_Reader();r.SetNameMode(True);r.SetColorMode(True);r.ReadFile(str(source));r.Transfer(doc)
  ls=Sequence_TDF_Label();st.GetFreeShapes(ls);assert ls.Length()==1
  root=ls.Value(1)
 else:root=st.NewShape();st.SetShape(root,compound([]))
 TDataStd_Name.Set_s(root,TCollection_ExtendedString('Orbo V9 - single AMOLED display mount'))
 ct=XCAFDoc_DocumentTool.ColorTool_s(doc.Main())
 for n,s,color in parts:
  lab=st.AddShape(s,False);TDataStd_Name.Set_s(lab,TCollection_ExtendedString(n));ct.SetColor(lab,Quantity_Color(*color,Quantity_TOC_RGB),XCAFDoc_ColorType.XCAFDoc_ColorGen)
  inst=st.AddComponent(root,lab,TopLoc_Location());TDataStd_Name.Set_s(inst,TCollection_ExtendedString(n))
 st.UpdateAssemblies();w=STEPCAFControl_Writer();w.SetNameMode(True);w.SetColorMode(True);w.Transfer(doc);w.Write(str(path))

def build():
 parts=[]
 # Clamp on the unchanged front carrier rail: rail Y=-46..-40, Z=124..128.
 saddle=box(-33,-53,128,33,-33,131)
 for ya,yb in [(-49,-46.3),(-39.7,-37)]:saddle=fuse(saddle,box(-32,ya,124.4,32,yb,128.1))
 for x in [-20,20]:
  saddle=fuse(saddle,box(x-3,-45,130.9,x+3,-39,161.5))
  saddle=fuse(saddle,prism([(x-3,-39,131),(x-3,-34,131),(x-3,-39,154)],(6,0,0)))
 # 57.50 x 24.50 metal case. Active screen offset is intentionally respected.
 # Final rigid transform x'=z+1.25, y'=-y-48.1, z'=x+176.7.
 xmin,xmax=-23.21,34.29;zmin,zmax=164.45,188.95
 left,right=xmin-4.5,xmax+4.5
 saddle=fuse(saddle,box(left-4.2,-53.7,158,right+4.2,-39,164.15))
 # Shallow edge tracks, full electronics/header area remains open at rear.
 for x0,x1 in [(xmin-3.3,xmin-.3),(xmax+.3,xmax+3.3)]:
  saddle=fuse(saddle,box(x0,-53.7,164.1,x1,-47.5,189.25))
 # front side lips act on metal rim only, never the active area
 for x0,x1 in [(xmin-3.3,xmin+1.1),(xmax-1.1,xmax+3.3)]:
  saddle=fuse(saddle,box(x0,-53.7,164.1,x1,-53.1,189.25))
 # top-retainer bolt columns and side-access captive M3 nut slots
 for x in [left,right]:
  saddle=fuse(saddle,cyl(x,-50.6,161,189.25,4.2))
  saddle=cut(saddle,cyl(x,-50.6,180,190,1.7))
  saddle=cut(saddle,hexprism(x,-50.6,183,185.7,5.7))
  # load nut horizontally from rear
  saddle=cut(saddle,box(x-3.3,-50.6,183,x+3.3,-46,185.7))
 # Narrow bottom front lip, below active screen
 saddle=fuse(saddle,box(xmin-.3,-53.7,164,xmax+.3,-53.1,165.6))
 # USB-C connector and plug exit at the right end; retain a front structural web.
 saddle=cut(saddle,box(xmax-.5,-49.3,170.4,right+5,-35,183))
 # Cable tie slots in uprights for strain relief
 for x in [-20,20]:saddle=cut(saddle,box(x-1.8,-46,144,x+1.8,-38,146.5))
 jaw=box(-33,-53,121,33,-33,124)
 for x in [-25,25]:
  for y in [-50,-36]:
   saddle=cut(saddle,cyl(x,y,123,135,1.7));jaw=cut(jaw,cyl(x,y,117,125,1.7))
   # Local access relief above the nut seat, especially beside the gussets.
   saddle=cut(saddle,cyl(x,y,131,157.9,3.5))
 # Retainer: separate removable top bar, 0.3 nominal case clearance.
 cap=box(left-4.2,-53.7,189.25,right+4.2,-47.5,192.25)
 cap=fuse(cap,box(xmin+1.4,-53.7,187.8,xmax-1.4,-53.1,189.3))
 for x in [left,right]:cap=cut(cap,cyl(x,-50.6,187,194,1.7))
 # Recess upper holder by 4 mm to clear the curved mask at its upper right edge.
 lower=boolean(BRepAlgoAPI_Common,saddle,box(-100,-100,100,100,100,158))
 upper=boolean(BRepAlgoAPI_Common,saddle,box(-100,-100,158,100,100,210))
 saddle=fuse(lower,moved(upper,dy=4));cap=moved(cap,dy=4)
 for n,s in [('V9 Display pedestal and carrier saddle',saddle),('V9 Carrier lower clamp jaw',jaw),('V9 Display removable top retainer',cap)]:
  unify=ShapeUpgrade_UnifySameDomain(s,True,True,True);unify.Build();parts.append((n,unify.Shape(),(.1,.48,.64)))
 for x in [-25,25]:
  for y in [-50,-36]:
   # Installed from below on the bench, so the upper cradle cannot block a driver.
   screw=fuse(cyl(x,y,121,137,1.5),cyl(x,y,118,121,2.75))
   nut=cut(hexprism(x,y,131,133.4,5.5),cyl(x,y,130,134,1.5))
   parts.extend([(f'V9 Hardware clamp M3x16 X{x} Y{y}',screw,(.5,.5,.5)),(f'V9 Hardware clamp M3 nut X{x} Y{y}',nut,(.5,.5,.5))])
 for x in [left,right]:
  screw=fuse(cyl(x,-50.6,182.25,192.25,1.5),cyl(x,-50.6,192.25,195.25,2.75))
  nut=cut(hexprism(x,-50.6,183.15,185.55,5.5),cyl(x,-50.6,183,186,1.5))
  parts.extend([(f'V9 Hardware retainer M3x10 X{x}',moved(screw,dy=4),(.5,.5,.5)),(f'V9 Hardware retainer M3 nut X{x}',moved(nut,dy=4),(.5,.5,.5))])
 t=gp_Trsf();t.SetValues(0,0,1,1.25,0,-1,0,-48.1,1,0,0,176.7)
 vendor=[]
 for a in json.loads((ROOT/'review/display_solids.json').read_text()):
  s=load_brep(ROOT/f"review/display_{a['i']:03}.brep")
  vendor.append(BRepBuilderAPI_Transform(s,t,True).Shape())
 parts.append(('V9 Waveshare ESP32-S3-AMOLED-1.91-M manufacturer STEP reference',compound(vendor),(.15,.15,.17)))
 return parts

def validate(parts):
 baseline=[(a['name'],load_brep(ROOT/'baseline_v8/review/final_breps'/a['brep'])) for a in json.loads((ROOT/'baseline_v8/review/step_validation.json').read_text())]
 report={'units':'mm','baseline_bodies':len(baseline),'new_parts':[],'interference_with_v8':[],'new_part_interference':[],'insertion_samples':[]}
 for n,s,c in parts:
  print('CHECKING',n,flush=True)
  e=TopExp_Explorer(s,TopAbs_SOLID);count=0
  while e.More():count+=1;e.Next()
  report['new_parts'].append(dict(name=n,valid=BRepCheck_Analyzer(s,False).IsValid(),solids=count,volume_mm3=vol(s),bounds=bbox(s)))
  for m,b in baseline:
   v=intersects(s,b)
   if v>.02:report['interference_with_v8'].append([n,m,v]);print('COLLISION',n,m,v,flush=True)
 for i,(n,a,c) in enumerate(parts):
  for m,b,cc in parts[i+1:]:
   v=intersects(a,b)
   if v>.02:report['new_part_interference'].append([n,m,v]);print('NEW PAIR COLLISION',n,m,v,flush=True)
 (ROOT/'review/static_checks.json').write_text(json.dumps(report,indent=2))
 # Install new assembly with head/mask/graphics/diffuser removed.
 fixed=[(n,b) for n,b in baseline if not any(k in n for k in ['Head -','Neck diffuser','R2 ','hidden head','captive head'])]
 for dz in [0,2,5,10,20,35,50,75]:
  moving=moved(parts[0][1],dz=dz);hits=[]
  for n,b in fixed:
   v=intersects(moving,b)
   if v>.02:hits.append([n,v])
  report['insertion_samples'].append(dict(part='pedestal from above, head removed',dz=dz,hits=hits))
 # Display slides vertically into open cradle before top retainer is installed.
 for dz in [0,1,3,8,15,25,35]:
  v=intersects(moved(parts[-1][1],dz=dz),parts[0][1]);report['insertion_samples'].append(dict(part='display into cradle',dz=dz,hits=[] if v<.02 else [['pedestal',v]]))
 gauge=box(34.8,-45,170.7,51.8,-35,182.7)
 report['usb_plug_gauge']={'dimensions_mm':[17,10,12],'bounds':bbox(gauge),'hits':[]}
 for n,s,c in parts[:-1]:
  v=intersects(gauge,s)
  if v>.02:report['usb_plug_gauge']['hits'].append([n,v])
 report['tool_access']=[]
 carrier=next(b for n,b in baseline if 'Internal neck carrier' in n)
 for x in [-25,25]:
  for y in [-50,-36]:
   tool=cyl(x,y,90,118,2.6);hits=[]
   for n,b in [(n,s) for n,s,c in parts[:3]]+[('V8 carrier',carrier)]:
    v=intersects(tool,b)
    if v>.02:hits.append([n,v])
   report['tool_access'].append(dict(path='clamp screw from underside on bench',x=x,y=y,hits=hits))
 (ROOT/'review/validation.json').write_text(json.dumps(report,indent=2))
 print(json.dumps({k:v for k,v in report.items() if k not in ['new_parts','insertion_samples']},indent=2),flush=True)
 print('NEW VALIDITY',[(p['name'],p['valid'],p['solids']) for p in report['new_parts']],flush=True)
 print('INSERTION HITS',[p for p in report['insertion_samples'] if p['hits']],flush=True)
 return report

if __name__=='__main__':
 parts=build();print('BUILT',flush=True);report=validate(parts)
 for i,(n,s,c) in enumerate(parts):BRepTools.Write_s(s,str(ROOT/f'review/new_{i:02}.brep'))
 (ROOT/'review/new_parts.json').write_text(json.dumps([dict(name=n,color=c,brep=f'new_{i:02}.brep') for i,(n,s,c) in enumerate(parts)],indent=2))
 if '--export' in sys.argv:
  assert not report['interference_with_v8'] and not report['new_part_interference'],'Resolve intersections before export'
  assert all(p['valid'] for p in report['new_parts']),'Invalid new shape'
  assert not any(p['hits'] for p in report['insertion_samples']),'Insertion collision'
  assert not report['usb_plug_gauge']['hits'],'USB plug gauge collision'
  assert not any(p['hits'] for p in report['tool_access']),'Driver path collision'
  named_step(parts,ROOT/'CAD/V9_Display_Mount_Additions.step')
  named_step(parts,ROOT/'CAD/Orbo_V9_DisplayMount.step',ROOT/'baseline_v8/CAD/Orbo_V8_RigidFrame.step')
  for i,(n,s,c) in enumerate(parts[:3],1):
   fn=f'{i:02}_'+n.replace(' ','_');step(s,ROOT/f'CAD/{fn}.step')
   BRepMesh_IncrementalMesh(s,.06,False,.2,True);w=StlAPI_Writer();w.ASCIIMode=False;w.Write(s,str(ROOT/f'STL/{fn}.stl'))
  print('EXPORTED',flush=True)

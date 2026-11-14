"""Reopen V9 STEP; compare every retained V8 body by volume, centroid and bounds."""
import json
from build_v9 import ROOT,bbox,load_brep,vol,solids
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.collections import Sequence_TDF_Label
from OCP.TDF import TDF_Label
from OCP.TDataStd import TDataStd_Name
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.BRepGProp import BRepGProp
from OCP.GProp import GProp_GProps
def centroid(s):
 p=GProp_GProps();BRepGProp.VolumeProperties_s(s,p);return p.CentreOfMass().Coord()
out=[];shapes={};st=None
def walk(label):
 a=TDataStd_Name();name=a.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(),a) else ''
 if st.IsReference_s(label):
  ref=TDF_Label();st.GetReferredShape_s(label,ref);label=ref
 if st.IsAssembly_s(label):
  seq=Sequence_TDF_Label();st.GetComponents_s(label,seq)
  for i in range(1,seq.Length()+1):walk(seq.Value(i))
 else:
  s=st.GetShape_s(label);shapes[name]=s
  out.append(dict(name=name,solids=len(solids(s)),valid=BRepCheck_Analyzer(s,False).IsValid()))
def read(path):
 global out,shapes,st
 out=[];shapes={}
 doc=TDocStd_Document(TCollection_ExtendedString('audit'));r=STEPCAFControl_Reader();r.SetNameMode(True)
 r.ReadFile(str(path));r.Transfer(doc);st=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main());labels=Sequence_TDF_Label();st.GetFreeShapes(labels)
 for i in range(1,labels.Length()+1):walk(labels.Value(i))
 return dict(shapes),list(out)
baseline,_=read(ROOT/'baseline_v8/CAD/Orbo_V8_RigidFrame.step')
shapes,out=read(ROOT/'CAD/Orbo_V9_DisplayMount.step')
retained=[]
for name,s in baseline.items():
 b=shapes.get(name)
 if b is None:retained.append(dict(name=name,passed=False,reason='not found'));continue
 dv=abs(vol(s)-vol(b));dc=max(abs(x-y) for x,y in zip(centroid(s),centroid(b)));db=max(abs(x-y) for x,y in zip(bbox(s),bbox(b)))
 retained.append(dict(name=name,passed=dv<.001 and dc<.001 and db<.001,volume_difference_mm3=dv,centroid_difference_mm=dc,bounds_difference_mm=db))
report=dict(parts=out,total_solids=sum(p['solids'] for p in out),retained=retained,all_retained_match=all(p['passed'] for p in retained),all_topology_valid=all(p['valid'] for p in out))
(ROOT/'review/step_validation.json').write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k not in ['parts','retained']},indent=2));print('MISMATCHES',[p for p in retained if not p['passed']])
assert report['all_retained_match'] and report['all_topology_valid']

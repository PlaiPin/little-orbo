import sys,os,json
sys.path.insert(0,r'C:\Users\sahlu\Desktop\conversion files\cad_cleanup_tools')
from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDF import TDF_Label
from OCP.collections import Sequence_TDF_Label
from OCP.TDataStd import TDataStd_Name
from OCP.BRepTools import BRepTools
from OCP.BRepCheck import BRepCheck_Analyzer
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID
ROOT=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
out=os.path.join(ROOT,'review','final_breps');os.makedirs(out,exist_ok=True)
doc=TDocStd_Document(TCollection_ExtendedString('doc'));reader=STEPCAFControl_Reader();reader.SetNameMode(True)
print(reader.ReadFile(os.path.join(ROOT,'CAD','Orbo_V8_RigidFrame.step')),flush=True);reader.Transfer(doc)
st=XCAFDoc_DocumentTool.ShapeTool_s(doc.Main());labels=Sequence_TDF_Label();st.GetFreeShapes(labels)
items=[]
def walk(label,path):
 a=TDataStd_Name();n=a.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(),a) else ''
 if st.IsReference_s(label):
  ref=TDF_Label();st.GetReferredShape_s(label,ref);label=ref
 if st.IsAssembly_s(label):
  seq=Sequence_TDF_Label();st.GetComponents_s(label,seq)
  for i in range(1,seq.Length()+1):walk(seq.Value(i),path+[n])
 else:
  shape=st.GetShape_s(label);exp=TopExp_Explorer(shape,TopAbs_SOLID);count=0
  while exp.More():count+=1;exp.Next()
  record=dict(name=n,path=path,solids=count)
  if True:
   record['topology_valid']=BRepCheck_Analyzer(shape,False).IsValid()
   record['brep']=f'{len(items):02d}.brep';BRepTools.Write_s(shape,os.path.join(out,record['brep']))
   print(record,flush=True)
  items.append(record)
for i in range(1,labels.Length()+1):walk(labels.Value(i),[])
json.dump(items,open(os.path.join(ROOT,'review','step_validation.json'),'w'),indent=2)
print('COMPLETE',len(items),flush=True)



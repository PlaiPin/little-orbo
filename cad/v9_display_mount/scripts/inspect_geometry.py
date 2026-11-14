import sys,json,pathlib,functools
sys.path.insert(0,r'C:\Users\sahlu\Desktop\conversion files\cad_cleanup_tools')
from OCP.STEPControl import STEPControl_Reader
from OCP.BRepTools import BRepTools
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Shape
from OCP.BRepBndLib import BRepBndLib
from OCP.Bnd import Bnd_Box
from OCP.TopExp import TopExp_Explorer
from OCP.TopAbs import TopAbs_SOLID
from OCP.IntCurvesFace import IntCurvesFace_ShapeIntersector
from OCP.gp import gp_Pnt,gp_Dir,gp_Lin
ROOT=pathlib.Path(__file__).resolve().parents[1]
@functools.lru_cache(maxsize=4096)
def bbox(s):
 b=Bnd_Box();BRepBndLib.Add_s(s,b);return [*b.CornerMin().Coord(),*b.CornerMax().Coord()]
def load_brep(f):
 s=TopoDS_Shape();BRepTools.Read_s(s,str(f),BRep_Builder());return s
def ray(s,x,z):
 r=IntCurvesFace_ShapeIntersector();r.Load(s,1e-7);r.Perform(gp_Lin(gp_Pnt(x,-100,z),gp_Dir(0,1,0)),0,150)
 return sorted(round(r.Pnt(i).Y(),4) for i in range(1,r.NbPnt()+1))
if __name__=='__main__':
 records=json.loads((ROOT/'baseline_v8/review/step_validation.json').read_text())
 if '--edge-rays' in sys.argv:
  for a in records:
   if any(k in a['name'] for k in ['display mask','face cover']):
    s=load_brep(ROOT/'baseline_v8/review/final_breps'/a['brep'])
    print(a['name'],{str((x,z)):ray(s,x,z) for x in [-32,35,39,43] for z in [164,177,189,192,195]},flush=True)
  sys.exit()
 for a in records:
  if any(k in a['name'] for k in ['Head -','display mask','neck carrier']):
   s=load_brep(ROOT/'baseline_v8/review/final_breps'/a['brep'])
   print(a['name'],bbox(s),flush=True)
   print({str((x,z)):ray(s,x,z) for x in [0,20,30] for z in [126,130,160,166.8,176.7,186.6,195]},flush=True)
 r=STEPControl_Reader();r.ReadFile(str(next((ROOT/'reference_display').glob('*.stp'))));r.TransferRoots();s=r.OneShape()
 print('DISPLAY',bbox(s),flush=True)
 e=TopExp_Explorer(s,TopAbs_SOLID);i=0;out=[]
 while e.More():
  ss=e.Current();out.append(dict(i=i,box=bbox(ss)));BRepTools.Write_s(ss,str(ROOT/f'review/display_{i:03}.brep'));e.Next();i+=1
 (ROOT/'review/display_solids.json').write_text(json.dumps(out,indent=2));print(out,flush=True)

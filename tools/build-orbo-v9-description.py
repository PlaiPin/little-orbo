#!/usr/bin/env python3
"""Export complete V9 STEP assembly to ROS visuals; requires cadquery-ocp 7.8.1.1.post1.

CAD millimetres: face -Y, up +Z. ROS metres: forward +X, left +Y,
up +Z. Base origin is the wheel axle midpoint (CAD 0,8,33 mm).
All STEP leaves are included once, including hidden/internal CAD parts.
"""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

from OCP.STEPCAFControl import STEPCAFControl_Reader
from OCP.TDocStd import TDocStd_Document
from OCP.TCollection import TCollection_ExtendedString
from OCP.XCAFDoc import XCAFDoc_DocumentTool
from OCP.TDF import TDF_LabelSequence, TDF_Label
from OCP.TDataStd import TDataStd_Name
from OCP.Bnd import Bnd_Box
from OCP.BRepBndLib import BRepBndLib
from OCP.TopLoc import TopLoc_Location
from OCP.gp import gp_Trsf
from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
from OCP.BRep import BRep_Builder
from OCP.TopoDS import TopoDS_Compound
from OCP.BRepMesh import BRepMesh_IncrementalMesh
from OCP.StlAPI import StlAPI_Writer

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'ros_ws/src/orbo_description'
SOURCE = ROOT / 'cad/v9_display_mount/CAD/Orbo_V9_DisplayMount.step'
XACRO = 'http://www.ros.org/wiki/xacro'
ET.register_namespace('xacro', XACRO)


def category(name):
    n = name.lower()
    if 'wheel_66x18' in n:
        return 'wheel'
    if 'torso' in n: return 'torso_shell'
    if 'v8 head' in n: return 'head_shell'
    if 'smooth_removable' in n or 'smooth removable' in n: return 'flipper_covers'
    if 'recessed black display mask' in n: return 'face_mask'
    if 'smoked' in n: return 'face_cover'
    if 'eye graphic' in n: return 'eyes'
    if 'neck diffuser' in n: return 'neck_diffuser'
    if 'servo -' in n: return 'servos'
    if 'waveshare' in n: return 'display_module'
    if n.startswith('v9 '): return 'display_mount'
    if 'xl430' in n: return 'wheel_motors'
    return 'chassis_and_equipment'


def visual(parent, group):
    v = ET.SubElement(parent, 'visual')
    g = ET.SubElement(v, 'geometry')
    ET.SubElement(g, 'mesh', filename=f'package://orbo_description/meshes/v9/{group}.stl', scale='0.001 0.001 0.001')
    colors = {'torso_shell': '.91 .87 .77 1', 'head_shell': '.91 .87 .77 1',
              'flipper_covers': '.95 .56 .37 1', 'eyes': '1 .98 .8 1',
              'neck_diffuser': '.3 .85 .88 1', 'face_cover': '.1 .13 .16 .45',
              'face_mask': '.055 .065 .075 1', 'left_wheel': '.1 .1 .1 1',
              'right_wheel': '.1 .1 .1 1', 'display_module': '.15 .2 .2 1'}
    m = ET.SubElement(v, 'material', name=group+'_material')
    ET.SubElement(m, 'color', rgba=colors.get(group, '.30 .43 .49 1'))


def main():
    reader = STEPCAFControl_Reader(); reader.SetNameMode(True)
    if int(reader.ReadFile(str(SOURCE))) != 1: raise RuntimeError('STEP read failed')
    doc = TDocStd_Document(TCollection_ExtendedString('orbo_v9'))
    if not reader.Transfer(doc): raise RuntimeError('STEP transfer failed')
    shapes = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())
    groups, parts = {}, []
    def visit(label, location):
        name_attr = TDataStd_Name()
        name = name_attr.Get().ToExtString() if label.FindAttribute(TDataStd_Name.GetID_s(), name_attr) else ''
        if shapes.IsReference_s(label):
            target = TDF_Label(); shapes.GetReferredShape_s(label, target)
            visit(target, location.Multiplied(shapes.GetLocation_s(label)))
            return
        children = TDF_LabelSequence()
        if shapes.GetComponents_s(label, children):
            for i in range(1, children.Length()+1): visit(children.Value(i), location)
            return
        shape = shapes.GetShape_s(label).Moved(location)
        bounds = Bnd_Box(); BRepBndLib.AddOptimal_s(shape, bounds)
        box = bounds.Get(); group = category(name)
        cy = 0.0
        if group == 'wheel':
            cx = (box[0]+box[3])/2
            group = 'left_wheel' if cx > 0 else 'right_wheel'
            cy = cx
        # Rotation +90 degrees about Z, then axle-origin translation.
        transform = gp_Trsf(); transform.SetValues(0,-1,0,8, 1,0,0,-cy, 0,0,1,-33)
        shape = BRepBuilderAPI_Transform(shape, transform, True).Shape()
        groups.setdefault(group, []).append(shape)
        parts.append({'name': name, 'group': group, 'cad_bounds_mm': list(box)})
    roots = TDF_LabelSequence(); shapes.GetFreeShapes(roots)
    for i in range(1, roots.Length()+1): visit(roots.Value(i), TopLoc_Location())
    if len(parts) != 176 or any(len(groups.get(s+'_wheel', [])) != 1 for s in ('left', 'right')):
        raise RuntimeError('V9 assembly changed: review body count and wheel selection')
    destination = ROOT/'cad/meshes/v9'; destination.mkdir(parents=True, exist_ok=True)
    records=[]
    for name, members in sorted(groups.items()):
        builder=BRep_Builder(); compound=TopoDS_Compound(); builder.MakeCompound(compound)
        for shape in members: builder.Add(compound, shape)
        BRepMesh_IncrementalMesh(compound, .15, False, .3, True)
        path=destination/(name+'.stl'); writer=StlAPI_Writer(); writer.ASCIIMode=False
        if not writer.Write(compound, str(path)): raise RuntimeError('STL export failed')
        records.append({'group':name,'parts':len(members),'sha256':hashlib.sha256(path.read_bytes()).hexdigest()})
    (destination/'manifest.json').write_text(json.dumps({'source':str(SOURCE.relative_to(ROOT)),
        'source_sha256':hashlib.sha256(SOURCE.read_bytes()).hexdigest(), 'units':'millimetres',
        'transform':'ROS=(8-CAD_Y, CAD_X, CAD_Z-33); wheel meshes additionally centered on their axle',
        'groups':records,'parts':parts},indent=2)+'\n')
    robot=ET.Element('robot'); macro=ET.SubElement(robot, '{'+XACRO+'}macro', name='orbo_cad')
    for group in sorted(groups):
        if group.endswith('_wheel'):continue
        name='cad_'+group; link=ET.SubElement(macro,'link',name=name); visual(link,group)
        j=ET.SubElement(macro,'joint',name=name+'_mount',type='fixed')
        ET.SubElement(j,'parent',link='base_link'); ET.SubElement(j,'child',link=name)
    for side in ('left','right'):
        m=ET.SubElement(robot,'{'+XACRO+'}macro',name='cad_'+side+'_wheel')
        visual(m,side+'_wheel')
    ET.indent(robot); ET.ElementTree(robot).write(OUT/'config/cad-v9.xacro',encoding='utf-8',xml_declaration=True)
    for view in ('full','internal'):
        preview=ET.Element('robot',name='orbo_v9_'+view)
        ET.SubElement(preview,'link',name='base_link')
        shells={'torso_shell','head_shell','face_mask','face_cover','eyes','neck_diffuser'}
        for group in sorted(groups):
            if view=='internal' and group in shells:continue
            name='cad_'+group; link=ET.SubElement(preview,'link',name=name);visual(link,group)
            j=ET.SubElement(preview,'joint',name=name+'_mount',type='fixed')
            ET.SubElement(j,'parent',link='base_link');ET.SubElement(j,'child',link=name)
            if group.endswith('_wheel'):
                ET.SubElement(j,'origin',xyz='0 '+('0.0509' if group=='left_wheel' else '-0.0509')+' 0')
        ET.indent(preview)
        ET.ElementTree(preview).write(OUT/'config'/('orbo-preview.urdf' if view=='full' else 'orbo-preview-internal.urdf'),encoding='utf-8',xml_declaration=True)
    print(f'Exported {len(parts)} assembly leaves into {len(groups)} groups, with full/internal previews.')

if __name__=='__main__': main()

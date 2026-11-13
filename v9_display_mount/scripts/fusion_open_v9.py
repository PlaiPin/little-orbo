"""Run in Fusion Python console; opens copied V8 archive in a new document."""
import adsk.core,adsk.fusion,json,os,traceback
ROOT=r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v9_display_mount'
try:
 app=adsk.core.Application.get();im=app.importManager
 doc=im.importToNewDocument(im.createFusionArchiveImportOptions(os.path.join(ROOT,'baseline_v8','CAD','Orbo_V8_RigidFrame.f3d')))
 doc.name='Orbo V9 - shared AMOLED display mount'
 des=adsk.fusion.Design.cast(doc.products.itemByProductType('DesignProductType'))
 def signature():
  return [dict(name=o.component.name,bodies=[dict(name=b.name,volume=b.volume,area=b.area,bounds=[list(p.asArray()) for p in [b.boundingBox.minPoint,b.boundingBox.maxPoint]],faces=b.faces.count) for b in o.bRepBodies]) for o in des.rootComponent.occurrences]
 json.dump(signature(),open(os.path.join(ROOT,'review','fusion_baseline_signature.json'),'w'),indent=2)
 open(os.path.join(ROOT,'review','fusion_status.txt'),'w').write('V8 ARCHIVE COPIED INTO NEW V9 DOCUMENT\n')
except:open(os.path.join(ROOT,'review','fusion_status.txt'),'w').write(traceback.format_exc())

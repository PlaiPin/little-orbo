exec(open(r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame\scripts\build_v8.py').read().split('try:\n open(')[0])
try:
 open(os.path.join(ROOT,'review','bridge_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;tm=adsk.fusion.TemporaryBRepManager.get();assert app.activeDocument.name.startswith('Orbo V8')
 o=next(o for o in root.occurrences if o.name.startswith('V8 Electronics'));c=o.component;b=c.bRepBodies.item(0);feature=c.features.baseFeatures.add();feature.name='Side webs around concealed driver passages';assert feature.startEdit()
 for sx in (-1,1):
  xa,xb=sorted((sx*38.4,sx*40.8));s=box(xa,-40,52,xb,-14,78);head_paths(s);patch=c.bRepBodies.add(s,feature);patch.name='Side load bypass '+str(sx)
 assert feature.finishEdit()
 collection=adsk.core.ObjectCollection.create()
 for toolbody in c.bRepBodies:
  if toolbody.name.startswith('Side load bypass'):collection.add(toolbody)
 assert collection.count==2
 ci=c.features.combineFeatures.createInput(b,collection);ci.operation=adsk.fusion.FeatureOperations.JoinFeatureOperation;ci.isKeepToolBodies=False
 joined=c.features.combineFeatures.add(ci);joined.name='Join continuous webs to braced cage'
 des.timeline.moveToEnd()
 exec(compile(open(os.path.join(ROOT,'scripts','audit_v8.py'),encoding='utf-8').read(),'audit_v8.py','exec'))
 exec(compile(open(os.path.join(ROOT,'scripts','validate_assembly.py'),encoding='utf-8').read(),'validate_assembly.py','exec'))
 exec(compile(open(os.path.join(ROOT,'scripts','check_fasteners.py'),encoding='utf-8').read(),'check_fasteners.py','exec'))
 exec(compile(open(os.path.join(ROOT,'scripts','save_exports.py'),encoding='utf-8').read(),'save_exports.py','exec'))
 open(os.path.join(ROOT,'review','bridge_status.txt'),'a').write('COMPLETE\n')
except:open(os.path.join(ROOT,'review','bridge_status.txt'),'a').write(traceback.format_exc())

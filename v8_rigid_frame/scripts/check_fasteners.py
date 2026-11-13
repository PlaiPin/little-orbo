exec(open(r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame\scripts\audit_v8.py').read().split('try:\n open(')[0])
try:
 open(os.path.join(ROOT,'review','fastener_status.txt'),'w').write('START\n')
 app=adsk.core.Application.get();des=adsk.fusion.Design.cast(app.activeProduct);root=des.rootComponent;tm=adsk.fusion.TemporaryBRepManager.get();assert app.activeDocument.name.startswith('Orbo V8')
 parts=[(o.component.name,b) for o in root.occurrences for b in o.bRepBodies];struct=[(n,b) for n,b in parts if not n.startswith('Hardware')]
 hardware=[(n,b) for n,b in parts if n.startswith(('Hardware frame','Hardware carrier','Hardware front frame','Hardware M3 insert'))]
 report={'hardware_checked':[n for n,b in hardware],'hits':[]}
 for n,b in hardware:
  for on,ob in struct:
   v=volume(b,ob)
   if v>.02:report['hits'].append([n,on,v])
 json.dump(report,open(os.path.join(ROOT,'review','fastener_checks.json'),'w'),indent=2)
 open(os.path.join(ROOT,'review','fastener_status.txt'),'a').write('COMPLETE\n')
except:open(os.path.join(ROOT,'review','fastener_status.txt'),'a').write(traceback.format_exc())

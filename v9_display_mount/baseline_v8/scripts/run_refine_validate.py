import os,json
ROOT=r'C:\Users\sahlu\Documents\Plaipin\Fall 2026\Rpi shell drive\v8_rigid_frame'
exec(compile(open(os.path.join(ROOT,'scripts','refine_frame.py'),encoding='utf-8').read(),'refine_frame.py','exec'))
assert not json.load(open(os.path.join(ROOT,'review','audit.json')))['static']
exec(compile(open(os.path.join(ROOT,'scripts','validate_assembly.py'),encoding='utf-8').read(),'validate_assembly.py','exec'))

"""Static/self-contained checks that do not load the LLM or contact web APIs."""
from pathlib import Path
import ast, json, subprocess, sys
root=Path(__file__).resolve().parent
for f in [root/'server.py',root/'stage1.py',root/'structures.py',root/'arfa_agents.py',*sorted((root/'flood_hazard').glob('*.py'))]:
    ast.parse(f.read_text(), filename=str(f))
for name in ['pyramid_config.json','states_index.json','cells_index.json']:
    json.loads((root/'USA_Structures_Index'/name).read_text())
js=root/'static'/'app.js'
try:
    subprocess.run(['node','--check',str(js)],check=True,capture_output=True,text=True)
except FileNotFoundError:
    print('node not installed: skipped JS parser check')
print('ARFA Hybrid static self-check: PASS')

# -*- coding: utf-8 -*-
"""诊断 locator 对 school_quest_chain case0 完整输入的候选产出。"""
import os, sys, json, shutil, tempfile
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")

_ROOT = Path(__file__).resolve().parents[2]
_env_file = _ROOT / ".env"
if _env_file.exists():
    with open(_env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "server"))

cases = json.loads((HERE / "cases" / "school_quest_chain_inputs.json").read_text(encoding="utf-8"))
text = cases[0]["input"]

res_dir = Path(os.environ.get("RES") or (_ROOT / "resources"))
sandbox = Path(tempfile.mkdtemp(prefix="diag_loc_"))
sb_res = sandbox / "resources"
shutil.copytree(res_dir, sb_res)
os.environ["RES"] = str(sb_res)

import logging
logging.basicConfig(level=logging.DEBUG)

from services.agent_service import AgentService
service = AgentService(resources_dir=sb_res, enable_skill=True)
agent = service.agent
loc = agent._locator_agent

res = loc.locate(text)
print(f"候选数: {len(res.candidates)}")
for c in res.candidates:
    print(f"  {c.stem:<22} conf={c.confidence:<6} level={c.level:<16} matched={c.matched_term!r}")
print(f"FK 边: {len(res.fk_edges)}")
for e in res.fk_edges:
    print(f"  {e.from_stem}.{e.from_column} -> {e.to_stem}.{e.to_column}")
import re as _re
print("--- debug ---")
print("complex_input:", loc._is_complex_input(text))
print("刷 in text:", bool(_re.search(r'刷|刷新', text)), "坐标/放在/space_id:", bool(_re.search(r'坐标|放在|space_id', text)))
print("新建一位/点击后展开/点击后弹出:", bool(_re.search(r'新建一位|点击后展开|点击后弹出', text)))
print("任务 in text:", '任务' in text, "prefab in text:", 'prefab' in text)

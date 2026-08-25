# -*- coding: utf-8 -*-
import sys, os
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
for _line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line: continue
    _k,_v = _line.split("=",1)
    if _k.strip() and _k.strip() not in os.environ: os.environ[_k.strip()] = _v.strip()
os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2","1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.excel.subagent.locator_agent import LocatorAgent
CASES = [
    ("简单加灵兽", "加一只新灵兽叫逐日麒麟，灵兽id 3001，model_id 1020，技能列表 8001/8002"),
    ("加奖励包", "建奖励包 20070 叫寻器奖励，每日限领 1 次，必给道具 10001 共 15 个"),
    ("加战斗BOSS", "在战场 10030 坐标(300,0,200)刷九幽守护者，model_id 1150，技能 9201/9202"),
]
ag = LocatorAgent()
for name, t in CASES:
    print(f"\n=== {name}: {t[:30]}...")
    r = ag.locate(t)
    for c in r.candidates[:4]:
        print(f"  {c.stem:18s} conf={c.confidence:.2f} level={c.level}")

# -*- coding: utf-8 -*-
import sys, os, json
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
from agent.excel.subagent.decompose_agent import DecomposeAgent
TEXT = (
    "新增一个限时世界BOSS活动叫'焚天赤龙降临'，activity_id 3001，"
    "活动描述'赤龙现世，各路豪杰齐心讨伐，首杀可得传说法宝'，"
    "开始时间 2024-12-21 00:00:00，结束时间 2024-12-28 23:59:59。"
    "配套首杀奖励包也建一下，reward_id 30010，叫'焚天赤龙首杀奖励'，"
    "每日限领 1 次，必给道具 10001 共 20 个，概率 100% 给经验、100% 给金币、"
    "金币公式填 800；再加一个保底掉落池 pool_id 3001，属性修改含 "
    "PhyAtkCon 范围 100-200 权重 20、MagAtkCon 范围 100-200 权重 20、"
    "CritCon 范围 5-15 权重 10，能力类型都填 2。"
)
ag = LocatorAgent()
r = ag.locate(TEXT)
print(f"候选数: {len(r.candidates)}")
print("candidates:", [(c.stem, round(c.confidence,2)) for c in r.candidates])
da = DecomposeAgent()
intents = da.decompose_segment(TEXT, r)
print(f"\nDecomposeAgent 产出 {len(intents)} 条 intent:")
for i, it in enumerate(intents):
    print(f"  [{i}] {it.action} {it.table_hint}/{it.sheet_hint}")
    print(f"      fields={json.dumps(it.fields, ensure_ascii=False)[:200]}")
    print(f"      produces={getattr(it,'produces','')} consumes={getattr(it,'consumes','')}")

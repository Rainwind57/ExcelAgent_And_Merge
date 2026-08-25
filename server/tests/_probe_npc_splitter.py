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
from agent.excel.core.cross_table_splitter import detect_cross_table_action, CrossTableIntentSplitter
NPC_SEG = (
    "再配一个引导 NPC 叫'赤龙指引人'，model_id 1020，放在战场 10050 坐标 (190,0,140)，"
    "玩家点击他弹出对话'焚天赤龙正在肆虐人间，请勇士速速前往讨伐，首杀者将获得传说奖励'，"
    "给两个选项：第一个'我这就去讨伐'选完后跳到第二段奖励介绍对话后结束对话；"
    "第二个'了解一下奖励'跳到新对话，内容为'首杀奖励包含 20 个精炼石、"
    "海量经验与金币，以及随机词条池加成'，再给一个选项'知道了'选完什么都不做结束对话。"
)
print(f"detect_cross_table_action: {detect_cross_table_action(NPC_SEG)}")
sp = CrossTableIntentSplitter()
splits = sp.split(NPC_SEG)
print(f"splitter 产出 {len(splits)} intent:")
for i, s in enumerate(splits):
    print(f"  [{i}] {s.action} {s.table_hint}/{s.sheet_hint} fields={list(s.fields.keys())} produces={s.produces}")

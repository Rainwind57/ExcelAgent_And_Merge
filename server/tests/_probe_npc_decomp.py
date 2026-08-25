# -*- coding: utf-8 -*-
"""诊断 NPC 对话树段 LLM decompose 为何产空。"""
import sys, os, json
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
for _line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line: continue
    _k,_v = _line.split("=",1)
    if _k.strip() and _k.strip() not in os.environ: os.environ[_k.strip()] = _v.strip()
os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2","1")
os.environ["CODEMAKER_DECOMPOSE_TIMEOUT"]="40"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.excel.subagent.locator_agent import LocatorAgent
from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.parser.codemaker_parser import CodemakerNLParser

NPC_SEG = (
    "再配一个引导 NPC 叫'赤龙指引人'，model_id 1020，放在战场 10050 坐标 (190,0,140)，"
    "玩家点击他弹出对话'焚天赤龙正在肆虐人间，请勇士速速前往讨伐，首杀者将获得传说奖励'，"
    "给两个选项：第一个'我这就去讨伐'选完后跳到第二段奖励介绍对话后结束对话；"
    "第二个'了解一下奖励'跳到新对话，内容为'首杀奖励包含 20 个精炼石、"
    "海量经验与金币，以及随机词条池加成'，再给一个选项'知道了'选完什么都不做结束对话。"
)

# capture thinking
_thoughts = []
def _sink(phase, detail):
    _thoughts.append((phase, detail))

cli = None
try:
    from agent.excel.cli.real_cli import RealCodeMakerCLI
    cli = RealCodeMakerCLI(workspace=_ROOT / "resources")
except Exception as e:
    print(f"cli 构造失败: {e}")

# CodemakerNLParser 用 client（LLM 通道），非 cli
parser = None
try:
    from agent.codemaker_client import CodemakerClient
    _client = CodemakerClient()
    parser = __import__("agent.excel.parser.codemaker_parser", fromlist=["CodemakerNLParser"]).CodemakerNLParser(client=_client)
except Exception as e:
    print(f"parser 构造失败: {e}")

loc = LocatorAgent(parser=parser, thinking_sink=_sink, cli=cli)
dec = DecomposeAgent(parser=parser, thinking_sink=_sink, cli=cli)

print(f"NPC 段长度: {len(NPC_SEG)}")
print(f"\n=== Locator ===")
lr = loc.locate(NPC_SEG)
for c in lr.candidates:
    print(f"  {c.stem:22s} conf={c.confidence:.2f} level={c.level:14s} sheet={c.sheet}")
print(f"FK edges: {len(lr.fk_edges)}")

print(f"\n=== Decompose (timeout=40s) ===")
intents = dec.decompose_segment(NPC_SEG, lr)
print(f"产出 intent 数: {len(intents)}")
for i, it in enumerate(intents):
    print(f"  [{i}] {it.action} {it.table_hint}/{it.sheet_hint} fields={list(it.fields.keys())[:6]} produces={getattr(it,'produces','')}")

print(f"\n=== Thinking ({len(_thoughts)} 条) ===")
for ph, d in _thoughts[-15:]:
    print(f"  [{ph}] {d[:150]}")

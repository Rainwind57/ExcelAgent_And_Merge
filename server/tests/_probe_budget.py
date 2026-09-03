# -*- coding: utf-8 -*-
import sys, os, time, json
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
for _l in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _l = _l.strip()
    if not _l or _l.startswith("#") or "=" not in _l: continue
    _k, _v = _l.split("=", 1)
    if _k.strip() and _k.strip() not in os.environ: os.environ[_k.strip()] = _v.strip()
os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2", "1")
os.environ["CODEMAKER_STEP1_DEADLINE_S"] = "0"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")

from agent.excel.subagent.locator_agent import LocatorAgent
from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.parser.codemaker_parser import CodemakerNLParser
from agent.excel.parser.multi_intent_splitter import split_multi_intent

T = ("新增节日活动中秋签到，类型节日，开始 2025-09-25 00:00:00、"
     "结束 2025-10-01 23:59:59；配奖励包 100900中秋礼每日限领 1 次、必给 10001×10；"
     "新建邮件模板 30101中秋祝福内容中秋快乐，发全服邮件 global_id 8 用该模板、附奖励包 100900。")

_th = []
def _s(p, d): _th.append((p, d))

from agent.excel.cli.real_cli import RealCodeMakerCLI
cli = RealCodeMakerCLI(workspace=_ROOT / "resources")
from agent.codemaker_client import CodemakerClient
parser = CodemakerNLParser(client=CodemakerClient())
ag = LocatorAgent(parser=parser, thinking_sink=_s, cli=cli)
da = DecomposeAgent(parser=parser, thinking_sink=_s, cli=cli)

segs = split_multi_intent(T)
print(f"N={len(segs)}")
t0 = time.time(); tot = 0
for i, seg in enumerate(segs):
    ts = time.time()
    r = ag.locate(seg.text)
    its = da.decompose_segment(seg.text, r)
    te = time.time(); tot += len(its)
    print(f"seg{i} cand={len(r.candidates)} its={len(its)} {te-ts:.1f}s")
    for j, it in enumerate(its):
        print(f"  [{j}] {it.action} {it.table_hint}/{it.sheet_hint} f={list(it.fields.keys())[:5]}")
print(f"total {tot} its wall {time.time()-t0:.1f}s")
_msgs = [d[:90] for p, d in _th if "预算" in d or "deadline" in d or "超时" in d]
print(f"budget/deadline msgs ({len(_msgs)}):")
for m in _msgs: print(f"  {m}")

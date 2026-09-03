# -*- coding: utf-8 -*-
import sys, os, time
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

SEG = ("新建邮件模板 30101中秋祝福内容中秋快乐，"
       "发全服邮件 global_id 8 用该模板、附奖励包 100900。")

_th = []
def _s(p, d): _th.append((p, d))

from agent.excel.cli.real_cli import RealCodeMakerCLI
cli = RealCodeMakerCLI(workspace=_ROOT / "resources")
from agent.codemaker_client import CodemakerClient
parser = CodemakerNLParser(client=CodemakerClient())
ag = LocatorAgent(parser=parser, thinking_sink=_s, cli=cli)
da = DecomposeAgent(parser=parser, thinking_sink=_s, cli=cli)

t0 = time.time()
r = ag.locate(SEG)
print(f"候选 {len(r.candidates)}: {[(c.stem, round(c.confidence,2)) for c in r.candidates]}")
its = da.decompose_segment(SEG, r)
print(f"产出 {len(its)} 条 ({time.time()-t0:.1f}s)")
for j, it in enumerate(its):
    print(f"  [{j}] {it.action} {it.table_hint}/{it.sheet_hint}")
    print(f"      produces={getattr(it,'produces','')} consumes={getattr(it,'consumes','')}")
print(f"\n=== Thinking ({len(_th)} 条) ===")
for p, d in _th:
    print(f"  [{p}] {d[:160]}")

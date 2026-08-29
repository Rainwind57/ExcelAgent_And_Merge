# -*- coding: utf-8 -*-
"""4-Step 全链路测试：TableAgent.run_v2 走真实 codemaker serve（8666）。

对每个用例：
  - 复制 resources 到沙箱（不污染真实资源）
  - 跑 run_v2（Step1 解析 → Step2 校验 → Step3 执行 → Step4 汇总）
  - 报告每步 ok / errors / failures / subtasks / 写盘 diff

用法（repo 根）：
    .venv\Scripts\python.exe server\tests\run_4step_fullchain.py --only 0
    .venv\Scripts\python.exe server\tests\run_4step_fullchain.py --only 0,1,2

前提：codemaker serve 已启动（.env 配置好 CODEMAKER_SERVER_URL 等）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER_DIR = ROOT / "server"
RES = ROOT / "resources"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _jsonable(v, depth=0):
    if v is None or isinstance(v, (str, int, float, bool)):
        return _CTRL_RE.sub("", v) if isinstance(v, str) else v
    if depth > 5:
        return str(v)[:200]
    if isinstance(v, dict):
        return {str(k): _jsonable(val, depth + 1) for k, val in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in v]
    if hasattr(v, "to_checkpoint_dict"):
        try:
            return _jsonable(v.to_checkpoint_dict(), depth + 1)
        except Exception:
            pass
    if hasattr(v, "__dict__"):
        try:
            return _jsonable(v.__dict__, depth + 1)
        except Exception:
            pass
    return _CTRL_RE.sub("", str(v)[:200])


CASES = [
    {
        "name": "月华庆典全服邮件（单意图两表联动）",
        "text": "月华庆典要开了，帮我发一封全服邮件。邮件模板：标题'月华庆典开启'，内容'月华照耀九州，庆典开启，登录即可领取好礼，祝少侠月下得宝'。全服邮件 global_id 21，邮件类型 1，发送人'系统'，发送时间 2026-10-01 00:00:00，附带奖励 10001。",
        "expect_intents": 2,
    },
    {
        "name": "限时活动·九霄论剑（activity 单表 add）",
        "text": "开一个限时活动叫'九霄论剑'，活动编号 3060，活动描述'九霄之上，群雄论剑，活动期间内每日可参与一次剑试'，开始时间 2026-11-01 00:00:00，结束时间 2026-11-15 23:59:59。",
        "expect_intents": 1,
    },
    {
        "name": "门派神通微调（modify + row_key 定位）",
        "text": "剑修的战斗模型换成 1075，然后把'驭风'这条神通的描述改成'每次施法积累风势，风势满溢后进入狂风状态，获得额外行动机会，首回合必得一层风势'。",
        "expect_intents": 2,
    },
]


def run_case(case: dict, sandbox: Path) -> dict:
    from agent.excel.cli.real_cli import RealCodeMakerCLI
    from agent.excel.parser.codemaker_parser import CodemakerNLParser
    from agent.excel.core.agent import TableAgent
    from agent.excel.core.pipeline import STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE

    cli = RealCodeMakerCLI(workspace=sandbox)
    parser = CodemakerNLParser(directory=str(sandbox), enable_skill=True)
    agent = TableAgent(
        cli=cli, parser=parser, enable_skill=True,
        enable_verify_repair_loop=False, enable_skill_tools_recovery=False,
    )
    t0 = time.time()
    res = agent.run_v2(case["text"], session_id=f"fc_{int(t0)}")
    dur = time.time() - t0

    out = {
        "name": case["name"],
        "dur_s": round(dur, 1),
        "ok": bool(getattr(res, "ok", False)),
        "message": str(getattr(res, "message", "") or ""),
        "intent": _jsonable(getattr(res, "intent", None)),
        "sub_tasks": _jsonable(getattr(res, "sub_tasks", []) or []),
        "failures": _jsonable(getattr(res, "failures", []) or []),
        "result_rows": _jsonable(getattr(res, "result_rows", []) or []),
        "llm_calls": _jsonable(getattr(agent, "_llm_counter", None).peek_total()
                                if getattr(agent, "_llm_counter", None) else 0),
    }
    # per-step summary
    for sid, label in ((STEP1_PARSE, "step1"), (STEP2_VALIDATE, "step2"),
                       (STEP3_EXECUTE, "step3"), (STEP4_CONCLUDE, "step4")):
        out[label] = {"present": False}
    if hasattr(res, "thinking_steps"):
        pass
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="逗号分隔 case 下标，空=全部")
    ap.add_argument("--report", default=str(
        SERVER_DIR / "tests" / "reports" / "fourstep_fullchain_latest.json"))
    args = ap.parse_args()

    idxs = ([int(x) for x in args.only.split(",") if x.strip()]
            if args.only else list(range(len(CASES))))
    cases = [CASES[i] for i in idxs if 0 <= i < len(CASES)]

    results = []
    for ci, case in enumerate(cases):
        tmp = Path(tempfile.mkdtemp(prefix="fc4step_"))
        sandbox = tmp / "resources"
        try:
            shutil.copytree(RES, sandbox)
            print(f"\n[{ci}] {case['name']}", flush=True)
            r = run_case(case, sandbox)
            r["case_index"] = ci
            results.append(r)
            print(f"  ok={r['ok']}  dur={r['dur_s']}s  llm={r['llm_calls']}")
            print(f"  message: {r['message'][:200]}")
            st = r.get("sub_tasks") or []
            ok_n = sum(1 for s in st if s.get("ok") is True) if isinstance(st, list) else 0
            print(f"  subtasks: ok={ok_n}/{len(st)}")
            for f in (r.get("failures") or [])[:8]:
                if isinstance(f, dict):
                    print(f"    FAIL {f.get('type')} {f.get('table')}/{f.get('sheet')} "
                          f"col={f.get('col')} :: {(f.get('root_cause') or '')[:120]}")
        except Exception as e:
            import traceback
            traceback.print_exc()
            results.append({"case_index": ci, "name": case["name"],
                            "crash": f"{type(e).__name__}: {e}"})
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    out_path = Path(args.report)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"results": results}, ensure_ascii=False,
                                   indent=2), encoding="utf-8")
    print(f"\n[report] {out_path}")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""严格审计：直接构造 pipeline 跑 4-step，逐条打印 Step1 JSON + 每步 errors。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
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


CASES = [
    {
        "name": "月华庆典全服邮件",
        "text": "月华庆典要开了，帮我发一封全服邮件。邮件模板：标题'月华庆典开启'，内容'月华照耀九州，庆典开启，登录即可领取好礼，祝少侠月下得宝'。全服邮件 global_id 21，邮件类型 1，发送人'系统'，发送时间 2026-10-01 00:00:00，附带奖励 10001。",
    },
    {
        "name": "限时活动·九霄论剑",
        "text": "开一个限时活动叫'九霄论剑'，活动编号 3060，活动类型 1，活动描述'九霄之上，群雄论剑，活动期间内每日可参与一次剑试'，开始时间 2026-11-01 00:00:00，结束时间 2026-11-15 23:59:59。",
    },
]


def audit(case: dict, sandbox: Path) -> dict:
    from agent.excel.cli.real_cli import RealCodeMakerCLI
    from agent.excel.parser.codemaker_parser import CodemakerNLParser
    from agent.excel.core.agent import TableAgent
    from agent.excel.core.pipeline import (
        ExcelAgentPipeline, ExcelAgentServices,
        Step1ParseSubAgent, Step2ValidateSubAgent,
        Step3ExecuteSubAgent, Step4ConcludeSubAgent,
        StepContext, STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE,
    )

    cli = RealCodeMakerCLI(workspace=sandbox)
    parser = CodemakerNLParser(directory=str(sandbox), enable_skill=True)
    agent = TableAgent(cli=cli, parser=parser, enable_skill=True,
                       enable_verify_repair_loop=False,
                       enable_skill_tools_recovery=False)

    ctx = StepContext(session_id=f"audit_{int(__import__('time').time())}",
                      user_text=case["text"], legacy_agent=agent,
                      thinking_sink=None)
    services = ExcelAgentServices(legacy_agent=agent)
    step1 = Step1ParseSubAgent(parser=agent.parser, thinking_sink=None,
                               cli=agent.cli, locator_agent=agent._locator_agent,
                               decompose_agent=agent._decompose_agent)
    pipeline = ExcelAgentPipeline(
        step1=step1, step2=Step2ValidateSubAgent(services=services),
        step3=Step3ExecuteSubAgent(services=services),
        step4=Step4ConcludeSubAgent(services=services))
    list(pipeline.run(ctx))

    out = {"name": case["name"]}
    s1 = ctx.get_result(STEP1_PARSE)
    s2 = ctx.get_result(STEP2_VALIDATE)
    s3 = ctx.get_result(STEP3_EXECUTE)
    s4 = ctx.get_result(STEP4_CONCLUDE)

    out["step1_ok"] = s1.ok if s1 else None
    intents = (s1.artifacts.get("intents") if s1 else []) or []
    out["step1_intents"] = []
    for i, it in enumerate(intents):
        fields = (getattr(it, "extras", None) or {}).get("fields") or {}
        out["step1_intents"].append({
            "idx": i, "action": getattr(it, "action", ""),
            "table": getattr(it, "table_hint", ""),
            "sheet": getattr(it, "sheet_hint", ""),
            "loc_field": getattr(it, "locator_field", None),
            "loc_value": getattr(it, "locator_value", None),
            "produces": getattr(it, "produces_label", None),
            "consumes": list(getattr(it, "consumes_labels", []) or []),
            "fields": fields,
        })
    out["step1_errors"] = [e.to_event() for e in (s1.errors if s1 else [])]

    out["step2_ok"] = s2.ok if s2 else None
    out["step2_errors"] = [e.to_event() for e in (s2.errors if s2 else [])]

    out["step3_ok"] = s3.ok if s3 else None
    out["step3_subtasks"] = [
        {"action": t.get("intent_action"), "table": t.get("table_stem"),
         "sheet": t.get("table_sheet"), "ok": t.get("ok"),
         "needs_confirm": t.get("needs_confirm"),
         "result_rows": t.get("result_rows")}
        for t in ((s3.artifacts.get("subtasks") if s3 else []) or [])
    ]
    out["step3_failures"] = list((s3.artifacts.get("failures") if s3 else []) or [])
    out["step3_errors"] = [e.to_event() for e in (s3.errors if s3 else [])]

    out["step4_ok"] = s4.ok if s4 else None
    out["step4_summary"] = (s4.artifacts.get("summary") if s4 else "") or ""
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    idxs = ([int(x) for x in args.only.split(",") if x.strip()]
            if args.only else list(range(len(CASES))))
    for ci in idxs:
        case = CASES[ci]
        tmp = Path(tempfile.mkdtemp(prefix="audit4step_"))
        try:
            shutil.copytree(RES, tmp / "resources")
            print("=" * 100)
            print(f"### CASE {ci}: {case['name']}")
            print("input:", case["text"][:120] + "...")
            r = audit(case, tmp / "resources")

            print("\n--- Step1 产出的 intent JSON ---")
            for it in r["step1_intents"]:
                print(f"  [{it['idx']}] {it['action']} {it['table']}/{it['sheet']} "
                      f"loc={it['loc_field']}={it['loc_value']} "
                      f"produces={it['produces']} consumes={it['consumes']}")
                for k, v in (it["fields"] or {}).items():
                    print(f"        {k!r} = {v!r}")
            print("  step1_errors:", json.dumps(r["step1_errors"], ensure_ascii=False))
            print("  step1_ok:", r["step1_ok"])

            print("\n--- Step2 校验 errors ---")
            print("  step2_ok:", r["step2_ok"])
            for e in r["step2_errors"]:
                print(f"    [{e.get('error_type')}] hard={e.get('is_hard')} "
                      f"{e.get('table')}/{e.get('sheet')} col={e.get('column')}")
                print(f"        {e.get('message')}")

            print("\n--- Step3 执行 ---")
            print("  step3_ok:", r["step3_ok"])
            for t in r["step3_subtasks"]:
                print(f"    {t['action']} {t['table']}/{t['sheet']} "
                      f"ok={t['ok']} needs_confirm={t['needs_confirm']}")
                for row in (t.get("result_rows") or [])[:10]:
                    print(f"        col[{row.get('col_name')}] "
                          f"{row.get('old_value')} -> {row.get('new_value')}")
            for f in r["step3_failures"]:
                print(f"    FAIL {f.get('type')} {f.get('table')}/{f.get('sheet')} "
                      f"col={f.get('col')} :: {(f.get('root_cause') or '')[:100]}")

            print("\n--- Step4 ---")
            print("  step4_ok:", r["step4_ok"])
            print("  summary:", (r["step4_summary"] or "")[:300])
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()

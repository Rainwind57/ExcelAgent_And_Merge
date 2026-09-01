# -*- coding: utf-8 -*-
"""LocatorAgent 分通道贡献度压测（真实调用，非全 e2e）。

口径：直接实例化 LocatorAgent(parser=None)（不经过 AgentService/LLM，跳过歧义
兜底的 _llm_resolve 分支，纯规则+FK+RAG 通道），对 S-A 集前 8 条真实用例调用
.locate(text)，用 expected_answer 里的 table 字段做 ground truth，统计候选表
stem 是否覆盖期望表。

四组对照：
  FULL      = 默认（L1+L2+L3+L4 全开）
  NO_L4     = CODEMAKER_RAG_MODE=off（关 L4 BM25）
  NO_L3     = CODEMAKER_LOCATOR_FK_OFF=1（关 L3 FK 扩展，本次新加 3 行 env gate）
  NO_L3_L4  = 两者都关

L1/L2 内嵌在 TableLocator 5 级递进里未拆分（规则命中与模糊兜底共用一条主路径），
本次不单独隔离，如实说明，不臆造数字。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SERVER_DIR = ROOT / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

os.chdir(SERVER_DIR)  # 相对路径 resources/ 等以 server/ 为基准

CASES_FILE = SERVER_DIR / "tests" / "cases" / "table_operation_test_cases.json"


def _stem(table_path: str) -> str:
    s = table_path.replace("\\", "/").rstrip("/")
    if s.lower().endswith(".xlsx"):
        s = s[:-5]
    return s.rsplit("/", 1)[-1]


def load_cases(n=8):
    data = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    out = []
    for c in data[:n]:
        expected_stems = sorted({_stem(a["table"]) for a in c["expected_answer"]})
        out.append({"input": c["input"], "expected": expected_stems})
    return out


def run_config(cases, env_overrides: dict):
    old = {}
    for k, v in env_overrides.items():
        old[k] = os.environ.get(k)
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        # 每组对照重新 import，规避模块级缓存（TableLocator/RelationGraph 单例）
        for mod in list(sys.modules):
            if mod.startswith("agent.excel") or mod == "agent":
                del sys.modules[mod]
        from agent.excel.subagent.locator_agent import LocatorAgent

        agent = LocatorAgent(parser=None)
        rows = []
        for c in cases:
            result = agent.locate(c["input"])
            got_stems = {cand.stem for cand in result.candidates}
            hit = [s for s in c["expected"] if s in got_stems]
            miss = [s for s in c["expected"] if s not in got_stems]
            rows.append({
                "input": c["input"][:24],
                "expected": c["expected"],
                "got": sorted(got_stems),
                "hit": hit,
                "miss": miss,
            })
        total_expected = sum(len(r["expected"]) for r in rows)
        total_hit = sum(len(r["hit"]) for r in rows)
        case_full_hit = sum(1 for r in rows if not r["miss"])
        return {
            "rows": rows,
            "table_recall": total_hit / total_expected if total_expected else 0.0,
            "case_full_hit_rate": case_full_hit / len(rows) if rows else 0.0,
            "total_expected": total_expected,
            "total_hit": total_hit,
            "n_cases": len(rows),
        }
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    cases = load_cases(n)
    configs = {
        "FULL(L1+L2+L3+L4)": {"CODEMAKER_RAG_MODE": None, "CODEMAKER_LOCATOR_FK_OFF": None},
        "NO_L4(关RAG)": {"CODEMAKER_RAG_MODE": "off", "CODEMAKER_LOCATOR_FK_OFF": None},
        "NO_L3(关FK扩展)": {"CODEMAKER_RAG_MODE": None, "CODEMAKER_LOCATOR_FK_OFF": "1"},
        "NO_L3_L4(全关)": {"CODEMAKER_RAG_MODE": "off", "CODEMAKER_LOCATOR_FK_OFF": "1"},
    }
    results = {}
    for name, env in configs.items():
        results[name] = run_config(cases, env)

    print("\n===== 分通道贡献度压测结果（S-A 8条真实用例，真实 LocatorAgent 调用）=====\n")
    print(f"{'配置':<20}{'表级recall':>12}{'样例全中率':>12}{'命中/期望':>12}")
    for name, r in results.items():
        print(f"{name:<20}{r['table_recall']*100:>10.1f}%{r['case_full_hit_rate']*100:>11.1f}%"
              f"{r['total_hit']:>6}/{r['total_expected']:<5}")

    print("\n----- 逐样例明细（FULL vs NO_L3_L4）-----")
    full_rows = results["FULL(L1+L2+L3+L4)"]["rows"]
    off_rows = results["NO_L3_L4(全关)"]["rows"]
    for i, (fr, orow) in enumerate(zip(full_rows, off_rows)):
        print(f"#{i} {fr['input']}")
        print(f"    期望: {fr['expected']}")
        print(f"    FULL命中: {fr['hit']} 漏: {fr['miss']}")
        print(f"    关L3L4命中: {orow['hit']} 漏: {orow['miss']}")

    out_path = ROOT / "bench" / f"ppt_locator_channel_ablation_raw_n{n}.json"
    out_path.parent.mkdir(exist_ok=True)
    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n原始数据已写入: {out_path}")


if __name__ == "__main__":
    main()

"""模拟压测：Step4 all_ok 计算从"仅镜像 s3.ok"改为"s1/s2/s3 全部 ok 才算 ok"。

目标文件：server/agent/excel/core/pipeline/step4_conclude_subagent.py

本次改动（见 git diff）：
    - s3_ok = (s3.ok if s3 else None)
    - if s3 is not None:
    -     all_ok = bool(s3_ok)
    - else:
    -     prior = [r for sid, r in ctx.results.items() if sid != STEP4_CONCLUDE]
    -     all_ok = bool(prior) and all(r.ok for r in prior)
    + prior = [r for sid, r in ctx.results.items() if sid != STEP4_CONCLUDE]
    + all_ok = bool(prior) and all(r.ok for r in prior)

旧逻辑：只要 s3 存在，all_ok 完全镜像 s3.ok，忽略 s1/s2 是否 ok（哪怕 Step1
解析出现 hard error 但 Step3 侧巧合全部 ok，也会汇报 all_ok=True，产生口径漂移）。
新逻辑：直接调用仓库真实代码里 `StepContext`/`StepResult` 构造 s1/s2/s3 三个
StepResult，塞进真实 `Step4ConcludeSubAgent.execute(ctx)`，读其返回
`StepResult.ok` 字段，即为真实 all_ok 计算结果（未复制表达式，是真实调用）。
"""
from __future__ import annotations

import itertools
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from agent.excel.core.pipeline.contracts import (  # noqa: E402
    STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, StepContext, StepResult,
)
from agent.excel.core.pipeline.step4_conclude_subagent import Step4ConcludeSubAgent  # noqa: E402

OUT_JSON = ROOT / "bench" / "ppt_step4_allok_drift.json"
OUT_MD = ROOT / "bench" / "ppt_step4_allok_drift.md"


def before_all_ok(s1_ok: bool, s2_ok: bool, s3_ok: bool) -> bool:
    """旧逻辑（按 diff 还原）：s3 存在时 all_ok 完全镜像 s3.ok，忽略 s1/s2。"""
    return bool(s3_ok)


def after_all_ok_via_real_step4(s1_ok: bool, s2_ok: bool, s3_ok: bool) -> bool:
    """真实新逻辑：构造 ctx 塞入真实 s1/s2/s3 StepResult，跑真实
    Step4ConcludeSubAgent.execute(ctx)，读返回 StepResult.ok。"""
    ctx = StepContext(session_id="sim", user_text="模拟指令")
    ctx.set_result(STEP1_PARSE, StepResult(step_id=STEP1_PARSE, ok=s1_ok))
    ctx.set_result(STEP2_VALIDATE, StepResult(step_id=STEP2_VALIDATE, ok=s2_ok))
    ctx.set_result(STEP3_EXECUTE, StepResult(
        step_id=STEP3_EXECUTE, ok=s3_ok,
        artifacts={"subtasks": [], "failures": []}))
    step4 = Step4ConcludeSubAgent(services=None)
    result = step4.execute(ctx)
    return bool(result.ok)


def main() -> None:
    rows = []
    drift_count = 0
    for s1_ok, s2_ok, s3_ok in itertools.product([True, False], repeat=3):
        b = before_all_ok(s1_ok, s2_ok, s3_ok)
        a = after_all_ok_via_real_step4(s1_ok, s2_ok, s3_ok)
        diff = (b != a)
        if diff:
            drift_count += 1
        rows.append({
            "s1_ok": s1_ok, "s2_ok": s2_ok, "s3_ok": s3_ok,
            "before_all_ok": b, "after_all_ok": a, "differs": diff,
        })

    out = {
        "note": ("模拟压测：Step4 all_ok 计算口径修复真值表。before 为按 git diff "
                 "还原的旧逻辑（all_ok 完全镜像 s3.ok，已被替换，不是猜测）；"
                 "after 为真实调用 Step4ConcludeSubAgent.execute(ctx)（未复制表达式，"
                 "是对生产代码的真实函数调用）读取的 StepResult.ok。"
                 "口径：全部 2^3=8 种布尔组合穷举，非真实端到端会话回放。"),
        "total_combinations": len(rows),
        "combinations_where_before_after_differ": drift_count,
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# Step4 all_ok 计算口径修复 真值表模拟压测",
        "",
        "> 口径：before 为按 git diff 还原的旧逻辑（`all_ok = s3.ok`，原始代码已被"
        "替换，此处按 diff 复原非猜测）；after 为**真实调用**"
        "`Step4ConcludeSubAgent.execute(ctx)`（构造真实 `StepContext`/`StepResult` "
        "塞入 s1/s2/s3，跑生产代码返回值），不是复制表达式。"
        "全部 2³=8 种 (s1.ok, s2.ok, s3.ok) 布尔组合穷举，非真实端到端会话回放。",
        "",
        f"## 本次修复覆盖场景数：{drift_count}/8",
        "",
        "| s1.ok | s2.ok | s3.ok | Before all_ok | After all_ok | 是否不同（本次修复覆盖） |",
        "|---|---|---|---|---|---|",
    ]
    for r in rows:
        md.append(
            f"| {r['s1_ok']} | {r['s2_ok']} | {r['s3_ok']} | "
            f"{r['before_all_ok']} | {r['after_all_ok']} | "
            f"{'⚠️ 是' if r['differs'] else '否'} |")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()

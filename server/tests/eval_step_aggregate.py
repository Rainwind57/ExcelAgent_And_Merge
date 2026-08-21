"""eval_step_aggregate：链按 step 位置聚合成功率 + 断点热步分布。

capability: step-wise-success-aggregation

读现有 task_chain_eval JSON 的 chains[].steps[].entry.status，按 step 位置
（0-based）聚合全链平均成功率，输出逐步表 + 断点热步。仅同长度链该 step
计入分母，短链后续 step 计 N/A 不计入。

CLI: python -m tests.eval_step_aggregate --report <path> [--baseline <run_id>]
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

# entry.status 取值
_STATUS_KEYS = ("matched", "partial", "located_only", "missing")
# 热步阈值：break_count / chain_count >= 此值标热步
_HOT_STEP_RATIO = 0.3

_RUN_ID_RE = re.compile(r"^(.+?)_(\d{8}_\d{6}_\w+(?:_.+)?)$")


def aggregate_step_success(json_path: str) -> dict:
    """读 JSON 聚合逐步成功率。文件缺失/无 chains → {"error": ...}。"""
    p = Path(json_path)
    if not p.exists():
        return {"error": f"file not found: {json_path}"}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as e:
        return {"error": f"read failed: {e}"}
    chains = data.get("chains") if isinstance(data, dict) else None
    if not chains:
        return {"error": "no chains"}

    chain_count = len(chains)
    max_len = max((len(c.get("steps", [])) for c in chains), default=0)
    step_wise = []
    for idx in range(max_len):
        total = 0
        by_status = {k: 0 for k in _STATUS_KEYS}
        for c in chains:
            steps = c.get("steps", []) or []
            if idx < len(steps):
                total += 1
                entry = steps[idx].get("entry", {}) or {}
                st = entry.get("status", "missing")
                if st not in by_status:
                    st = "missing"
                by_status[st] += 1
        matched = by_status["matched"]
        success_rate = matched / total if total else 0.0
        step_wise.append({
            "step_index": idx,
            "success_rate": success_rate,
            "total": total,
            "by_status": by_status,
            "break_count": total - matched,  # status != matched 的计数
        })
    return {"chain_count": chain_count, "step_wise": step_wise}


def format_step_report(agg: dict) -> str:
    if "error" in agg:
        return f"# 逐步成功率报告\n\n错误: {agg['error']}\n"
    chain_count = agg.get("chain_count", 0)
    lines = ["# 逐步成功率报告", "", "## 逐步成功率", "",
             "| step | 成功率 | total | break | by_status |",
             "|---|---|---|---|---|"]
    hot = []
    for s in agg.get("step_wise", []):
        rate = s["success_rate"]
        lines.append(f"| {s['step_index']} | {rate * 100:.1f}% | {s['total']} | "
                     f"{s['break_count']} | {s['by_status']} |")
        if chain_count and s["break_count"] > 0 \
                and (s["break_count"] / chain_count) >= _HOT_STEP_RATIO:
            hot.append(s["step_index"])
    if hot:
        lines += ["", "## 断点热步", "", f"- 热步 step: {hot}"]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description="链逐步成功率聚合")
    ap.add_argument("--report", default="reports/task_chain_eval_latest.json",
                    help="task_chain_eval JSON 路径")
    ap.add_argument("--baseline", default="", help="从归档 run_id 读")
    args = ap.parse_args()

    path = args.report
    if args.baseline:
        # 从 reports/archive/task_chain_eval_<run_id>.json 读
        archive = Path(__file__).resolve().parent / "reports" / "archive"
        m = _RUN_ID_RE.match(args.baseline)
        script = m.group(1) if m else "task_chain_eval"
        path = str(archive / f"{script}_{args.baseline}.json")
    agg = aggregate_step_success(path)
    print(format_step_report(agg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

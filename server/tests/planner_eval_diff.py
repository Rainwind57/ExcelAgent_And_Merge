"""planner_eval_diff：step1_planner_eval 两次 run 的分层指标 diff（路线图 §8）。

把 step1_planner_eval 落盘的 summary（raw 计数）转成分层指标（Step1 JSON 指标），
对比 before/after（或 DeepSeek / codemaker 双底座）两次归档：

指标方向（§8 Step1 JSON 指标）：
  - 表命中率 / sheet 命中率 / action 命中率 / 整条全对率 → higher_better
  - 字段键命中率 / 键值一致率 → higher_better
  - 占位符闭环率 → higher_better；悬空占位符数 → lower_better
  - 实际产出意图数 → 中性（仅展示，不判方向）

用法（双底座对比：同一批 cases，两个模型各跑一次 step1_planner_eval 并归档）：
    CODEMAKER_MODEL=opencode/deepseek-v4-flash-free python -m tests.step1_planner_eval
    CODEMAKER_MODEL=opencode/glm-4.7-free     python -m tests.step1_planner_eval
    python -m tests.planner_eval_diff --before <before.json> --after <after.json>

纯函数、0 LLM、确定性。
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

# metric -> (方向, 取值函数)。higher_better=值越大越好；lower_better=越小越好。
_METRICS: dict[str, tuple[str, object]] = {
    "actual_count": ("neutral", lambda s: int(s.get("actual", 0))),
    "table_hit_rate": ("higher_better", lambda s: _rate(s, "table_hit")),
    "sheet_hit_rate": ("higher_better", lambda s: _rate(s, "sheet_hit")),
    "action_hit_rate": ("higher_better", lambda s: _rate(s, "action_hit")),
    "full_match_rate": ("higher_better", lambda s: _rate(s, "full")),
    "keys_found_rate": ("higher_better", lambda s: _rate(s, "keys_found", "keys_exp")),
    "keys_ok_rate": ("higher_better", lambda s: _rate(s, "keys_ok", "keys_exp")),
    "placeholder_resolved_rate": ("higher_better", lambda s: _rate(s, "ph_ok", "ph_total")),
    "placeholder_unresolved": ("lower_better", lambda s: int(s.get("ph_unresolved", 0))),
    "locate_ok_rate": ("higher_better", lambda s: _rate(s, "loc_ok", "loc_total")),
}


def _rate(s: dict, num_key: str, den_key: str = "expected") -> float:
    den = int(s.get(den_key, 0))
    if not den:
        return 0.0
    return int(s.get(num_key, 0)) / den


def _status(direction: str, delta: float) -> str:
    if direction == "neutral":
        return "neutral"
    if delta == 0:
        return "neutral"
    improved = delta > 0 if direction == "higher_better" else delta < 0
    return "improved" if improved else "regressed"


def compare_planner(before: dict, after: dict) -> dict:
    """对比两次 planner eval summary，返回逐指标 delta + status。"""
    rows = []
    improved = regressed = 0
    for metric, (direction, getter) in _METRICS.items():
        b = getter(before)
        a = getter(after)
        delta = a - b
        st = _status(direction, delta)
        if st == "improved":
            improved += 1
        elif st == "regressed":
            regressed += 1
        rows.append({
            "metric": metric, "direction": direction,
            "before": round(b, 4), "after": round(a, 4),
            "delta": round(delta, 4), "status": st,
        })
    return {
        "before": {"expected": int(before.get("expected", 0)),
                    "actual": int(before.get("actual", 0))},
        "after": {"expected": int(after.get("expected", 0)),
                   "actual": int(after.get("actual", 0))},
        "improved": improved, "regressed": regressed,
        "rows": rows,
    }


def format_planner_diff(diff: dict) -> str:
    lines = ["| 指标 | before | after | delta | 状态 |",
             "|---|---|---|---|---|"]
    for r in diff["rows"]:
        b = _fmt(r["before"], r["metric"])
        a = _fmt(r["after"], r["metric"])
        d = _fmt(r["delta"], r["metric"])
        lines.append(f"| {r['metric']} | {b} | {a} | {d} | {r['status']} |")
    lines.append("")
    lines.append(f"改进 {diff['improved']} / 回退 {diff['regressed']} / 中性 "
                 f"{len(diff['rows']) - diff['improved'] - diff['regressed']}")
    return "\n".join(lines)


def _fmt(v: float, metric: str) -> str:
    if "rate" in metric:
        return f"{v*100:.0f}%"
    return f"{v:.2f}"


def _load(path: str) -> dict:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        return {}
    # 直接落盘的 raw summary（step1_planner_eval 的 reports/*.json）
    if "summary" in data:
        return data["summary"]
    # eval_baseline 归档包装 {_header, results:{summary, results}}
    results = data.get("results")
    if isinstance(results, dict) and "summary" in results:
        return results["summary"]
    return data


def main() -> int:
    ap = argparse.ArgumentParser(description="planner eval 双 run 指标 diff")
    ap.add_argument("--before", required=True)
    ap.add_argument("--after", required=True)
    args = ap.parse_args()
    before = _load(args.before)
    after = _load(args.after)
    diff = compare_planner(before, after)
    print(format_planner_diff(diff))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

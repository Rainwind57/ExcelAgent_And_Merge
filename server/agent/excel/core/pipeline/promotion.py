"""error-budget 促升门控（纯函数，0 LLM）。

背景（docs §"用错误预算管理风险" + §灰度纪律）：新变更（新 planner / 模板降级 /
Step1 重构）能否从 shadow 灰度切主用，必须由**数据 + 硬指标**决定，而非拍脑袋：

  - 表 recall 不能比 baseline 跌超过 max_recall_drop（默认 2%）。
  - 不能新增缺表（total_missing 不增）。
  - field_recall 不能跌超过 max_field_drop。
  - 达标 → promote；否则 hold + 给出具体未达标项。

纯函数、确定性，输入两份 aggregate_scores 聚合 + 预算阈值，输出判定。
"""
from __future__ import annotations

from typing import Optional

__all__ = ["DEFAULT_ERROR_BUDGET", "evaluate_promotion"]

DEFAULT_ERROR_BUDGET = {
    "max_recall_drop": 0.02,      # 表 recall 允许的最大下降（绝对值，2%）
    "max_field_drop": 0.05,       # 字段 recall 允许的最大下降
    "allow_new_missing": 0,       # 允许新增的缺表总数
    "min_recall": 0.0,            # 候选 recall 绝对下限（可选硬门槛）
}


def evaluate_promotion(baseline: dict, candidate: dict,
                       budget: Optional[dict] = None) -> dict:
    """对比 baseline 与 candidate 聚合分数，按 error-budget 判是否可促升。

    Args:
        baseline / candidate: aggregate_scores 输出。
        budget: 覆盖 DEFAULT_ERROR_BUDGET 的阈值。

    Returns:
        {"promote": bool, "violations": [str...], "deltas": {...}}
    """
    b = dict(DEFAULT_ERROR_BUDGET)
    b.update(budget or {})
    baseline = baseline or {}
    candidate = candidate or {}

    def _g(d, k): 
        v = d.get(k, 0.0)
        return v if isinstance(v, (int, float)) else 0.0

    recall_delta = round(_g(candidate, "avg_table_recall") - _g(baseline, "avg_table_recall"), 4)
    field_delta = round(_g(candidate, "avg_field_recall") - _g(baseline, "avg_field_recall"), 4)
    prec_delta = round(_g(candidate, "avg_table_precision") - _g(baseline, "avg_table_precision"), 4)
    missing_delta = int(_g(candidate, "total_missing_tables") - _g(baseline, "total_missing_tables"))

    violations: list[str] = []
    if recall_delta < -b["max_recall_drop"]:
        violations.append(
            f"表 recall 跌 {abs(recall_delta):.3f} > 预算 {b['max_recall_drop']:.3f}")
    if field_delta < -b["max_field_drop"]:
        violations.append(
            f"字段 recall 跌 {abs(field_delta):.3f} > 预算 {b['max_field_drop']:.3f}")
    if missing_delta > b["allow_new_missing"]:
        violations.append(
            f"新增缺表 {missing_delta} > 允许 {b['allow_new_missing']}")
    if _g(candidate, "avg_table_recall") < b["min_recall"]:
        violations.append(
            f"候选 recall {_g(candidate,'avg_table_recall'):.3f} < 下限 {b['min_recall']:.3f}")

    return {
        "promote": not violations,
        "violations": violations,
        "deltas": {
            "recall": recall_delta,
            "precision": prec_delta,
            "field_recall": field_delta,
            "missing_tables": missing_delta,
        },
    }

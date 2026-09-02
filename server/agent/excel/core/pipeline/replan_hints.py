"""最小清单#6 + P2：Step2 replan_hints（纯函数，0 LLM）。

背景（docs §P2"Step2 发现 Step1 计划不完整时应返回 replan hints：缺哪张表、
哪条 FK、哪个 producer label"）：把 Step2 的校验错误归纳成**结构化、可机器处理**的
重规划提示，供 Step1/planner 下一轮修计划（而不是仅报 validation issue）。

本模块只做确定性归类（error → replan hint），纯函数、无 IO、无 LLM、不改判定门。
默认不开 hard gate（灰度纪律）：hints 只作 artifacts 附带信息，供 shadow/诊断消费。
"""
from __future__ import annotations

from typing import Any, Iterable

__all__ = ["build_replan_hints"]

# error_type（含 issue_type / root_cause 前缀）→ replan hint kind
_KIND_MAP = {
    "col_not_found": "missing_column",
    "type_mismatch": "fix_field_type",
    "unique_violation": "pk_conflict",
    "missing_required": "missing_required",
    "upstream_placeholder_unresolved": "missing_producer",
    "intent_coverage_gap": "missing_table",
    "segment_partial_coverage": "missing_table",
    "plan_incomplete": "missing_producer",
}

_SUGGESTION = {
    "missing_column": "核对真实表头列名，或让 planner 换用正确列",
    "fix_field_type": "该列类型不符，planner 应产合法类型值或改列",
    "pk_conflict": "主键冲突，planner 应换主键值或改为 set",
    "missing_required": "补齐业务必填/主键列",
    "missing_producer": "缺上游 producer，planner 应补前置新增意图并连 produces/consumes",
    "missing_table": "分段覆盖缺表，planner 应补该表的意图",
}


def _get(obj: Any, *keys: str) -> Any:
    for k in keys:
        if isinstance(obj, dict):
            v = obj.get(k)
        else:
            v = getattr(obj, k, None)
        if v not in (None, ""):
            return v
    return None


def _classify(error_type: str, issue_type: str, root: str) -> str:
    hay = " ".join(str(x or "").lower() for x in (error_type, issue_type, root))
    for key, kind in _KIND_MAP.items():
        if key in hay:
            return kind
    return ""


def build_replan_hints(errors: Iterable[Any]) -> list[dict]:
    """把 Step2 错误（StepError 或 failure dict）归纳为结构化 replan hints。

    Returns: [{kind, table, sheet, col, producer_label, detail, suggestion}]，
    去重（同 kind+table+sheet+col 只留一条）。
    """
    hints: list[dict] = []
    seen: set = set()
    for e in (errors or []):
        error_type = str(_get(e, "error_type", "type") or "")
        issue_type = str(_get(e, "issue_type") or "")
        root = str(_get(e, "root_cause", "message") or "")
        kind = _classify(error_type, issue_type, root)
        if not kind:
            continue
        table = _get(e, "table")
        sheet = _get(e, "sheet")
        col = _get(e, "column", "col")
        producer = _get(e, "producer_label", "producer") or ""
        key = (kind, table, sheet, col)
        if key in seen:
            continue
        seen.add(key)
        hints.append({
            "kind": kind,
            "table": table or "",
            "sheet": sheet or "",
            "col": col or "",
            "producer_label": producer,
            "detail": root or error_type,
            "suggestion": _SUGGESTION.get(kind, ""),
        })
    return hints

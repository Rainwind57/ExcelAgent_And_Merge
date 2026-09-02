"""金标 shadow 评测打分（纯函数，0 LLM）。

给定真实管线产出的 intents 与金标真值 expect（tables + 可选 fields），确定性打分：
表级 recall/precision、缺表/多表、字段覆盖率。用于把"拆得对不对"量化，供
error-budget 门控（promotion.py）判定新变更能否灰度/切主用。

纯函数、无 IO、无 LLM，可离线单测（不依赖真实 API）。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = ["score_case", "aggregate_scores"]

_NORM_RE = re.compile(r"[\s_:\-./\\()\[\]{}（）【】]+")


def _norm(v: object) -> str:
    return _NORM_RE.sub("", str(v if v is not None else "").split(":")[0]).strip().lower()


def _table_of(it: Any) -> str:
    v = it.get("table_hint") or it.get("table") if isinstance(it, dict) \
        else (getattr(it, "table_hint", None) or getattr(it, "table", None))
    return _norm(v)


def _fields_of(it: Any) -> dict:
    if isinstance(it, dict):
        f = it.get("fields") or (it.get("extras") or {}).get("fields")
    else:
        ex = getattr(it, "extras", None) or {}
        f = getattr(it, "fields", None) or ex.get("fields")
    return f if isinstance(f, dict) else {}


def score_case(actual_intents: Iterable[Any], expect: dict) -> dict:
    """对单条金标打分。

    Args:
        actual_intents: 真实管线产出的 intents（dict 或对象，含 table_hint/fields）。
        expect: {"tables": [...], "fields": {table: [cols]}}。

    Returns:
        {table_recall, table_precision, missing_tables, extra_tables,
         field_recall, n_expected_tables, n_actual_tables}
    """
    expect = expect or {}
    exp_tables = {_norm(t) for t in (expect.get("tables") or []) if _norm(t)}
    act_tables = {_table_of(it) for it in (actual_intents or []) if _table_of(it)}

    hit = exp_tables & act_tables
    recall = len(hit) / len(exp_tables) if exp_tables else 1.0
    precision = len(hit) / len(act_tables) if act_tables else (1.0 if not exp_tables else 0.0)
    missing = sorted(exp_tables - act_tables)
    extra = sorted(act_tables - exp_tables)

    # 字段覆盖率：期望某表关键列在该表任一 intent 的 fields 键出现的比例
    exp_fields = expect.get("fields") or {}
    field_hits = 0
    field_total = 0
    if exp_fields:
        # 归一化 actual：table_norm -> 该表所有 intent 的字段键归一集合
        by_table: dict[str, set] = {}
        for it in (actual_intents or []):
            t = _table_of(it)
            if not t:
                continue
            by_table.setdefault(t, set()).update(
                _norm(k) for k in _fields_of(it).keys() if str(k).strip())
        for tbl, cols in exp_fields.items():
            tnorm = _norm(tbl)
            have = by_table.get(tnorm, set())
            for c in (cols or []):
                field_total += 1
                if _norm(c) in have:
                    field_hits += 1
    field_recall = (field_hits / field_total) if field_total else 1.0

    return {
        "table_recall": round(recall, 4),
        "table_precision": round(precision, 4),
        "missing_tables": missing,
        "extra_tables": extra,
        "field_recall": round(field_recall, 4),
        "n_expected_tables": len(exp_tables),
        "n_actual_tables": len(act_tables),
    }


def aggregate_scores(scores: Iterable[dict]) -> dict:
    """聚合多条 case 分数。返回均值 recall/precision/field_recall + 计数。"""
    scores = list(scores or [])
    n = len(scores) or 1
    def _avg(k): return round(sum(s.get(k, 0.0) for s in scores) / n, 4)
    total_missing = sum(len(s.get("missing_tables") or []) for s in scores)
    total_extra = sum(len(s.get("extra_tables") or []) for s in scores)
    perfect = sum(1 for s in scores
                  if s.get("table_recall", 0) >= 1.0 and not s.get("missing_tables"))
    return {
        "cases": len(scores),
        "avg_table_recall": _avg("table_recall"),
        "avg_table_precision": _avg("table_precision"),
        "avg_field_recall": _avg("field_recall"),
        "total_missing_tables": total_missing,
        "total_extra_tables": total_extra,
        "perfect_recall_cases": perfect,
    }

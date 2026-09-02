"""建议4：schema-first 字段归属划分（纯函数，0 LLM）。

背景（docs §"LLM 不应该背 schema" + schema grounding）：schema（候选表/sheet/列/
必填/FK）应一次形成，LLM 只输出真实列名；产出的字段键若不在 schema 列集合内，
应归入 unknown_fields（供 Step2/诊断可见 + 写前闸门处置），而不是先硬塞进 fields
造成脏写/幻觉列。

本模块提供确定性划分：给定 fields 与目标 sheet 的表头列集合，按归一化列名成员关系
分成 known / unknown。纯函数、无 IO、无 LLM。

注意：这里用**归一化精确成员**判定（去后缀/分隔/大小写），刻意保守——只把明显
不属于该 schema 的键判为 unknown；点分嵌套键（effect.data.N.x）与占位键交由既有
翻译/闸门链处理，不在此误判。
"""
from __future__ import annotations

from typing import Iterable

from .column_gate import normalize_column_name

__all__ = ["partition_fields_by_schema"]


def partition_fields_by_schema(
    fields: dict,
    header_names: Iterable[object],
) -> tuple[dict, dict]:
    """把 fields 按目标 sheet 表头列集合划分为 (known, unknown)。

    Args:
        fields: LLM/规则产出的 {列名: 值}。
        header_names: 目标 sheet 的真实表头列名（可含 :type 后缀）。

    Returns:
        (known_fields, unknown_fields)。判定保守：
          - 键归一后命中表头归一集合 → known。
          - 点分键（含 '.'）→ 视为 known（交既有 dotted 翻译链处理，不误判）。
          - 纯数字键 → 视为 unknown（列序号退化键，非真实列名）。
          - 其余不命中 → unknown。
    """
    header_norm = {normalize_column_name(h) for h in (header_names or [])
                   if str(h or "").strip()}
    known: dict = {}
    unknown: dict = {}
    for k, v in (fields or {}).items():
        ks = str(k).strip()
        if not ks:
            unknown[k] = v
            continue
        if "." in ks:                     # 点分嵌套键交既有翻译链，不误判
            known[k] = v
            continue
        if ks.isdigit():                  # 纯数字=列序号退化键
            unknown[k] = v
            continue
        if normalize_column_name(k) in header_norm:
            known[k] = v
        else:
            unknown[k] = v
    return known, unknown

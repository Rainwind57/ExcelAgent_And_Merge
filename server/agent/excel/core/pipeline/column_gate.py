"""现象3 列匹配闸门（纯函数，0 LLM，无副作用）。

背景（docs/ExcelAgent_4Step_问题诊断与优化策略）：Step3 执行 add 时，逐列匹配
表头；某列 key 无法匹配（ColumnMatcher.match 返回 None）时旧逻辑只 `continue`
跳过该列，但仍照写其余列 → 写出"关键列缺失的残缺行"，且 res 被 match_field
失败预毒化为 ok=False，形成"文件已脏写 / 整体判失败"的自相矛盾。

本模块提供通用判定：给定"本行已成功匹配落库的列 covered_columns"、"本行无法
绑定表头的字段键 unmatched_keys"、以及"本 sheet 的关键列 key_columns"，判定：

  - abort  ：存在关键列未被覆盖 且 存在无法绑定的字段键 → 整行判失败、禁止写盘、
             回 Step2（关键列可能就是那个没绑上的键，写盘会产脏行）。
  - partial：仅非关键列未匹配 → 跳过这些字段照写其余列，写后把 res 归正为 partial。
  - ok     ：没有未匹配键 → 无需闸门动作。

关键列口径与项目 P26 原则一致（validator_agent 只强校验主键）：由调用方传入
"复合主键列"（单列主键自增分配，不参与本闸门）。不绑表名/字段特判——纯靠传入
的 schema 派生集合泛化判定。
"""
from __future__ import annotations

import re
from typing import Callable, Iterable, Optional

__all__ = ["normalize_column_name", "evaluate_column_match_gate"]

_NORM_RE = re.compile(r"[\s_:\-./\\()\[\]{}（）【】]+")


def normalize_column_name(value: object) -> str:
    """列名归一化：取冒号前主名、去分隔符/空白、转小写。

    与 agent 内部 ColumnMatcher / 复合主键比对口径保持一致，供闸门做集合运算。
    """
    core = str(value or "").split(":")[0]
    return _NORM_RE.sub("", core).strip().lower()


def evaluate_column_match_gate(
    *,
    unmatched_keys: Iterable[object],
    covered_columns: Iterable[object],
    key_columns: Iterable[object],
    normalizer: Optional[Callable[[object], str]] = None,
) -> dict:
    """判定列匹配闸门动作。纯函数：仅依赖入参，无 IO / 无 LLM。

    Args:
        unmatched_keys: 本行中无法绑定到任何表头的字段键（LLM/规则给的列名）。
        covered_columns: 本行已成功匹配、将要落库的列（表头名或归一名均可）。
        key_columns: 本 sheet 的关键列（复合主键列名）。空集合 → 永不 abort。
        normalizer: 列名归一函数（默认 normalize_column_name）。

    Returns:
        {
          "action": "ok" | "partial" | "abort",
          "uncovered_key_columns": [归一后的未覆盖关键列, ...],
          "unmatched_keys": [原始未匹配键, ...],
          "reason": str,
        }
    """
    norm = normalizer or normalize_column_name

    unmatched = [str(k) for k in (unmatched_keys or []) if str(k or "").strip()]
    key_norm = {norm(c) for c in (key_columns or []) if str(c or "").strip()}
    covered_norm = {norm(c) for c in (covered_columns or []) if str(c or "").strip()}
    uncovered = sorted(key_norm - covered_norm)

    if not unmatched:
        return {
            "action": "ok",
            "uncovered_key_columns": uncovered,
            "unmatched_keys": [],
            "reason": "",
        }
    if uncovered:
        return {
            "action": "abort",
            "uncovered_key_columns": uncovered,
            "unmatched_keys": unmatched,
            "reason": (
                f"关键列未匹配：{uncovered}；存在无法绑定表头的字段键：{unmatched}。"
                f"为避免写出残缺行，整行判失败、禁止写盘、回 Step2 修正。"
            ),
        }
    return {
        "action": "partial",
        "uncovered_key_columns": [],
        "unmatched_keys": unmatched,
        "reason": f"仅非关键列未匹配，跳过不写：{unmatched}",
    }

"""Phase 2-a：确定性字段值抽取（0 LLM）。

定位（plan-first §Phase 2「确定性优先」）：逐实体填值前，先用规则/正则从原文那一段
把「一眼能提」的字段值确定性抽出来——占字段的一大半（id、数字、引号名、显式
"列名：值"）——只有残余模糊字段才交给 LLM。直接砍掉大量 LLM 调用 → 治超时。

高精度优先（宁可少提不可提错）：
  1. header 锚定的 "列名<分隔>值"：分隔符 ：:=＝ 或 词 为/是；列名必须命中给定表头。
     这是最强信号，几乎无歧义。
  2. 引号名：「」『』""''《》 内的内容，作为候选名称值（供未解字段/LLM 参考）。
  3. 裸数字串：作为候选 id/数值（供未解字段/LLM 参考）。

只做抽取、不猜字段归属（除 header 锚定外）；把未解字段与残余候选如实返回，
由调用方决定是否调 LLM 补齐。纯函数、无副作用、可离线确定性验证。
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

# header 与值之间的分隔符（含中英文冒号 / 等号 / "为" "是"）
_SEP = r"(?:\s*(?:[:：=＝]|为|是)\s*)"
# 值终止边界（下一个分隔/标点/换行）
_VALUE = r"([^，,；;、。\n\r]+?)(?=(?:[，,；;、。\n\r])|$)"

_QUOTE_RE = re.compile(r"[「『\"'“”‘’《]([^」』\"'“”‘’》]+)[」』\"'“”‘’》]")
_NUMBER_RE = re.compile(r"(?<![\w.])(\d{2,})(?![\w.])")


def _clean_value(raw: str) -> str:
    v = str(raw or "").strip()
    # 去成对引号
    for lq, rq in (("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"),
                   ("“", "”"), ("‘", "’")):
        if v.startswith(lq) and v.endswith(rq) and len(v) >= 2:
            v = v[1:-1].strip()
            break
    return v


def extract_fields_from_text(
    text: str,
    headers: Iterable[str],
    existing: Optional[dict] = None,
) -> dict:
    """从单段原文里确定性抽取字段值。

    Args:
        text: 该实体对应的原文片段。
        headers: 目标表的表头列名列表（用于 header 锚定匹配）。
        existing: 已有字段（不覆盖；仅补齐缺失）。

    Returns:
        {
          "fields": {col: value},        # 确定性抽出的字段（header 锚定）
          "resolved": [col, ...],        # 本次新解出的列
          "unresolved": [col, ...],      # 仍缺、需 LLM 的表头列
          "residual_quoted": [str, ...], # 未消费的引号名候选
          "residual_numbers": [str, ...],# 未消费的数字候选
        }
    """
    text = str(text or "")
    existing = dict(existing or {})
    header_list = [str(h).strip() for h in (headers or []) if str(h).strip()]

    fields: dict[str, Any] = {}
    consumed_spans: list[tuple[int, int]] = []

    def _overlaps(a: int, b: int) -> bool:
        return any(not (b <= s or a >= e) for s, e in consumed_spans)

    # 规则 1：header 锚定 "列名<分隔>值"。长表头优先，避免短列名先吃掉子串。
    for h in sorted(set(header_list), key=len, reverse=True):
        if h in existing or h in fields:
            continue
        pat = re.compile(re.escape(h) + _SEP + _VALUE)
        m = pat.search(text)
        if not m:
            continue
        # 命中区间若与已消费区间重叠（如短表头落在长表头子串里）→ 跳过
        if _overlaps(m.start(), m.end()):
            continue
        val = _clean_value(m.group(1))
        if val == "":
            continue
        # 值若恰好是另一个表头名（无实际值）则跳过
        if val in header_list:
            continue
        fields[h] = val
        consumed_spans.append((m.start(), m.end()))

    # 残余候选：从"未被消费"的文本里收集引号名 / 数字（供未解字段 / LLM 参考）。
    def _in_consumed(pos: int) -> bool:
        return any(s <= pos < e for s, e in consumed_spans)

    residual_quoted: list[str] = []
    for m in _QUOTE_RE.finditer(text):
        if _in_consumed(m.start(1)):
            continue
        v = m.group(1).strip()
        if v and v not in residual_quoted:
            residual_quoted.append(v)

    residual_numbers: list[str] = []
    for m in _NUMBER_RE.finditer(text):
        if _in_consumed(m.start(1)):
            continue
        v = m.group(1)
        if v not in residual_numbers:
            residual_numbers.append(v)

    resolved = list(fields.keys())
    unresolved = [h for h in header_list
                  if h not in fields and h not in existing]
    return {
        "fields": fields,
        "resolved": resolved,
        "unresolved": unresolved,
        "residual_quoted": residual_quoted,
        "residual_numbers": residual_numbers,
    }


def has_uncovered_literal_values(text: str, field_values: Iterable[Any]) -> bool:
    """判断原文里的引号名/裸数字是否已经被现有字段值（跨所有 intent）覆盖。

    用途：LLM 字段自检补漏（_llm_complete_fields）调用前的低成本预判——若原文
    出现的字面值全部已在产出字段里出现过，大概率无字段可补，跳过一次昂贵 LLM
    调用；只要有一个字面值未被覆盖，才值得真调 LLM 去核对。

    纯启发式、无表名/列名硬编码，只做字符串包含关系比较（宁可误判"值得查"，
    不可误判"无需查"而漏检——包含关系判断天然偏保守/宽松，不会漏报）。

    Args:
        text: 原始指令文本。
        field_values: 当前所有 intent 已产出的字段值（跨表/跨 intent 合并）。

    Returns:
        True 表示存在原文字面值未被现有字段覆盖（值得调 LLM 核对）；
        False 表示原文所有引号名/裸数字都已出现在某个字段值里（可跳过）。
    """
    text = str(text or "")
    have = " \x00 ".join(str(v).strip() for v in (field_values or []) if str(v).strip())
    if not have:
        # 没有任何已产出字段值，但原文有字面值候选 → 明显值得一查
        for m in _QUOTE_RE.finditer(text):
            if m.group(1).strip():
                return True
        for m in _NUMBER_RE.finditer(text):
            if m.group(1).strip():
                return True
        return False
    for m in _QUOTE_RE.finditer(text):
        v = m.group(1).strip()
        if v and v not in have:
            return True
    for m in _NUMBER_RE.finditer(text):
        v = m.group(1).strip()
        if v and v not in have:
            return True
    return False


__all__ = ["extract_fields_from_text", "has_uncovered_literal_values"]

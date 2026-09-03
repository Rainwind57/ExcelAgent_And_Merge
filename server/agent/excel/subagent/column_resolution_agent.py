"""Column Resolution Agent（文档三.4 / 四.第二阶段.1）。

把"字段短语/坏列名 → 真实列名"的判断从 validator 里的裸字符串消歧
(`_resolve_col_with_llm`)独立出来，升级为结构化输出：

    {"mappings": [{"phrase","column","confidence","reason"}, ...],
     "ambiguous": [{"phrase","candidates":[...],"needs_user_confirm":True,"reason"}]}

设计原则（对齐文档五.1/五.2）：
- 不写任何表名/列名硬编码映射规则。判断依据始终是「真实表头 + 值语义 + 原始
  文本片段 + LLM 结构化裁决」，本模块只负责组 prompt、解析 JSON、做"幻觉列名"
  白名单过滤（LLM 选出的列必须真实存在于表头里才采信）。
- confidence 是通用元层信号（0~1），不针对任何具体业务列名；低于阈值的命中
  一律降级为 ambiguous，交上层决定是否需要人工确认，而不是由本模块替业务判断。
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Callable, Optional

logger = logging.getLogger(__name__)

CallLLMRaw = Callable[..., Optional[str]]

# 置信度分界：低于此值即使 LLM 给出唯一候选，也归入 ambiguous 需人工确认。
# 通用元层阈值（不针对任何具体业务列名），可用 env 调，默认 0.6。
MIN_CONFIDENCE = float(
    os.environ.get("CODEMAKER_COLUMN_RESOLUTION_MIN_CONFIDENCE", "0.6") or 0.6)


def _norm(s) -> str:
    try:
        from ..schema_bundle import _norm_col_name
        return _norm_col_name(s)
    except Exception:
        return str(s or "").strip()


def resolve_columns(call_llm_raw: Optional[CallLLMRaw], unresolved: list[dict],
                    headers: list, type_row: Optional[list] = None,
                    raw: str = "", timeout: int = 30) -> dict:
    """批量解析未匹配列名短语 → 真实列名，一次 LLM 调用覆盖多个短语。

    Args:
        call_llm_raw: 宿主注入的 LLM 原文调用（复用 SubAgent._call_llm_raw，
            签名 (prompt, timeout) -> Optional[str]）。None 时直接返回空结果。
        unresolved: [{"phrase": 短语/坏列名, "value": 对应值}, ...]。
        headers: 该表真实表头。
        type_row: 对应 row2 类型/规范名，供语义参考（可选）。
        raw: 原始指令片段，供语义参考（可选）。
        timeout: LLM 超时秒数。

    Returns:
        {"mappings": [...], "ambiguous": [...]}；LLM 不可用/解析失败/表头为空
        时返回两个空列表（上层原有兜底逻辑不受影响，零回归）。
    """
    empty = {"mappings": [], "ambiguous": []}
    if not unresolved or not headers or call_llm_raw is None:
        return empty
    cand_labels = []
    for h, t in zip(headers or [], (type_row or [])):
        n = _norm(h)
        if n:
            cand_labels.append(n + (f"（{t}）" if t else ""))
    if not cand_labels:
        return empty
    items = []
    for i, u in enumerate(unresolved):
        phrase = str(u.get("phrase", "") or "")[:40]
        value = "" if u.get("value") is None else str(u.get("value"))[:60]
        items.append(f"{i + 1}. 短语「{phrase}」值「{value}」")
    prompt = (
        "配表列名消歧。下面这些字段短语/列名在真实表头里找不到完全匹配的列：\n"
        + "\n".join(items) + "\n"
        "该表真实列名清单：" + "、".join(cand_labels)[:1000] + "\n"
        + (f"原始指令片段（供语义参考）：{raw[:200]}\n" if raw else "")
        + "对每一条，从真实列名清单里选出语义最接近的一个列，给出 0~1 置信度和"
          "一句简短理由（依据列名含义+值的语义，不要编造清单外的列名）。若某条"
          "有两个以上都说得过去的候选，把它们都放进 ambiguous 的 candidates，"
          "needs_user_confirm 设 true，不要放进 mappings。\n"
        "只输出一个 JSON 对象，不要输出其它文字：\n"
        '{"mappings":[{"phrase":"...","column":"清单内的真实列名","confidence":0.0,'
        '"reason":"..."}],"ambiguous":[{"phrase":"...","candidates":["...","..."],'
        '"needs_user_confirm":true,"reason":"..."}]}'
    )
    try:
        resp = call_llm_raw(prompt, timeout)
    except Exception:
        logger.debug("column_resolution_agent LLM 调用异常", exc_info=True)
        return empty
    if not resp:
        return empty
    m = re.search(r"\{.*\}", resp, re.DOTALL)
    if not m:
        logger.warning("column_resolution_agent 无 JSON 响应")
        return empty
    try:
        d = json.loads(m.group(0))
    except ValueError:
        logger.warning("column_resolution_agent JSON 解析失败")
        return empty
    if not isinstance(d, dict):
        return empty
    real_set = {_norm(h) for h in (headers or []) if h}
    mappings: list = []
    ambiguous: list = []
    for it in (d.get("mappings") or []):
        if not isinstance(it, dict):
            continue
        col = _norm(it.get("column", ""))
        if col not in real_set:
            continue  # 幻觉列名：不在真实表头 → 丢弃，不采信
        try:
            conf = float(it.get("confidence", 0) or 0)
        except (TypeError, ValueError):
            conf = 0.0
        phrase = str(it.get("phrase", "") or "")
        reason = str(it.get("reason", "") or "")
        if conf < MIN_CONFIDENCE:
            ambiguous.append({"phrase": phrase, "candidates": [col],
                              "needs_user_confirm": True, "reason": reason})
            continue
        mappings.append({"phrase": phrase, "column": col,
                         "confidence": conf, "reason": reason})
    for it in (d.get("ambiguous") or []):
        if not isinstance(it, dict):
            continue
        cands = [_norm(c) for c in (it.get("candidates") or [])]
        cands = [c for c in cands if c in real_set]
        if not cands:
            continue
        ambiguous.append({"phrase": str(it.get("phrase", "") or ""),
                          "candidates": cands, "needs_user_confirm": True,
                          "reason": str(it.get("reason", "") or "")})
    return {"mappings": mappings, "ambiguous": ambiguous}


__all__ = ["resolve_columns", "MIN_CONFIDENCE"]

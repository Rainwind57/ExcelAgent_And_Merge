"""最小清单#1 + P4：StepTrace 统一账本 + 慢因归因（纯函数，0 LLM，观测只读）。

背景（docs §"先做可观测性，不靠猜" + §P4"Step4 输出可行动诊断"）：candidate/
schema/prompt/LLM/执行指标散落各 Step 的 metrics。本模块把它们聚合成一份统一
trace，并做**确定性慢因归因**（候选过多 / schema 过长 / LLM 超时 / 执行慢），供
Step4 面向开发者输出"本次慢在哪"。

纯函数：只读入 step metrics + llm 快照，产汇总 dict，不改任何执行结果、不发 LLM。
阈值可由入参覆盖（默认经验值），便于确定性单测。
"""
from __future__ import annotations

from typing import Any, Optional

__all__ = ["build_step_trace", "DEFAULT_THRESHOLDS"]

# 慢因归因阈值（经验默认，可由 build_step_trace(thresholds=...) 覆盖）
DEFAULT_THRESHOLDS = {
    "prompt_chars_large": 20000,   # 单次请求 prompt 体量过大（schema 过长）
    "candidate_large": 8,          # 候选表进入 prompt 过多
    "slow_step_ms": 8000,          # 单步耗时视为"慢"的下限
}


def _num(d: dict, *keys: str) -> int:
    """从 dict 取第一个存在的数值键，缺失返 0。"""
    for k in keys:
        v = (d or {}).get(k)
        if isinstance(v, (int, float)):
            return int(v)
    return 0


def build_step_trace(
    step_metrics: dict[str, dict],
    llm_stats: Optional[dict] = None,
    *,
    thresholds: Optional[dict] = None,
) -> dict:
    """聚合各 Step metrics + LLM 快照 → 统一 trace + 慢因归因。

    Args:
        step_metrics: {step_id: metrics_dict}（各 SubAgent 的 StepResult.metrics）。
        llm_stats: llm_counter.as_dict() 快照（total_dur_ms/total_prompt_chars/
                   total_timeouts/total_errors/by_site...），可为空。
        thresholds: 覆盖 DEFAULT_THRESHOLDS 的阈值。

    Returns:
        {
          "steps": {step_id: {dur_ms, ...原样}},
          "totals": {dur_ms, llm_calls, prompt_chars, resp_chars, timeouts, errors,
                     candidate_count, schema_chars},
          "slow_cause": str,          # 主慢因标签
          "attribution": [ {cause, evidence} ],  # 全部命中的归因（可多条）
        }
    """
    th = dict(DEFAULT_THRESHOLDS)
    th.update(thresholds or {})
    sm = step_metrics or {}
    ls = llm_stats or {}

    # 各步耗时
    steps = {sid: dict(m or {}) for sid, m in sm.items()}
    total_step_ms = sum(_num(m, "dur_ms") for m in sm.values())

    # 候选 / schema 体量（Step1 metrics 常带 candidate_count/schema_chars）
    candidate_count = max(
        (_num(m, "candidate_count", "candidates") for m in sm.values()),
        default=0)
    schema_chars = max(
        (_num(m, "schema_chars", "prompt_chars") for m in sm.values()),
        default=0)

    llm_calls = _num(ls, "total_calls")
    prompt_chars = max(_num(ls, "total_prompt_chars"), schema_chars)
    resp_chars = _num(ls, "total_resp_chars")
    timeouts = _num(ls, "total_timeouts")
    errors = _num(ls, "total_errors")
    llm_dur_ms = _num(ls, "total_dur_ms")

    totals = {
        "dur_ms": total_step_ms,
        "llm_calls": llm_calls,
        "llm_dur_ms": llm_dur_ms,
        "prompt_chars": prompt_chars,
        "resp_chars": resp_chars,
        "timeouts": timeouts,
        "errors": errors,
        "candidate_count": candidate_count,
        "schema_chars": schema_chars,
    }

    # ── 确定性慢因归因（可多条命中，slow_cause 取优先级最高的一条）──
    attribution: list[dict] = []
    if timeouts > 0:
        attribution.append({
            "cause": "llm_timeout",
            "evidence": f"LLM 超时 {timeouts} 次（errors={errors}）"})
    if prompt_chars >= th["prompt_chars_large"]:
        attribution.append({
            "cause": "schema_too_large",
            "evidence": f"prompt/schema 字符数 {prompt_chars} ≥ {th['prompt_chars_large']}"})
    if candidate_count >= th["candidate_large"]:
        attribution.append({
            "cause": "candidate_overflow",
            "evidence": f"候选表 {candidate_count} ≥ {th['candidate_large']}"})
    # 最慢步（排除已归 LLM/schema 的情况仍给出执行热点）
    if sm:
        slow_sid, slow_ms = max(
            ((sid, _num(m, "dur_ms")) for sid, m in sm.items()),
            key=lambda kv: kv[1], default=(None, 0))
        if slow_ms >= th["slow_step_ms"]:
            attribution.append({
                "cause": "slow_step",
                "evidence": f"{slow_sid} 耗时 {slow_ms}ms ≥ {th['slow_step_ms']}"})

    # 优先级：超时 > schema 过长 > 候选过多 > 慢步 > none
    _priority = ["llm_timeout", "schema_too_large", "candidate_overflow", "slow_step"]
    slow_cause = "none"
    for c in _priority:
        if any(a["cause"] == c for a in attribution):
            slow_cause = c
            break

    return {
        "steps": steps,
        "totals": totals,
        "slow_cause": slow_cause,
        "attribution": attribution,
    }

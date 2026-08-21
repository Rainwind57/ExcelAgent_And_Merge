"""Test2 验证：AI 校验发现主线意图遗漏时回退 parse_multi 重拆。

覆盖 `_apply_ai_intent_check`（agent.run 内 Step1 AI 校验段，抽成 helper 便于单测）。
场景：
  1. AI 通过 → 保持规则拆分
  2. AI 遗漏主线 + parse_multi 成功 → 用 parse_multi 结果取代
  3. AI 遗漏 + parse_multi 抛异常 → 保持规则拆分 + 记建议
  4. AI 遗漏 + parse_multi 返空 → 保持规则拆分 + 记建议
  5. AI 仅字段映射建议（无 missing）→ 保持 + 记建议
  6. AI 返 None → 保持
  7. ai_enhancer 为 None / intents<2 → 跳过校验
  8. ai_verify_intents 抛异常 → 保持（不崩）
"""
from __future__ import annotations

import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.nl_parser import NLIntent
from agent.agent import TableAgent


def _ns(**kw):
    """轻量 AgentResult-like：add_thinking 收集 + ai_suggestions 列表。"""
    ns = types.SimpleNamespace(
        ai_suggestions=[],
        thinking=[],
        add_thinking=lambda phase, detail: ns.thinking.append((phase, detail)),
    )
    return ns


def _agent_with(ai_enhancer=None, parser=None):
    """绑定 _apply_ai_intent_check 到轻量 agent（仅 _ai_enhancer + parser）。"""
    ag = types.SimpleNamespace(_ai_enhancer=ai_enhancer, parser=parser)
    ag._apply_ai_intent_check = TableAgent._apply_ai_intent_check.__get__(ag)
    return ag


def _intent(table_hint, action="add", fields=None):
    it = NLIntent(action=action, table_hint=table_hint, raw="test")
    it.extras = {"fields": fields or {}}
    return it


# ── 场景 1：AI 通过 → 保持规则拆分 ─────────────────────────────
def test_ai_ok_keeps_rule_split():
    rule = [_intent("ability"), _intent("spell")]
    ai = types.SimpleNamespace(
        ai_verify_intents=lambda text, summary: {"ok": True})
    ag = _agent_with(ai_enhancer=ai, parser=types.SimpleNamespace())
    res = _ns()
    out = ag._apply_ai_intent_check(list(rule), "x", res)
    assert out == rule
    assert any("通过" in d for _, d in res.thinking)


# ── 场景 2：AI 遗漏主线 + parse_multi 成功 → 取代 ───────────────
def test_ai_missing_falls_back_to_parse_multi():
    """规则拆分 5 条全错路由（无 item 表），AI 说遗漏道具主线 → parse_multi 重拆取代。"""
    rule = [_intent("ability"), _intent("assistant_level"),
            _intent("hero_level"), _intent("spell")]
    pm_intents = [_intent("item"), _intent("ability")]  # parse_multi 正确分解
    ai = types.SimpleNamespace(
        ai_verify_intents=lambda text, summary: {
            "ok": False, "missing": ["新增道具炎爆符"], "corrections": []})
    parser = types.SimpleNamespace(parse_multi=lambda text: pm_intents)
    ag = _agent_with(ai_enhancer=ai, parser=parser)
    res = _ns()
    out = ag._apply_ai_intent_check(list(rule), "先新增道具炎爆符再用它作消耗新增神通", res)
    assert out == pm_intents, "应被 parse_multi 结果取代"
    assert out[0].table_hint == "item"
    # missing 已被 parse_multi 补上，不应再进 ai_suggestions
    assert not any("建议补充" in d for _, d in res.thinking)
    assert res.ai_suggestions == []


# ── 场景 3：AI 遗漏 + parse_multi 抛异常 → 保持 + 记建议 ─────────
def test_ai_missing_parse_multi_raises_keeps_rule():
    rule = [_intent("ability"), _intent("spell")]
    ai = types.SimpleNamespace(
        ai_verify_intents=lambda text, summary: {
            "ok": False, "missing": ["道具主线"], "corrections": []})
    def _raise(text):
        raise RuntimeError("parse_multi boom")
    parser = types.SimpleNamespace(parse_multi=_raise)
    ag = _agent_with(ai_enhancer=ai, parser=parser)
    res = _ns()
    out = ag._apply_ai_intent_check(list(rule), "x", res)
    assert out == rule, "parse_multi 失败应保持规则拆分"
    assert any("道具主线" in s for s in res.ai_suggestions)


# ── 场景 4：AI 遗漏 + parse_multi 返空 → 保持 + 记建议 ──────────
def test_ai_missing_parse_multi_empty_keeps_rule():
    rule = [_intent("ability"), _intent("spell")]
    ai = types.SimpleNamespace(
        ai_verify_intents=lambda text, summary: {
            "ok": False, "missing": ["道具主线"], "corrections": []})
    parser = types.SimpleNamespace(parse_multi=lambda text: [])
    ag = _agent_with(ai_enhancer=ai, parser=parser)
    res = _ns()
    out = ag._apply_ai_intent_check(list(rule), "x", res)
    assert out == rule
    assert any("道具主线" in s for s in res.ai_suggestions)


# ── 场景 5：AI 仅字段映射建议（无 missing）→ 保持 + 记 ──────────
def test_ai_corrections_only_keeps_rule_and_records():
    rule = [_intent("ability"), _intent("spell")]
    ai = types.SimpleNamespace(
        ai_verify_intents=lambda text, summary: {
            "ok": False, "missing": [], "corrections": [{"idx": 0, "field": "消耗道具"}]})
    ag = _agent_with(ai_enhancer=ai, parser=types.SimpleNamespace())
    res = _ns()
    out = ag._apply_ai_intent_check(list(rule), "x", res)
    assert out == rule
    assert any("消耗道具" in s for s in res.ai_suggestions)
    assert any("字段映射" in d for _, d in res.thinking)


# ── 场景 6：AI 返 None → 保持 ──────────────────────────────────
def test_ai_returns_none_keeps_rule():
    rule = [_intent("ability"), _intent("spell")]
    ai = types.SimpleNamespace(ai_verify_intents=lambda text, summary: None)
    ag = _agent_with(ai_enhancer=ai, parser=types.SimpleNamespace())
    out = ag._apply_ai_intent_check(list(rule), "x", _ns())
    assert out == rule


# ── 场景 7：ai_enhancer 为 None / intents<2 → 跳过 ─────────────
def test_no_ai_enhancer_skips_check():
    rule = [_intent("ability"), _intent("spell")]
    ag = _agent_with(ai_enhancer=None, parser=types.SimpleNamespace())
    out = ag._apply_ai_intent_check(list(rule), "x", _ns())
    assert out == rule


def test_single_intent_skips_check():
    rule = [_intent("ability")]
    ai = types.SimpleNamespace(
        ai_verify_intents=lambda text, summary: {"ok": False, "missing": ["x"]})
    ag = _agent_with(ai_enhancer=ai, parser=types.SimpleNamespace())
    out = ag._apply_ai_intent_check(list(rule), "x", _ns())
    assert out == rule


# ── 场景 8：ai_verify_intents 抛异常 → 保持不崩 ─────────────────
def test_ai_verify_raises_keeps_rule_no_crash():
    rule = [_intent("ability"), _intent("spell")]
    def _raise(text, summary):
        raise RuntimeError("ai_verify boom")
    ai = types.SimpleNamespace(ai_verify_intents=_raise)
    ag = _agent_with(ai_enhancer=ai, parser=types.SimpleNamespace())
    out = ag._apply_ai_intent_check(list(rule), "x", _ns())
    assert out == rule


# ── 场景 9：parser 无 parse_multi 属性 → missing 走记建议路径 ──
def test_parser_without_parse_multi_records_missing():
    rule = [_intent("ability"), _intent("spell")]
    ai = types.SimpleNamespace(
        ai_verify_intents=lambda text, summary: {
            "ok": False, "missing": ["主线"], "corrections": []})
    # parser 无 parse_multi 方法
    ag = _agent_with(ai_enhancer=ai, parser=types.SimpleNamespace())
    res = _ns()
    out = ag._apply_ai_intent_check(list(rule), "x", res)
    assert out == rule
    assert any("主线" in s for s in res.ai_suggestions)


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

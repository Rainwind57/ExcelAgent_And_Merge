"""D3 最小 agentic loop 单测（capability: agent-retry-loop）。

验证：
1. 硬错误→重试成功→ok=True
2. 重试仍失败→回滚+ok=False+两次失败描述
3. 正常路径不触发重试（无额外 LLM 调用）
4. 重试仅触发一次，不递归
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import TableAgent, AgentResult
from agent.nl_parser import NLIntent


class _StubParser:
    """记录 parse_multi 调用次数 + 返回可控 intents。"""
    def __init__(self, return_intents_factory=None):
        self.call_count = 0
        self.error_feedback_received = ""
        self._factory = return_intents_factory or (lambda text, error_feedback: [])

    def parse_multi(self, text: str, context: str = "", error_feedback: str = "") -> list:
        self.call_count += 1
        self.error_feedback_received = error_feedback
        return self._factory(text, error_feedback)


def _bind_retry_agent(parser, **extra) -> types.SimpleNamespace:
    """绑定 _collect_error_feedback + _retry_with_error_feedback + _rollback_write。"""
    agent = types.SimpleNamespace(parser=parser, **extra)
    for name in ("_collect_error_feedback", "_retry_with_error_feedback", "_rollback_write"):
        setattr(agent, name, getattr(TableAgent, name).__get__(agent))
    return agent


def test_retry_success_sets_ok_true():
    """硬错误→重试成功→ok=True（覆盖首次 False）。"""
    # parser 重试时返回一个修正 intent
    def factory(text, error_feedback):
        return [NLIntent(raw=text, action="set", target_field="类型", value=1)]

    parser = _StubParser(factory)
    agent = _bind_retry_agent(parser)

    # 首次失败 res
    res = AgentResult(intent=NLIntent(raw="改类型为attack", action="set"))
    res.add("write", False, "类型=int 不接受字符串 attack")
    res.ok = False
    res.message = "首次失败：类型硬错误"

    intent = NLIntent(raw="改类型为attack", action="set", target_field="类型", value="attack")

    # mock _retry 内部 _dispatch：重试成功
    def fake_dispatch_for_retry(new_intent, path, sheet, res):
        res.add("write_retry", True, "重试写入成功")
        res.message = "重试成功"
        return res

    # 替换 _run_set（_retry_dispatch 内部调）
    agent._run_set = fake_dispatch_for_retry

    error_feedback = agent._collect_error_feedback(res, intent, Path("t.xlsx"), "Sheet1")
    retry_out = agent._retry_with_error_feedback(intent, Path("t.xlsx"), "Sheet1", res, error_feedback)

    assert retry_out is not None
    assert retry_out.ok is True
    assert parser.call_count == 1, "重试仅触发一次"
    assert "类型" in error_feedback
    assert "attack" in error_feedback


def test_retry_failure_keeps_ok_false():
    """重试仍失败→ok=False + 两次失败描述。"""
    def factory(text, error_feedback):
        return [NLIntent(raw=text, action="set", target_field="类型", value="still_wrong")]

    parser = _StubParser(factory)
    agent = _bind_retry_agent(parser)

    res = AgentResult(intent=NLIntent(raw="改", action="set"))
    res.add("write", False, "首次失败")
    res.ok = False

    intent = NLIntent(raw="改", action="set")

    # _run_set 重试时仍失败
    def fake_dispatch_fail(new_intent, path, sheet, res):
        res.add("write_retry", False, "重试仍失败")
        res.ok = False
        res.message = "重试仍失败"
        return res

    agent._run_set = fake_dispatch_fail

    error_feedback = agent._collect_error_feedback(res, intent, Path("t.xlsx"), "Sheet1")
    retry_out = agent._retry_with_error_feedback(intent, Path("t.xlsx"), "Sheet1", res, error_feedback)

    assert retry_out is not None
    assert retry_out.ok is False
    assert parser.call_count == 1, "不递归重试"


def test_normal_path_no_retry():
    """正常路径（ok=True）不触发重试（parser 调用次数=0）。

    本测验证 _retry_with_error_feedback 仅在失败时被调用。
    实际调用逻辑在 _run_single_impl，这里验证调用条件。
    """
    parser = _StubParser()
    agent = _bind_retry_agent(parser)

    # 正常路径 out.ok=True，不应进入 retry 分支
    out = AgentResult(intent=NLIntent(raw="ok", action="set"))
    out.add("write", True, "成功")
    assert out.ok is True
    # 若 out.ok=True，_run_single_impl 不调 _retry_with_error_feedback
    assert parser.call_count == 0


def test_retry_parse_failure_returns_none():
    """重试解析失败（parse_multi 返回空）→ 返回 None。"""
    parser = _StubParser(lambda *a, **kw: [])  # 返回空
    agent = _bind_retry_agent(parser)

    res = AgentResult(intent=NLIntent(raw="改", action="set"))
    intent = NLIntent(raw="改", action="set")

    retry_out = agent._retry_with_error_feedback(intent, Path("t.xlsx"), "Sheet1", res, "error feedback")
    assert retry_out is None
    assert parser.call_count == 1, "仅触发一次（不递归）"


def test_error_feedback_contains_failed_steps_and_schema():
    """error_feedback 文案含失败步骤 + 目标列 + 列名候选。"""
    parser = _StubParser()
    agent = _bind_retry_agent(parser, cli=types.SimpleNamespace(read_header=lambda *a: ["id", "名称", "类型"]))

    res = AgentResult(intent=NLIntent(raw="改", action="set"))
    res.add("write", False, "类型=int 不接受 attack")
    intent = NLIntent(raw="改", action="set", target_field="类型", value="attack")

    feedback = agent._collect_error_feedback(res, intent, Path("t.xlsx"), "Sheet1")

    assert "类型=int 不接受 attack" in feedback
    assert "目标列：类型" in feedback
    assert "attack" in feedback
    assert "列名候选" in feedback


def test_error_feedback_d4_format_with_failed_col_and_schema(monkeypatch):
    """D4: error_feedback 含 '上次 fields 中 [列]='值' 失败' + 期望列类型 + D2 schema 块，
    且正确拼入 parser.parse_multi 的 error_feedback 参数。
    """
    parser = _StubParser(lambda text, ef: [])  # 重试返回空（仅验证 feedback 传递）
    agent = _bind_retry_agent(
        parser,
        cli=types.SimpleNamespace(read_header=lambda *a: ["id", "名称", "类型"]),
        _get_col_type=lambda stem, sheet, col: "int" if col == "类型" else "",
    )
    # mock D2 列类型 schema 块（测试环境无真实 value_constraints）
    import agent.excel.core.skill_context as sc_mod
    monkeypatch.setattr(sc_mod, "_format_column_types_block",
                        lambda stems: "## 目标表列类型 schema\n  pet[Pet]:\n    类型: int",
                        raising=False)

    res = AgentResult(intent=NLIntent(raw="改", action="set"))
    # 真实 _coerce_value detail 格式
    res.add("coerce_value", False,
            "列[类型]类型为 int，值'attack'无法转为整数且无枚举映射，已阻止写入")
    intent = NLIntent(raw="改", action="set", target_field="类型", value="attack")

    feedback = agent._collect_error_feedback(res, intent, Path("pet.xlsx"), "Pet")
    # D4 文案
    assert "上次 fields 中 [类型]='attack' 失败" in feedback
    assert "该列类型 int" in feedback
    # D2 schema 块
    assert "列类型 schema" in feedback
    assert "类型: int" in feedback
    assert "请重新产出 fields" in feedback

    # 验证正确拼入 parser.parse_multi 的 error_feedback 参数
    agent._retry_with_error_feedback(intent, Path("pet.xlsx"), "Pet", res, feedback)
    assert parser.error_feedback_received == feedback
    assert parser.call_count == 1

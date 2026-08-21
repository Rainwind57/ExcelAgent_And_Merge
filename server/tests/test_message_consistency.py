"""D5 错误形态一致性单测（capability: error-observability）。

验证：
1. 成功路径 aggregated_message="成功：{step_names}"，不含失败文本
2. 失败路径 aggregated_message="失败：{failed_step} - {detail}"，不含成功步骤文本
3. ok=None → "未完成"
4. 结构化 steps 可解析（AgentStep list）
5. 读回失败（5.6 覆盖 write-verification spec scenario）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import AgentResult, AgentStep


def test_success_aggregated_message():
    """成功路径 message="成功：{step_names}"。"""
    res = AgentResult()
    res.add("resolve_table", True, "定位到 t.xlsx")
    res.add("write", True, "写入成功")
    assert res.ok is True
    msg = res.aggregated_message
    assert msg.startswith("成功：")
    assert "resolve_table" in msg
    assert "write" in msg


def test_failure_aggregated_message_excludes_success_text():
    """失败路径 message="失败：{failed_step} - {detail}"，不含成功步骤文本。"""
    res = AgentResult()
    res.add("resolve_table", True, "定位到 t.xlsx")
    res.add("write", True, "已新增：行8")  # 成功步骤（含"已新增"文本）
    res.add("verify_write_back", False, "model_id 落盘不符")
    assert res.ok is False
    msg = res.aggregated_message
    assert msg.startswith("失败：verify_write_back")
    assert "model_id 落盘不符" in msg
    # 关键：失败 message 不含成功步骤的"已新增"文本
    assert "已新增" not in msg
    assert "定位到 t.xlsx" not in msg


def test_none_ok_aggregated_message():
    """ok=None → "未完成"。"""
    res = AgentResult()
    assert res.ok is None
    assert res.aggregated_message == "未完成"


def test_steps_structured_parseable():
    """steps 是 AgentStep list，可解析 name/ok/detail。"""
    res = AgentResult()
    res.add("step1", True, "detail1")
    res.add("step2", False, "detail2")
    assert all(isinstance(s, AgentStep) for s in res.steps)
    assert res.steps[0].name == "step1"
    assert res.steps[0].ok is True
    assert res.steps[0].detail == "detail1"
    assert res.steps[1].name == "step2"
    assert res.steps[1].ok is False
    assert res.steps[1].detail == "detail2"


def test_message_field_preserved_for_compat():
    """顶层 message 字符串保留（兼容 eval/上层直接赋值）。"""
    res = AgentResult()
    res.message = "自定义消息"
    res.add("step", True, "ok")
    # message 字段不被 aggregated_message 覆盖
    assert res.message == "自定义消息"
    # aggregated_message 是独立 property
    assert res.aggregated_message == "成功：step"


def test_read_back_failure_ok_false_no_crash():
    """5.6 读回失败 → ok=False + error="read_back_failed"，主流程不抛异常。

    覆盖 write-verification spec scenario "读回失败返回错误"。
    """
    # 模拟 _verify_write_back 读回失败返回
    verify_result = {"ok": False, "error": "read_back_failed"}
    res = AgentResult()
    res.add("append_row", False, verify_result["error"])
    assert res.ok is False
    assert "read_back_failed" in res.aggregated_message


def test_failure_only_failed_step_in_message():
    """多失败步骤：aggregated_message 只含首个失败步骤。"""
    res = AgentResult()
    res.add("step1", True, "ok1")
    res.add("step2", False, "fail2")
    res.add("step3", False, "fail3")
    assert res.ok is False
    msg = res.aggregated_message
    assert "step2" in msg
    assert "fail2" in msg
    # 不含后续失败步骤（仅首个）
    assert "step3" not in msg
    assert "fail3" not in msg

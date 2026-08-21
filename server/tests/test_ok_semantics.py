"""D2 ok 语义重构单测（capability: write-verification）。

验证 AgentResult.ok 语义基于写后验证真值：
1. 初始 ok=None（未知）
2. 写步骤 add(True) → ok=True（首步成功确立基线）
3. 任一写步骤 add(False) → ok=False
4. 无写步骤的查询操作 → 构造时 ok=True
5. add 驱动：先 True 后 False → ok=False；先 False 后 True → 仍 False（不覆盖失败）
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import AgentResult


def test_initial_ok_is_none():
    """ok 初始为 None（未知）。"""
    res = AgentResult()
    assert res.ok is None


def test_first_success_step_sets_true():
    """首个成功步骤 → ok=True。"""
    res = AgentResult()
    res.add("step1", True, "成功")
    assert res.ok is True


def test_failure_step_sets_false():
    """失败步骤 → ok=False。"""
    res = AgentResult()
    res.add("step1", False, "失败")
    assert res.ok is False


def test_success_then_failure_sets_false():
    """先成功后失败 → ok=False。"""
    res = AgentResult()
    res.add("step1", True, "成功")
    assert res.ok is True
    res.add("step2", False, "失败")
    assert res.ok is False


def test_failure_then_success_keeps_false():
    """先失败后成功 → ok 仍 False（不覆盖失败）。"""
    res = AgentResult()
    res.add("step1", False, "失败")
    assert res.ok is False
    res.add("step2", True, "成功")
    assert res.ok is False  # 不被成功覆盖


def test_query_op_ok_true():
    """查询操作（get/col）构造时 ok=True（业务成功）。"""
    from agent.nl_parser import NLIntent
    intent = NLIntent(action="get", raw="查询")
    res = AgentResult(ok=True, intent=intent)
    assert res.ok is True


def test_explicit_ok_false_preserved():
    """显式 ok=False 构造保留（失败路径）。"""
    from agent.nl_parser import NLIntent
    intent = NLIntent(action="delete", raw="失败")
    res = AgentResult(ok=False, intent=intent)
    assert res.ok is False

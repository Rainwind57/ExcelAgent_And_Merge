"""repair_context 单测：累积历史、重复策略检测、占位符解析。

capability: verify-repair-loop
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.repair.error_classifier import ErrorType
from agent.excel.repair.repair_context import RepairContext


def test_record_attempt_accumulates():
    ctx = RepairContext()
    ctx.record_attempt(ErrorType.TYPE_MISMATCH, "type_coerce", failed_col="等级", detail="首轮")
    ctx.record_attempt(ErrorType.COLUMN_NOT_FOUND, "column_candidate_remap", detail="二轮")
    assert ctx.attempts == 2
    assert len(ctx.failed_ops) == 2
    assert ctx.failed_ops[0].attempt == 1
    assert ctx.error_type_history == [ErrorType.TYPE_MISMATCH, ErrorType.COLUMN_NOT_FOUND]


def test_is_repeat_strategy_detects_dup():
    ctx = RepairContext()
    ctx.record_attempt(ErrorType.TYPE_MISMATCH, "type_coerce")
    assert ctx.is_repeat_strategy(ErrorType.TYPE_MISMATCH, "type_coerce") is True
    assert ctx.is_repeat_strategy(ErrorType.TYPE_MISMATCH, "other") is False
    assert ctx.is_repeat_strategy(ErrorType.ID_CONFLICT, "type_coerce") is False


def test_last_error_type():
    ctx = RepairContext()
    assert ctx.last_error_type() is None
    ctx.record_attempt(ErrorType.ID_CONFLICT, "id_reallocate")
    assert ctx.last_error_type() is ErrorType.ID_CONFLICT


def test_resolve_placeholder():
    ctx = RepairContext()
    ctx.resolve_placeholder("<new_id>", 1001)
    assert ctx.resolved_placeholders["<new_id>"] == 1001


def test_set_final_failure_and_summary():
    ctx = RepairContext()
    ctx.record_attempt(ErrorType.TYPE_MISMATCH, "type_coerce")
    ctx.record_attempt(ErrorType.COLUMN_NOT_FOUND, "column_candidate_remap")
    ctx.set_final_failure({"root_cause": "达上限"})
    assert ctx.final_failure["root_cause"] == "达上限"
    summary = ctx.summarized_strategies()
    assert len(summary) == 2
    assert "轮1:type_mismatch/type_coerce" in summary[0]

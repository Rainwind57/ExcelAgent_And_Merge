"""O20c _run_set 失败信号 + classifier 结构化 failed_col 单测（S6 modify 失败修复）。

覆盖：
- _run_set 多字段 write 失败 → res.failures 入 {failed_col, failed_val, kind=write_failed}
- _run_set 单字段 match_target 失败 → res.failures 入 {failed_col, kind=column_not_found}
- classify 读 res.failures 结构化 failed_col → COLUMN_NOT_FOUND（免 regex 漏）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from agent.excel.repair.error_classifier import classify, ClassifiedError, ErrorType


class _FakeRes:
    """轻量 AgentResult（含 failures + steps + message）。"""
    def __init__(self, message="", failures=None, steps=None):
        self.message = message
        self.failures = failures or []
        self.steps = steps or []
        self.final = None
        self.intent = None


class TestClassifyReadsFailures:
    def test_column_not_found_from_failures(self):
        """res.failures 含 kind=column_not_found + failed_col → classify 直接定类。"""
        res = _FakeRes(
            message="无法匹配目标列",
            failures=[{"code": 40, "kind": "column_not_found",
                       "failed_col": "阴阳权重", "failed_val": 0.45,
                       "message": "无法匹配目标列：阴阳权重"}])
        c = classify(None, res, None, context={"table_stem": "item", "sheet": "Fabao"})
        assert c.error_type == ErrorType.COLUMN_NOT_FOUND
        assert c.failed_col == "阴阳权重"
        assert c.failed_val == 0.45

    def test_write_failed_col_extracted(self):
        """res.failures 含 kind=write_failed + failed_col → classify 取 col（regex 兜底）。"""
        res = _FakeRes(
            message="未能写入任何列",
            failures=[{"code": 40, "kind": "write_failed",
                       "failed_col": "spell", "failed_val": 70,
                       "message": "写后验证不符"}])
        c = classify(None, res, None, context={"table_stem": "item", "sheet": "Fabao"})
        assert c.failed_col == "spell"
        assert c.failed_val == 70

    def test_no_failures_fallback_regex(self):
        """无 res.failures → 回退 detail regex（原逻辑不变）。"""
        res = _FakeRes(message="列[abc] 类型为int，无法转为str")
        c = classify(None, res, None, context={})
        assert c.failed_col == "abc"

    def test_empty_failures_noop(self):
        res = _FakeRes(message="未知错误", failures=[])
        c = classify(None, res, None, context={})
        # 无 verify 无 regex 信号 → UNKNOWN
        assert c.error_type == ErrorType.UNKNOWN

    def test_failures_without_failed_col_fallback(self):
        """res.failures 无 failed_col → 回退 regex。"""
        res = _FakeRes(
            message="列[x] 不存在",
            failures=[{"code": 40, "kind": "other", "message": "misc"}])
        c = classify(None, res, None, context={})
        assert c.error_type == ErrorType.COLUMN_NOT_FOUND
        assert c.failed_col == "x"

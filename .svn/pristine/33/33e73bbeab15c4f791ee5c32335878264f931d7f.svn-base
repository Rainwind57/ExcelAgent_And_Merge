"""verify_repair_loop 单测（§3.2 最小版：check_type_constraint 纯函数抽离）。

§3.2 完整版（_run_verify_repair_loop 循环主体抽文件）待后续大重构（464 行 + 8 helper）。
本测聚焦最小版抽离的 check_type_constraint 纯函数 + agent 薄转发。

运行: python -m pytest server/tests/test_verify_repair_loop.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.repair.verify_repair_loop import check_type_constraint


class TestCheckTypeConstraint:
    def test_int_ok(self):
        assert check_type_constraint("int", 1) == (True, "")
        assert check_type_constraint("int", "1") == (True, "")
        assert check_type_constraint("integer", 1.0) == (True, "")  # int(float("1.0"))

    def test_int_fail(self):
        ok, err = check_type_constraint("int", "abc")
        assert ok is False
        assert "int" in err

    def test_float_ok(self):
        assert check_type_constraint("float", 1.5) == (True, "")
        assert check_type_constraint("double", "1.5") == (True, "")
        assert check_type_constraint("number", "2.0") == (True, "")

    def test_float_fail(self):
        ok, err = check_type_constraint("float", "xyz")
        assert ok is False
        assert "float" in err

    def test_bool_ok(self):
        assert check_type_constraint("bool", True) == (True, "")
        assert check_type_constraint("bool", "true") == (True, "")
        assert check_type_constraint("bool", "1") == (True, "")
        assert check_type_constraint("bool", "是") == (True, "")

    def test_bool_fail(self):
        ok, err = check_type_constraint("bool", "maybe")
        assert ok is False
        assert "bool" in err

    def test_bool_isinstance(self):
        """Python bool 实例直接通过（isinstance(value, bool)）。"""
        assert check_type_constraint("bool", False) == (True, "")

    def test_unknown_type_passes(self):
        """未知类型（如 string）默认通过（轻量校验,非类型系统）。"""
        assert check_type_constraint("string", "anything") == (True, "")
        assert check_type_constraint("", "x") == (True, "")
        assert check_type_constraint("date", "2026-01-01") == (True, "")

    def test_case_insensitive(self):
        """类型名大小写不敏感。"""
        assert check_type_constraint("INT", 1) == (True, "")
        assert check_type_constraint("Float", "1.5") == (True, "")
        assert check_type_constraint("BOOL", "true") == (True, "")

    def test_none_col_type_passes(self):
        """col_type None → 默认通过（无约束）。"""
        assert check_type_constraint(None, "x") == (True, "")


class TestAgentForward:
    """agent._check_type_constraint 薄转发到 verify_repair_loop（§3.2）。"""

    def test_agent_method_exists(self):
        from agent.excel.core.agent import TableAgent
        assert hasattr(TableAgent, "_check_type_constraint")

    def test_agent_forward_uses_module(self):
        """agent._check_type_constraint 薄转发到 verify_repair_loop.check_type_constraint。"""
        from agent.excel.core.agent import TableAgent
        ag = object.__new__(TableAgent)
        # 薄转发：调 verify_repair_loop.check_type_constraint
        ok, err = ag._check_type_constraint("int", 1)
        assert ok is True
        ok, err = ag._check_type_constraint("float", "xyz")
        assert ok is False
        assert "float" in err

    def test_agent_forward_consistent_with_module(self):
        """agent 薄转发与模块函数返回一致。"""
        from agent.excel.core.agent import TableAgent
        ag = object.__new__(TableAgent)
        for col_type, value in [("int", 1), ("float", "abc"), ("bool", "maybe")]:
            assert ag._check_type_constraint(col_type, value) == \
                check_type_constraint(col_type, value)


# ── §3.2 完整版待后续（文档化）──────────────────────────────


class TestVerifyRepairLoopFullExtractPending:
    """§3.2 完整版：_run_verify_repair_loop 循环主体抽文件待后续大重构。

    现状：_run_verify_repair_loop（agent.py:6109-6372, 464 行）+ 8 helper 散落 agent.py。
    execute_no_llm=1（§3.1）已跳 verify-repair LLM,§3.2 抽文件为代码组织优化,无功能影响。

    完整版需：
      - 抽 _run_verify_repair_loop 主体到 run_verify_repair_loop 函数
      - 8 helper 经 agent 回调传入（_apply_repair_fix/_llm_call/_run_react_repair/
        _llm_diagnose_only/_record_repair_signal/_safe_redispatch/_rollback_write/_verify_write）
      - agent._verify_write 薄转发避免改 _phase_execute 多处调用点
    """

    def test_module_docstring_documents_pending(self):
        """模块 docstring 记录完整版待后续。"""
        from agent.excel.repair import verify_repair_loop
        doc = verify_repair_loop.__doc__ or ""
        assert "run_verify_repair_loop" in doc or "循环主体" in doc
        assert "待后续" in doc or "大重构" in doc

"""TableAgent P19 单测（OPTIMIZATION_LEDGER §4 第二批）。

覆盖 P19：`CODEMAKER_EXECUTE_NO_LLM=1` × `enable_verify_repair_loop=True`
互斥校验。前者=1 时 _phase_execute 失败路径早返跳 verify_repair
（agent.py:5608），后者默认 True → 失败路径零修复（配置陷阱，低频但
行为不确定）。__init__ 加 warning 提示（不强制改，保用户显式意图）。

运行: python -m pytest server/tests/test_agent_p19_mutex.py -v
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.agent import TableAgent


def _make_agent(**kwargs):
    """轻量 TableAgent（绕过重 __init__ 依赖,只测 P19 互斥逻辑）。"""
    ag = object.__new__(TableAgent)
    _env_loop = os.environ.get("CODEMAKER_VERIFY_REPAIR_LOOP", "").strip()
    enable_vr = kwargs.get("enable_verify_repair_loop", True)
    if _env_loop in ("0", "false", "False", "off"):
        enable_vr = False
    ag.enable_verify_repair_loop = enable_vr
    _no_llm_env = kwargs.get("execute_no_llm_env",
                             os.getenv("CODEMAKER_EXECUTE_NO_LLM", "0"))
    ag.execute_no_llm = _no_llm_env != "0"
    return ag


class TestP19Mutex:
    def test_both_on_emits_warning(self, monkeypatch, caplog):
        """EXECUTE_NO_LLM=1 + verify_repair=True → warning。"""
        monkeypatch.setenv("CODEMAKER_EXECUTE_NO_LLM", "1")
        monkeypatch.delenv("CODEMAKER_VERIFY_REPAIR_LOOP", raising=False)
        ag = _make_agent()
        with caplog.at_level(logging.WARNING, logger="agent.excel.core.agent"):
            ag._check_p19_mutex_conflict()
        assert ag.execute_no_llm is True
        assert ag.enable_verify_repair_loop is True
        assert any("P19" in r.message for r in caplog.records), \
            "应发 P19 互斥 warning"

    def test_only_no_llm_no_warning(self, monkeypatch, caplog):
        """EXECUTE_NO_LLM=1 + verify_repair=False → 无 warning。"""
        monkeypatch.setenv("CODEMAKER_EXECUTE_NO_LLM", "1")
        monkeypatch.setenv("CODEMAKER_VERIFY_REPAIR_LOOP", "0")
        ag = _make_agent()
        with caplog.at_level(logging.WARNING, logger="agent.excel.core.agent"):
            ag._check_p19_mutex_conflict()
        assert ag.execute_no_llm is True
        assert ag.enable_verify_repair_loop is False
        assert not any("P19" in r.message for r in caplog.records)

    def test_only_verify_repair_no_warning(self, monkeypatch, caplog):
        """EXECUTE_NO_LLM=0 + verify_repair=True → 无 warning（默认配置）。"""
        monkeypatch.setenv("CODEMAKER_EXECUTE_NO_LLM", "0")
        monkeypatch.delenv("CODEMAKER_VERIFY_REPAIR_LOOP", raising=False)
        ag = _make_agent()
        with caplog.at_level(logging.WARNING, logger="agent.excel.core.agent"):
            ag._check_p19_mutex_conflict()
        assert ag.execute_no_llm is False
        assert ag.enable_verify_repair_loop is True
        assert not any("P19" in r.message for r in caplog.records)

    def test_both_off_no_warning(self, monkeypatch, caplog):
        """EXECUTE_NO_LLM=0 + verify_repair=False → 无 warning。"""
        monkeypatch.setenv("CODEMAKER_EXECUTE_NO_LLM", "0")
        monkeypatch.setenv("CODEMAKER_VERIFY_REPAIR_LOOP", "0")
        ag = _make_agent()
        with caplog.at_level(logging.WARNING, logger="agent.excel.core.agent"):
            ag._check_p19_mutex_conflict()
        assert not any("P19" in r.message for r in caplog.records)

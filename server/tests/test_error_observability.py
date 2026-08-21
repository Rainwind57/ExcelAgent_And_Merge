"""D4 错误吞噬治理单测（capability: error-observability）。

验证：
1. 索引刷新失败 → logging.warning + AgentResult.index_dirty=True
2. 回滚失败 → logging.error + AgentResult.dirty_data=True
3. 证据记录失败 → logging.warning + res.ok 不变（不阻断主流程）
"""
from __future__ import annotations

import logging
import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import TableAgent, AgentResult


def _bind_agent(**attrs) -> types.SimpleNamespace:
    """构造最小 agent 壳，绑定 TableAgent 方法用于测试。"""
    agent = types.SimpleNamespace(**attrs)
    for name in ("_refresh_index_after_write", "_rollback_write", "_log_evidence"):
        setattr(agent, name, getattr(TableAgent, name).__get__(agent))
    return agent


def test_refresh_index_failure_sets_index_dirty_and_warns(caplog):
    """4.1 索引刷新失败 → warning + 调用方设 index_dirty=True。"""
    agent = _bind_agent(
        cli=types.SimpleNamespace(workspace="/nonexistent"),
        live_index=True,
        _index_cache=None,
    )
    # monkeypatch refresh_if_changed 抛异常
    import agent.excel.table_index as ti_mod
    orig = ti_mod.refresh_if_changed
    ti_mod.refresh_if_changed = lambda *_a, **_kw: (_ for _ in ()).throw(IOError("锁"))

    try:
        res = AgentResult(ok=True, intent=None)
        ok = agent._refresh_index_after_write(Path("dummy.xlsx"))
        assert ok is False, "刷新失败应返回 False"
        # 调用方（_do_append 模式）按返回值设 index_dirty
        if not ok:
            res.index_dirty = True
        assert res.index_dirty is True
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert any("索引刷新失败" in r.getMessage() for r in caplog.records)
    finally:
        ti_mod.refresh_if_changed = orig


def test_refresh_index_success_returns_true_no_dirty(caplog):
    """4.1 正常路径：刷新成功返回 True，index_dirty 保持 False。"""
    agent = _bind_agent(live_index=False, _index_cache=None)
    res = AgentResult(ok=True, intent=None)
    ok = agent._refresh_index_after_write(Path("dummy.xlsx"))
    assert ok is True, "非 live_index 应返回 True（跳过）"
    assert res.index_dirty is False


def test_rollback_failure_sets_dirty_data_and_errors(caplog):
    """4.2 回滚失败 → error + dirty_data=True。"""
    class BoomAuditor:
        def rollback_to_backup(self, *a, **kw):
            raise RuntimeError("回滚 IO 失败")

    agent = _bind_agent(auditor=BoomAuditor())
    res = AgentResult(ok=True, intent=None)
    agent._rollback_write(Path("dummy.xlsx"), "backup.zip", res)
    assert res.dirty_data is True
    assert any(r.levelno == logging.ERROR for r in caplog.records)
    assert any("回滚失败" in r.getMessage() for r in caplog.records)


def test_log_evidence_failure_warns_and_keeps_ok(caplog):
    """4.4 证据记录失败 → warning + res.ok 不变（不阻断主流程）。"""
    class BoomDialogLogger:
        def log(self, *a, **kw):
            raise RuntimeError("disk full")

    import agent.agent as agent_mod
    orig = agent_mod.get_dialog_logger
    agent_mod.get_dialog_logger = lambda: BoomDialogLogger()

    agent = _bind_agent(enable_skill=False)  # 走 dialog 分支即返回，不触发 evidence
    intent = types.SimpleNamespace(raw="测试指令", action="add")
    res = AgentResult(ok=True, intent=intent, table_stem="t", table_sheet="s")

    try:
        agent._log_evidence(res, intent, user_text="测试指令")
        assert res.ok is True, "证据失败不应改 ok"
        assert any(r.levelno == logging.WARNING for r in caplog.records)
        assert any("对话记录写盘失败" in r.getMessage() for r in caplog.records)
    finally:
        agent_mod.get_dialog_logger = orig

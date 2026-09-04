"""repair 信号回流学习单测（capability: error-classification-repair）。

验证：
1. skill_updater._ingest_anti_pattern_signal 接受显式 signal_type 并写 jsonl
2. agent._record_repair_signal 把 ErrorType 映射到 frequent_* 信号类型
3. 高置信度且非重复时不记录（避免噪声）
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import TableAgent
from agent.excel.repair.error_classifier import ClassifiedError, ErrorType
from agent.excel.skill_updater import SkillUpdater


def test_skill_updater_ingest_explicit_signal_type(tmp_path):
    """显式 signal_type → 写入 anti_pattern_signals.jsonl。"""
    su = SkillUpdater(skills_dir=tmp_path)
    su._ingest_anti_pattern_signal({
        "signal_type": "frequent_type_mismatch",
        "table_stem": "pet", "sheet": "Pet",
        "col": {"resolved": "等级"},
        "intent_action": "add",
        "reason": "int 列写字符串",
    })
    import json as _json
    sigs = []
    if su.signals_path.exists():
        for line in su.signals_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                sigs.append(_json.loads(line))
    assert any(s.get("type") == "frequent_type_mismatch" and s.get("table_stem") == "pet"
               and s.get("column") == "等级" for s in sigs)


def test_record_repair_signal_maps_type_mismatch(monkeypatch):
    """type_mismatch + 低置信度 → 记录 frequent_type_mismatch。"""
    calls = []

    class _Rec:
        def _ingest_anti_pattern_signal(self, record):
            calls.append(record)

    import agent.agent as agent_mod
    monkeypatch.setattr(agent_mod, "get_skill_updater", lambda: _Rec())

    agent = types.SimpleNamespace(enable_skill=True)
    agent._record_repair_signal = getattr(TableAgent, "_record_repair_signal").__get__(agent)
    err = ClassifiedError(error_type=ErrorType.TYPE_MISMATCH, confidence=0.3,
                          failed_col="等级", root_cause="类型不符")
    agent._record_repair_signal(err, "pet", "Pet", is_repeat=False)
    assert len(calls) == 1
    assert calls[0]["signal_type"] == "frequent_type_mismatch"
    assert calls[0]["table_stem"] == "pet"


def test_record_repair_signal_high_conf_no_repeat_skips(monkeypatch):
    """高置信度 + 非重复 → 不记录。"""
    calls = []

    class _Rec:
        def _ingest_anti_pattern_signal(self, record):
            calls.append(record)

    import agent.agent as agent_mod
    monkeypatch.setattr(agent_mod, "get_skill_updater", lambda: _Rec())

    agent = types.SimpleNamespace(enable_skill=True)
    agent._record_repair_signal = getattr(TableAgent, "_record_repair_signal").__get__(agent)
    err = ClassifiedError(error_type=ErrorType.TYPE_MISMATCH, confidence=0.9, failed_col="等级")
    agent._record_repair_signal(err, "pet", "Pet", is_repeat=False)
    assert calls == []


def test_record_repair_signal_repeat_records_even_high_conf(monkeypatch):
    """重复出现时即使高置信度也记录（捕获重复失败模式）。"""
    calls = []

    class _Rec:
        def _ingest_anti_pattern_signal(self, record):
            calls.append(record)

    import agent.agent as agent_mod
    monkeypatch.setattr(agent_mod, "get_skill_updater", lambda: _Rec())

    agent = types.SimpleNamespace(enable_skill=True)
    agent._record_repair_signal = getattr(TableAgent, "_record_repair_signal").__get__(agent)
    err = ClassifiedError(error_type=ErrorType.COLUMN_NOT_FOUND, confidence=0.85, failed_col="名")
    agent._record_repair_signal(err, "pet", "Pet", is_repeat=True)
    assert len(calls) == 1
    assert calls[0]["signal_type"] == "frequent_column_mapping_error"


def test_record_repair_signal_disabled_skill_skips(monkeypatch):
    """enable_skill=False → 不记录（与 evidence 门控一致）。"""
    calls = []

    class _Rec:
        def _ingest_anti_pattern_signal(self, record):
            calls.append(record)

    import agent.agent as agent_mod
    monkeypatch.setattr(agent_mod, "get_skill_updater", lambda: _Rec())

    agent = types.SimpleNamespace(enable_skill=False)
    agent._record_repair_signal = getattr(TableAgent, "_record_repair_signal").__get__(agent)
    err = ClassifiedError(error_type=ErrorType.TYPE_MISMATCH, confidence=0.2)
    agent._record_repair_signal(err, "pet", "Pet", is_repeat=False)
    assert calls == []

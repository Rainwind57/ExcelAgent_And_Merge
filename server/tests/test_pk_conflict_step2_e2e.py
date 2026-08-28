"""核心4 PK 冲突前移到 Step2 validate 阶段端到端测试。

验证链路:
1. add intent 含已被占用 PK 值 → validate_two_layer 检测 UNIQUE_VIOLATION/主动扫
2. 预算建议 ID(max+1) → ask callback 弹 PK 冲突专用 payload(含 suggested_id)
3. 用户接受建议 → intent PK 字段被改写为 suggested_id
4. 该 issue 从 tips 移除(已处理,不重复软失败)
5. 无 callback → 非阻断 ok=True 继续(不卡死)

运行: python -m pytest server/tests/test_pk_conflict_step2_e2e.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parser.nl_parser import NLIntent, IssueType
from agent.excel.subagent.validator_agent import ValidatorAgent


def _intent(table="reward", sheet="Reward", fields=None):
    it = NLIntent(action="add", table_hint=table, sheet_hint=sheet,
                  raw="test", extras={"fields": fields or {}})
    return it


def _schema_getter(headers, type_row=None):
    return lambda intent: (list(headers), list(type_row or []))


def _data_getter(existing_values: dict):
    """构造 data_getter callable,返 {existing_values: {col_lower: set}}。"""
    def _dg(intent):
        return {"existing_values": existing_values,
                "stem": getattr(intent, "table_hint", ""),
                "sheet": getattr(intent, "sheet_hint", "")}
    return _dg


def _make_validator():
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v._parser = None
    v._ask_callback = None
    v._required_fields = None
    v._pk_cols_cache = None
    return v


class TestPkConflictStep2E2E:
    """核心4:PK 冲突在 Step2 validate 阶段拦 + ask 接受建议 + intent 改写。"""

    def test_unique_violation_triggers_ask_with_suggested_id(self, monkeypatch):
        """field_map 抓到 UNIQUE_VIOLATION → 预算建议 ID → ask 带 suggested_id。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(range(len(intents)))), raising=False)
        v = _make_validator()
        _asked = []
        def _cb(q):
            _asked.append(q)
            return {"accept_suggest": True}
        v.set_ask_callback(_cb)
        # PK=99001 已占用
        it = _intent(fields={"reward_id": 99001, "名称": "测试包"})
        sg = _schema_getter(["reward_id", "名称"], ["int", "string"])
        ev = {"reward_id": {99001, 100600, 100601, 100602}, "名称": set()}
        dg = _data_getter(ev)
        v.validate_two_layer([it], schema_getter=sg, data_getter=dg)
        # 核心4应触发 ask
        assert len(_asked) == 1, f"应触发1次 ask,实际 {_asked}"
        _q = _asked[0]
        assert _q["error_type"] == "id_conflict"
        assert _q["mode_hint"] == "pk_conflict"
        assert _q["suggested_id"] == 100603  # max(100602)+1=100603
        assert "99001" in _q["suggestion"]

    def test_accept_suggest_rewrites_intent_pk(self, monkeypatch):
        """用户接受建议 → intent PK 字段被改为 suggested_id。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(range(len(intents)))), raising=False)
        v = _make_validator()
        v.set_ask_callback(lambda q: {"accept_suggest": True})
        it = _intent(fields={"reward_id": 99001, "名称": "测试包"})
        sg = _schema_getter(["reward_id", "名称"], ["int", "string"])
        ev = {"reward_id": {99001, 100, 200}, "名称": set()}
        dg = _data_getter(ev)
        v.validate_two_layer([it], schema_getter=sg, data_getter=dg)
        # intent PK 应被改为 99002(max=99001 +1)
        assert it.extras["fields"]["reward_id"] == 99002, \
            f"接受建议后 PK 应改写为 99002,实际 {it.extras['fields'].get('reward_id')}"

    def test_custom_id_rewrites_intent_pk(self, monkeypatch):
        """用户自定义输入 ID → intent PK 改为 custom_id。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(range(len(intents)))), raising=False)
        v = _make_validator()
        v.set_ask_callback(lambda q: {"custom_id": 88888})
        it = _intent(fields={"reward_id": 99001, "名称": "测试包"})
        sg = _schema_getter(["reward_id", "名称"], ["int", "string"])
        ev = {"reward_id": {99001, 100}, "名称": set()}
        dg = _data_getter(ev)
        v.validate_two_layer([it], schema_getter=sg, data_getter=dg)
        assert it.extras["fields"]["reward_id"] == 88888

    def test_skip_keeps_intent_unchanged(self, monkeypatch):
        """用户跳过 → intent PK 不改,issue 保留走软失败。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(range(len(intents)))), raising=False)
        v = _make_validator()
        v.set_ask_callback(lambda q: {"mode": "skip"})
        it = _intent(fields={"reward_id": 99001, "名称": "测试包"})
        sg = _schema_getter(["reward_id", "名称"], ["int", "string"])
        ev = {"reward_id": {99001, 100}, "名称": set()}
        dg = _data_getter(ev)
        out = v.validate_two_layer([it], schema_getter=sg, data_getter=dg)
        # skip → intent 不改
        assert it.extras["fields"]["reward_id"] == 99001
        # 要求 A：Step2 真阻断 → skip 标 skipped, ok=False（不写半成品）
        assert out["ok"] is False

    def test_no_callback_non_blocking(self, monkeypatch):
        """无 callback(非交互场景) → 自动改号为下一可用 ID，ok=True 不卡死。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(range(len(intents)))), raising=False)
        v = _make_validator()  # _ask_callback=None
        it = _intent(fields={"reward_id": 99001, "名称": "测试包"})
        sg = _schema_getter(["reward_id", "名称"], ["int", "string"])
        ev = {"reward_id": {99001, 100}, "名称": set()}
        dg = _data_getter(ev)
        out = v.validate_two_layer([it], schema_getter=sg, data_getter=dg)
        # 无 cb → 自动改号兜底（不卡死），PK 改为下一可用 ID
        assert out["ok"] is True
        assert it.extras["fields"]["reward_id"] == 99002  # 自动改号

    def test_proactive_scan_when_field_map_misses(self, monkeypatch):
        """field_map 漏检(intent fields 键不含表头列名) → 主动扫兜底查占用。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(range(len(intents)))), raising=False)
        v = _make_validator()
        _asked = []
        v.set_ask_callback(lambda q: (_asked.append(q), {"accept_suggest": True})[1])
        # intent fields 键是"ID"(非表头"reward_id"),field_map 唯一性检查会漏
        it = _intent(fields={"ID": 99001, "名称": "测试包"})
        sg = _schema_getter(["reward_id", "名称"], ["int", "string"])
        ev = {"reward_id": {99001, 100}, "名称": set()}
        dg = _data_getter(ev)
        v.validate_two_layer([it], schema_getter=sg, data_getter=dg)
        # 主动扫应触发 ask(field_map 漏检兜底)
        assert len(_asked) == 1, f"主动扫应触发 ask,实际 {_asked}"
        assert _asked[0]["suggested_id"] == 99002  # max 99001 +1

    def test_resolved_pk_removed_from_tips(self, monkeypatch):
        """用户接受建议后,该 PK issue 从 tips 移除(不重复软失败)。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(range(len(intents)))), raising=False)
        v = _make_validator()
        v.set_ask_callback(lambda q: {"accept_suggest": True})
        it = _intent(fields={"reward_id": 99001, "名称": "测试包"})
        sg = _schema_getter(["reward_id", "名称"], ["int", "string"])
        ev = {"reward_id": {99001, 100}, "名称": set()}
        dg = _data_getter(ev)
        out = v.validate_two_layer([it], schema_getter=sg, data_getter=dg)
        # tips 不应含 UNIQUE_VIOLATION(已处理移除)
        _uv_tips = [t for t in (out.get("tips") or [])
                   if getattr(t, "issue_type", "") == IssueType.UNIQUE_VIOLATION.value]
        assert len(_uv_tips) == 0, f"已处理的 PK issue 应从 tips 移除,残留 {_uv_tips}"

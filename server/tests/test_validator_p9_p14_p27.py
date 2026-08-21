"""validator P9/P14/P27 单测（OPTIMIZATION_LEDGER §4 清零批次）。

覆盖：
  - P9: NLIntent.multi_op_same_sheet 标记 + _suppress_over_produce 跳过标记 op
  - P14: _llm_judge_forward_ref 失败路径 return "" + logger.warning 可观测
  - P27: NLIntent.to/from_checkpoint_dict round-trip + agent_service save/load

运行: python -m pytest server/tests/test_validator_p9_p14_p27.py -v
"""
from __future__ import annotations

import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parser.nl_parser import (
    NLIntent, ValidationResult, ExecutionResult)
from agent.excel.subagent.validator_agent import ValidatorAgent


def _make_validator():
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v.parser = None
    v._ask_callback = None
    v._required_fields = None
    v._thinking_sink = None
    return v


def _split_intent(table="pet", sheet="Pet", produces=None,
                  multi_op=False):
    """SplitIntent 替身（_suppress 读 produces + multi_op_same_sheet）。"""
    from types import SimpleNamespace
    return SimpleNamespace(
        table_hint=table, sheet_hint=sheet,
        produces=produces,
        extras={"produces": produces} if produces else {},
        multi_op_same_sheet=multi_op,
    )


# ── P9：multi_op_same_sheet 标记 ─────────────────────────────


class TestP9MultiOpSameSheet:
    def test_default_false(self):
        it = NLIntent(action="add", raw="x")
        assert it.multi_op_same_sheet is False

    def test_suppress_dedupes_same_sheet_two_producers_default(self):
        """默认：同 (stem,sheet) 两 produces op → 抑制第二个（LLM 过产契约）。"""
        v = _make_validator()
        intents = [
            _split_intent("pet", "Pet", produces="new_pet_id"),
            _split_intent("pet", "Pet", produces="new_pet_id2"),
        ]
        n = v._suppress_over_produce(intents)
        assert n == 1
        assert len(intents) == 1

    def test_suppress_keeps_multi_op_same_sheet_marked(self):
        """P9：multi_op_same_sheet=True 的 op 不抑制（用户显式多 producer）。"""
        v = _make_validator()
        intents = [
            _split_intent("pet", "Pet", produces="new_pet_id", multi_op=True),
            _split_intent("pet", "Pet", produces="new_pet_id2", multi_op=True),
        ]
        n = v._suppress_over_produce(intents)
        assert n == 0, f"multi_op 标记的 op 不应被抑制: {n}"
        assert len(intents) == 2

    def test_suppress_mixed_only_unmarked_deduped(self):
        """混合：仅未标记的第二个被抑制，标记的保留。"""
        v = _make_validator()
        intents = [
            _split_intent("pet", "Pet", produces="new_pet_id", multi_op=False),
            _split_intent("pet", "Pet", produces="new_pet_id2", multi_op=True),
            _split_intent("pet", "Pet", produces="new_pet_id3", multi_op=False),
        ]
        n = v._suppress_over_produce(intents)
        # idx 0 (unmarked) 入 seen; idx 1 (marked) 跳过; idx 2 (unmarked) → seen 命中 → 抑制
        assert n == 1
        remaining = [it.produces for it in intents]
        assert "new_pet_id2" in remaining  # 标记的保留
        assert "new_pet_id3" not in remaining  # 未标记的第二个被抑制

    def test_no_produce_ops_not_suppressed(self):
        """无 produces 的引用/明细行不抑制（produces guard，P9 既有）。"""
        v = _make_validator()
        intents = [
            _split_intent("pet", "Pet", produces=None),  # 无 produces
            _split_intent("pet", "Pet", produces=None),
        ]
        n = v._suppress_over_produce(intents)
        assert n == 0
        assert len(intents) == 2


# ── P14：_llm_judge_forward_ref 失败路径可观测 ────────────────


class TestP14ForwardRefLlmUnreachable:
    def test_no_session_returns_empty_with_warning(self, caplog):
        """无 session → return ""（非阻断）+ warning 留痕（P14 可观测）。"""
        v = _make_validator()
        v._ensure_own_session = lambda: None  # 模拟无 session
        with caplog.at_level(logging.WARNING, logger="agent.excel.subagent.validator_agent"):
            out = v._llm_judge_forward_ref("pet", "pet_id", 100)
        assert out == ""
        assert any("P14" in r.message for r in caplog.records)

    def test_exception_returns_empty_with_warning(self, caplog):
        """LLM 调用异常 → return "" + warning（P14 可观测）。"""
        v = _make_validator()
        v._ensure_own_session = lambda: "sid"
        def _boom(prompt, timeout=30):
            raise RuntimeError("LLM boom")
        v._call_llm_raw = _boom
        with caplog.at_level(logging.WARNING, logger="agent.excel.subagent.validator_agent"):
            out = v._llm_judge_forward_ref("pet", "pet_id", 100)
        assert out == ""
        assert any("P14" in r.message and "异常" in r.message for r in caplog.records)

    def test_empty_response_returns_empty_with_warning(self, caplog):
        """LLM 空响应 → return "" + warning（P14 可观测）。"""
        v = _make_validator()
        v._ensure_own_session = lambda: "sid"
        v._call_llm_raw = lambda prompt, timeout=30: ""
        with caplog.at_level(logging.WARNING, logger="agent.excel.subagent.validator_agent"):
            out = v._llm_judge_forward_ref("pet", "pet_id", 100)
        assert out == ""
        assert any("空响应" in r.message for r in caplog.records)

    def test_no_json_returns_empty_with_warning(self, caplog):
        """LLM 返回无 JSON → return "" + warning（P14 可观测）。"""
        v = _make_validator()
        v._ensure_own_session = lambda: "sid"
        v._call_llm_raw = lambda prompt, timeout=30: "no json here"
        with caplog.at_level(logging.WARNING, logger="agent.excel.subagent.validator_agent"):
            out = v._llm_judge_forward_ref("pet", "pet_id", 100)
        assert out == ""
        assert any("无 JSON" in r.message for r in caplog.records)

    def test_build_verdict_returned_no_warning(self, caplog):
        """LLM 返 build → return "build" + 无 warning（正常路径）。"""
        v = _make_validator()
        v._ensure_own_session = lambda: "sid"
        v._call_llm_raw = lambda prompt, timeout=30: '{"verdict":"build","reason":"x"}'
        with caplog.at_level(logging.WARNING, logger="agent.excel.subagent.validator_agent"):
            out = v._llm_judge_forward_ref("pet", "pet_id", 100)
        assert out == "build"
        assert not any("P14" in r.message for r in caplog.records)

    def test_unknown_verdict_returns_empty_with_warning(self, caplog):
        """LLM 返未知 verdict → return "" + warning（P14 可观测）。"""
        v = _make_validator()
        v._ensure_own_session = lambda: "sid"
        v._call_llm_raw = lambda prompt, timeout=30: '{"verdict":"maybe","reason":"x"}'
        with caplog.at_level(logging.WARNING, logger="agent.excel.subagent.validator_agent"):
            out = v._llm_judge_forward_ref("pet", "pet_id", 100)
        assert out == ""
        assert any("未知 verdict" in r.message for r in caplog.records)


# ── P27：NLIntent checkpoint 序列化 round-trip ────────────────


class TestP27NLIntentCheckpoint:
    def _intent(self, **kw):
        defaults = dict(action="add", table_hint="pet", sheet_hint="Pet",
                        raw="加灵兽", extras={"fields": {"pet_id": 1, "名称": "饕餮"}})
        defaults.update(kw)
        return NLIntent(**defaults)

    def test_round_trip_basic(self):
        """基本字段 round-trip。"""
        it = self._intent()
        d = it.to_checkpoint_dict()
        rt = NLIntent.from_checkpoint_dict(d)
        assert rt.action == "add"
        assert rt.table_hint == "pet"
        assert rt.sheet_hint == "Pet"
        assert rt.raw == "加灵兽"
        assert rt.extras["fields"] == {"pet_id": 1, "名称": "饕餮"}

    def test_round_trip_with_validation(self):
        """嵌套 ValidationResult round-trip。"""
        it = self._intent()
        it.validation = ValidationResult(
            issues=[{"col": "pet_id", "issue_type": "type_mismatch"}],
            ok=False, skipped=False)
        d = it.to_checkpoint_dict()
        rt = NLIntent.from_checkpoint_dict(d)
        assert rt.validation is not None
        assert rt.validation.ok is False
        assert rt.validation.issues == [{"col": "pet_id", "issue_type": "type_mismatch"}]

    def test_round_trip_with_execution(self):
        """嵌套 ExecutionResult round-trip。"""
        it = self._intent()
        it.execution = ExecutionResult(ok=True, row=5,
                                        written_fields=["pet_id", "名称"],
                                        new_row_pk=100)
        d = it.to_checkpoint_dict()
        rt = NLIntent.from_checkpoint_dict(d)
        assert rt.execution is not None
        assert rt.execution.ok is True
        assert rt.execution.row == 5
        assert rt.execution.written_fields == ["pet_id", "名称"]
        assert rt.execution.new_row_pk == 100

    def test_round_trip_with_failures_and_marker(self):
        """P23 failures + P9 multi_op_same_sheet round-trip。"""
        it = self._intent()
        it.failures = [{"type": "validation_tip", "col": "pet_id"}]
        it.multi_op_same_sheet = True
        d = it.to_checkpoint_dict()
        rt = NLIntent.from_checkpoint_dict(d)
        assert rt.failures == [{"type": "validation_tip", "col": "pet_id"}]
        assert rt.multi_op_same_sheet is True

    def test_round_trip_none_validation_execution(self):
        """validation/execution=None → round-trip None。"""
        it = self._intent()
        d = it.to_checkpoint_dict()
        rt = NLIntent.from_checkpoint_dict(d)
        assert rt.validation is None
        assert rt.execution is None

    def test_checkpoint_dict_is_jsonable(self):
        """to_checkpoint_dict 返回 JSON-able（可 json.dumps）。"""
        import json
        it = self._intent()
        it.validation = ValidationResult(issues=[{"col": "x"}])
        d = it.to_checkpoint_dict()
        s = json.dumps(d)  # 不抛
        assert json.loads(s) == d


# ── P27：agent_service NL checkpoint save/load ────────────────


class TestP27NLCheckpointSaveLoad:
    def test_save_off_when_env_not_set(self, monkeypatch):
        """CODEMAKER_4STEP_CHECKPOINT 缺省 → save 不写，返 False。"""
        monkeypatch.delenv("CODEMAKER_4STEP_CHECKPOINT", raising=False)
        from services.agent_service import AgentService
        svc = object.__new__(AgentService)
        svc._nl_checkpoints = {}
        it = NLIntent(action="add", table_hint="pet", raw="x")
        ok = svc._save_nl_checkpoint("sess", "post_parse", [it])
        assert ok is False
        assert svc._nl_checkpoints == {}

    def test_save_load_round_trip(self, monkeypatch):
        """opt-in → save + load round-trip NLIntent[]。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        from services.agent_service import AgentService
        svc = object.__new__(AgentService)
        svc._nl_checkpoints = {}
        it = NLIntent(action="add", table_hint="pet", sheet_hint="Pet",
                      raw="加灵兽", extras={"fields": {"pet_id": 1}})
        it.validation = ValidationResult(ok=True)
        ok = svc._save_nl_checkpoint("sess", "post_parse", [it])
        assert ok is True
        loaded = svc._load_nl_checkpoint("sess", "post_parse")
        assert loaded is not None
        assert len(loaded) == 1
        rt = loaded[0]
        assert rt.table_hint == "pet"
        assert rt.sheet_hint == "Pet"
        assert rt.raw == "加灵兽"
        assert rt.extras["fields"] == {"pet_id": 1}
        assert rt.validation is not None and rt.validation.ok is True

    def test_load_missing_returns_none(self):
        """无 checkpoint → load 返 None。"""
        from services.agent_service import AgentService
        svc = object.__new__(AgentService)
        svc._nl_checkpoints = {}
        assert svc._load_nl_checkpoint("nope", "post_parse") is None

    def test_save_failure_silent_false(self, monkeypatch):
        """save 异常 → 静默返 False 不阻断。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        from services.agent_service import AgentService
        svc = object.__new__(AgentService)
        svc._nl_checkpoints = None  # 故意设 None 致 .setdefault 抛
        ok = svc._save_nl_checkpoint("sess", "post_parse",
                                     [NLIntent(action="add", raw="x")])
        assert ok is False

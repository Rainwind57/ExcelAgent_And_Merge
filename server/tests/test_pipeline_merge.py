"""LLM 合并路径(confirm+plan+validate 单次调用)单测。

覆盖:
  - StepAIEnhancer.ai_pipeline_merge 解析/校验/回退
  - pipeline_merge_enabled feature flag
  - TableAgent._merge_applicable 门控
  - TableAgent._phase_plan_validate_merged happy/拆分/表纠正
"""
from __future__ import annotations

import json
import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from agent.agent import TableAgent, AgentResult
from agent.nl_parser import NLIntent
from agent.excel.step_ai_enhancer import StepAIEnhancer


# ── StepAIEnhancer 级:mock _call_llm + fake client ──────────────────

def _fake_client():
    def _extract(raw):
        if not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None
    return types.SimpleNamespace(
        cfg=types.SimpleNamespace(default_model="codemaker"),
        extract_json_from_response=_extract,
    )


class MockEnhancer(StepAIEnhancer):
    """override _call_llm 返回固定 JSON 字符串。"""
    def __init__(self, llm_response):
        super().__init__(_fake_client())
        self._llm_response = llm_response
        self.call_count = 0

    def _call_llm(self, prompt, timeout=60):
        self.call_count += 1
        self.last_prompt = prompt
        return self._llm_response


def test_pipeline_merge_enabled_default_on(monkeypatch):
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    enh = MockEnhancer("{}")
    assert enh.pipeline_merge_enabled() is True


def test_pipeline_merge_enabled_flag_off(monkeypatch):
    monkeypatch.setenv("CODEMAKER_LLM_PIPELINE_MERGE", "0")
    enh = MockEnhancer("{}")
    assert enh.pipeline_merge_enabled() is False


def test_ai_pipeline_merge_happy():
    resp = json.dumps({
        "confirm_stem": "pet", "ambiguous": False, "confidence": 0.9,
        "fields": {"名称": "福神", "model_id": 1020}, "notes": "ok",
        "ok": True, "issues": [], "suggestions": [],
    })
    enh = MockEnhancer(resp)
    out = enh.ai_pipeline_merge(
        user_text="新增NPC福神", rule_stem="pet", all_tables=["pet", "item"],
        table_stem="pet", sheet="Pet", columns=["名称", "model_id"],
        intent_fields={"名称": "福神"}, action="add")
    assert out is not None
    assert out["confirm_stem"] == "pet"
    assert out["fields"] == {"名称": "福神", "model_id": 1020}
    assert out["ok"] is True
    assert out["ambiguous"] is False
    assert out["confidence"] == 0.9
    assert enh.call_count == 1  # 单次 LLM


def test_ai_pipeline_merge_confirm_stem_not_in_pool_falls_back():
    """LLM 输出不在表池的 stem → 回退 rule_stem。"""
    resp = json.dumps({"confirm_stem": "非表", "fields": {}, "ok": True})
    enh = MockEnhancer(resp)
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet", "item"],
        table_stem="pet", sheet="Pet", columns=["名称"],
        intent_fields={}, action="add")
    assert out["confirm_stem"] == "pet"  # 回退


def test_ai_pipeline_merge_confirm_stem_fuzzy_match():
    """LLM 输出近义 stem → 宽松匹配命中。"""
    resp = json.dumps({"confirm_stem": "pe", "fields": {}, "ok": True})
    enh = MockEnhancer(resp)
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet", "item"],
        table_stem="pet", sheet="Pet", columns=["名称"],
        intent_fields={}, action="add")
    assert out["confirm_stem"] == "pet"  # "pe" ⊂ "pet"


def test_ai_pipeline_merge_fields_list_normalized():
    """LLM 返回 list 形态 fields → 归一为 dict。"""
    resp = json.dumps({
        "confirm_stem": "pet", "fields": [{"名称": "福神"}, ["model_id", 1020]],
        "ok": True,
    })
    enh = MockEnhancer(resp)
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet"],
        table_stem="pet", sheet="Pet", columns=["名称", "model_id"],
        intent_fields={}, action="add")
    assert out["fields"] == {"名称": "福神", "model_id": 1020}


def test_ai_pipeline_merge_field_key_fuzzy_to_column():
    """LLM 输出列名不在实际列 → 宽松匹配到实际列。"""
    resp = json.dumps({
        "confirm_stem": "pet", "fields": {"名": "福神"}, "ok": True,
    })
    enh = MockEnhancer(resp)
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet"],
        table_stem="pet", sheet="Pet", columns=["名称"],
        intent_fields={}, action="add")
    assert out["fields"] == {"名称": "福神"}  # "名" ⊂ "名称" 宽松命中


def test_ai_pipeline_merge_ok_false_marks_ambiguous():
    resp = json.dumps({"confirm_stem": "pet", "fields": {}, "ok": False,
                       "issues": ["x"], "suggestions": []})
    enh = MockEnhancer(resp)
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet"],
        table_stem="pet", sheet="Pet", columns=["名称"],
        intent_fields={}, action="add")
    assert out["ok"] is False
    assert out["ambiguous"] is True  # 校验不通过 → 拆分信号


def test_ai_pipeline_merge_llm_none_returns_none():
    enh = MockEnhancer(None)
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet"],
        table_stem="pet", sheet="Pet", columns=["名称"],
        intent_fields={}, action="add")
    assert out is None


def test_ai_pipeline_merge_malformed_json_returns_none():
    enh = MockEnhancer("not json at all")
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet"],
        table_stem="pet", sheet="Pet", columns=["名称"],
        intent_fields={}, action="add")
    assert out is None


def test_ai_pipeline_merge_ai_assist_off_returns_none(monkeypatch):
    monkeypatch.setenv("CODEMAKER_AI_ASSIST", "0")
    enh = MockEnhancer(json.dumps({"confirm_stem": "pet", "fields": {}, "ok": True}))
    out = enh.ai_pipeline_merge(
        user_text="x", rule_stem="pet", all_tables=["pet"],
        table_stem="pet", sheet="Pet", columns=["名称"],
        intent_fields={}, action="add")
    assert out is None
    assert enh.call_count == 0  # AI_ASSIST=0 不调 LLM


# ── TableAgent 级:_merge_applicable + _phase_plan_validate_merged ──

class FakeEnhancer:
    """agent 级测试用的 enhancer 替身。"""
    def __init__(self, merge_result, pipeline_enabled=True):
        self._merge_result = merge_result
        self._pipeline_enabled = pipeline_enabled
        self.merge_calls = 0

    def pipeline_merge_enabled(self):
        return self._pipeline_enabled

    def ai_pipeline_merge(self, **kw):
        self.merge_calls += 1
        self.last_kw = kw
        return self._merge_result

    def _should_skip_ai(self, step, intent=None):
        return False

    def ai_plan_operation(self, **kw):
        return None

    def ai_validate_plan(self, **kw):
        return None


def _make_agent(enhancer, cli=None):
    """SimpleNamespace agent + 绑定 TableAgent 合并相关方法。"""
    agent = types.SimpleNamespace(cli=cli, _ai_enhancer=enhancer)
    for name in ("_merge_applicable", "_apply_plan_fields",
                 "_phase_plan_validate_merged", "_phase_plan",
                 "_phase_validate", "_resolve_sheet"):
        if hasattr(TableAgent, name):
            setattr(agent, name, getattr(TableAgent, name).__get__(agent))
    return agent


def _intent(action="add", fields=None, source=None):
    return NLIntent(action=action, raw="新增福神",
                    extras={"fields": fields or {}, "source": source})


def test_merge_applicable_default_on(monkeypatch):
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    monkeypatch.delenv("CODEMAKER_AI_ASSIST", raising=False)
    enh = FakeEnhancer(merge_result=None)
    agent = _make_agent(enh)
    assert agent._merge_applicable(_intent("add", {"名称": "x"})) is True


def test_merge_applicable_flag_off(monkeypatch):
    monkeypatch.setenv("CODEMAKER_LLM_PIPELINE_MERGE", "0")
    enh = FakeEnhancer(merge_result=None, pipeline_enabled=False)
    agent = _make_agent(enh)
    assert agent._merge_applicable(_intent("add", {"名称": "x"})) is False


def test_merge_applicable_wrong_action(monkeypatch):
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    enh = FakeEnhancer(merge_result=None)
    agent = _make_agent(enh)
    assert agent._merge_applicable(_intent("get", {"名称": "x"})) is False
    assert agent._merge_applicable(_intent("delete", {"名称": "x"})) is False


def test_merge_applicable_no_fields(monkeypatch):
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    enh = FakeEnhancer(merge_result=None)
    agent = _make_agent(enh)
    assert agent._merge_applicable(_intent("add", {})) is False


def test_merge_applicable_splitter_excluded(monkeypatch):
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    enh = FakeEnhancer(merge_result=None)
    agent = _make_agent(enh)
    assert agent._merge_applicable(
        _intent("add", {"名称": "x"}, source="splitter")) is False


def test_merge_applicable_no_enhancer():
    agent = types.SimpleNamespace(_ai_enhancer=None)
    assert TableAgent._merge_applicable.__get__(agent)(_intent("add", {"x": 1})) is False


# ── _phase_plan_validate_merged:happy / 拆分 / 表纠正 ───────────────

class _FakeCLI:
    def __init__(self, tables, headers):
        self._tables = tables
        self._headers = headers

    def list_tables(self):
        return self._tables

    def read_header(self, path, sheet):
        return self._headers


def test_merged_happy_path_applies_fields_no_split(monkeypatch):
    """高置信 + 表一致 + ok → 采用合并结果,不调独立 plan/validate。"""
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    merge_result = {
        "confirm_stem": "pet", "fields": {"名称": "福神"}, "notes": "n",
        "ok": True, "issues": [], "suggestions": [],
        "ambiguous": False, "confidence": 0.9,
    }
    enh = FakeEnhancer(merge_result)
    cli = _FakeCLI([Path("pet.xlsx")], ["名称"])
    agent = _make_agent(enh, cli)
    # 独立 plan/validate 替身为 recorder(不应被调)
    agent._phase_plan = lambda *a, **k: pytest.fail("不应调独立 plan")
    agent._phase_validate = lambda *a, **k: pytest.fail("不应调独立 validate")
    intent = _intent("add", {"名称": "福神"})
    res = AgentResult(intent=intent)
    path, sheet = Path("pet.xlsx"), "Pet"
    out_path, out_sheet = agent._phase_plan_validate_merged(intent, path, sheet, res)
    assert enh.merge_calls == 1
    assert out_path == path and out_sheet == sheet
    assert intent.extras["fields"] == {"名称": "福神"}  # 合并结果已应用
    # Step3/Step4 标记由合并路径补上
    assert any(s.name == "Step3计划" for s in res.steps)
    assert any(s.name == "Step4校验" for s in res.steps)


def test_merged_ambiguous_triggers_split(monkeypatch):
    """ambiguous=True → 拆分回独立 plan+validate。"""
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    merge_result = {
        "confirm_stem": "pet", "fields": {"名称": "x"}, "notes": "",
        "ok": True, "issues": [], "suggestions": [],
        "ambiguous": True, "confidence": 0.9,
    }
    enh = FakeEnhancer(merge_result)
    cli = _FakeCLI([Path("pet.xlsx")], ["名称"])
    agent = _make_agent(enh, cli)
    calls = {"plan": 0, "validate": 0}

    def fake_plan(i, p, s, r):
        calls["plan"] += 1

    def fake_validate(i, p, s, r):
        calls["validate"] += 1
    agent._phase_plan = fake_plan
    agent._phase_validate = fake_validate
    intent = _intent("add", {"名称": "x"})
    res = AgentResult(intent=intent)
    agent._phase_plan_validate_merged(intent, Path("pet.xlsx"), "Pet", res)
    assert enh.merge_calls == 1
    assert calls == {"plan": 1, "validate": 1}


def test_merged_low_confidence_triggers_split(monkeypatch):
    """confidence < 0.6 → 拆分。"""
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    merge_result = {
        "confirm_stem": "pet", "fields": {}, "notes": "",
        "ok": True, "issues": [], "suggestions": [],
        "ambiguous": False, "confidence": 0.3,
    }
    enh = FakeEnhancer(merge_result)
    cli = _FakeCLI([Path("pet.xlsx")], ["名称"])
    agent = _make_agent(enh, cli)
    calls = {"plan": 0, "validate": 0}
    agent._phase_plan = lambda *a, **k: calls.__setitem__("plan", calls["plan"] + 1)
    agent._phase_validate = lambda *a, **k: calls.__setitem__("validate", calls["validate"] + 1)
    intent = _intent("add", {"名称": "x"})
    res = AgentResult(intent=intent)
    agent._phase_plan_validate_merged(intent, Path("pet.xlsx"), "Pet", res)
    assert calls == {"plan": 1, "validate": 1}


def test_merged_llm_none_triggers_split(monkeypatch):
    """合并 LLM 失败(None) → 拆分回独立 plan+validate(不调 confirm)。"""
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    enh = FakeEnhancer(merge_result=None)
    cli = _FakeCLI([Path("pet.xlsx")], ["名称"])
    agent = _make_agent(enh, cli)
    calls = {"plan": 0, "validate": 0}
    agent._phase_plan = lambda *a, **k: calls.__setitem__("plan", calls["plan"] + 1)
    agent._phase_validate = lambda *a, **k: calls.__setitem__("validate", calls["validate"] + 1)
    intent = _intent("add", {"名称": "x"})
    res = AgentResult(intent=intent)
    agent._phase_plan_validate_merged(intent, Path("pet.xlsx"), "Pet", res)
    assert calls == {"plan": 1, "validate": 1}


def test_merged_table_correction_relocates_and_splits(monkeypatch):
    """合并确认纠正表 → 重定位 path/sheet + 拆分跑独立 plan/validate。"""
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    merge_result = {
        "confirm_stem": "item", "fields": {"名": "x"}, "notes": "",
        "ok": True, "issues": [], "suggestions": [],
        "ambiguous": False, "confidence": 0.9,
    }
    enh = FakeEnhancer(merge_result)
    cli = _FakeCLI([Path("pet.xlsx"), Path("item.xlsx")], ["名称"])
    agent = _make_agent(enh, cli)
    relocate_calls = []

    def fake_resolve_sheet(p, i):
        relocate_calls.append(p)
        return "Item"

    agent._resolve_sheet = fake_resolve_sheet
    split_calls = {"plan": 0, "validate": 0}
    agent._phase_plan = lambda i, p, s, r: split_calls.__setitem__("plan", split_calls["plan"] + 1)
    agent._phase_validate = lambda i, p, s, r: split_calls.__setitem__("validate", split_calls["validate"] + 1)
    intent = _intent("add", {"名称": "x"})
    res = AgentResult(intent=intent)
    out_path, out_sheet = agent._phase_plan_validate_merged(
        intent, Path("pet.xlsx"), "Pet", res)
    assert out_path == Path("item.xlsx")  # 重定位到纠正表
    assert out_sheet == "Item"
    assert relocate_calls == [Path("item.xlsx")]
    assert split_calls == {"plan": 1, "validate": 1}  # 表变 → 拆分
    assert res.table_stem == "item"


def test_merged_numeric_value_protected(monkeypatch):
    """happy path 应用字段时,显式数字编号/占位符被保护不被 AI 改。"""
    monkeypatch.delenv("CODEMAKER_LLM_PIPELINE_MERGE", raising=False)
    # AI 试图把 item_id=28599 改成 9999 → 保护回 28599
    merge_result = {
        "confirm_stem": "pet", "fields": {"item_id": 9999, "名称": "福神"},
        "notes": "", "ok": True, "issues": [], "suggestions": [],
        "ambiguous": False, "confidence": 0.9,
    }
    enh = FakeEnhancer(merge_result)
    cli = _FakeCLI([Path("pet.xlsx")], ["item_id", "名称"])
    agent = _make_agent(enh, cli)
    agent._phase_plan = lambda *a, **k: None
    agent._phase_validate = lambda *a, **k: None
    intent = _intent("add", {"item_id": "28599", "名称": "福神"})
    res = AgentResult(intent=intent)
    agent._phase_plan_validate_merged(intent, Path("pet.xlsx"), "Pet", res)
    assert intent.extras["fields"]["item_id"] == "28599"  # 原数字值保护

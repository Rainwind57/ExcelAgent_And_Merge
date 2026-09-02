"""语义计划 shadow diff 单测（纯函数，0 LLM，确定性）。"""
import types

from agent.excel.core.pipeline.semantic_shadow import (
    intent_signature, diff_intent_plans,
)


def _it(table, sheet, action="add", fields=None):
    return types.SimpleNamespace(
        table_hint=table, sheet_hint=sheet, action=action,
        extras={"fields": fields or {}})


def test_signature_normalizes():
    a = intent_signature(_it("Reward", "Reward", "ADD"))
    b = intent_signature({"table": "reward", "sheet": "reward", "action": "add"})
    assert a == b


def test_identical_plans_match():
    old = [_it("reward", "Reward", fields={"名称": "x", "reward_id": 1})]
    new = [_it("reward", "Reward", fields={"reward_id": 2, "名称": "y"})]
    d = diff_intent_plans(old, new)
    assert d["match"] is True
    assert d["missing_tables"] == []
    assert d["field_diffs"] == []


def test_missing_and_extra_tables():
    old = [_it("reward", "Reward"), _it("interaction", "Interaction")]
    new = [_it("reward", "Reward"), _it("spawn", "Spawn")]
    d = diff_intent_plans(old, new)
    assert "interaction/interaction/add" in d["missing_tables"]
    assert "spawn/spawn/add" in d["extra_tables"]
    assert d["match"] is False


def test_field_diffs_reported():
    old = [_it("reward", "Reward", fields={"名称": "x", "数量": 1})]
    new = [_it("reward", "Reward", fields={"名称": "x", "图标": "i"})]
    d = diff_intent_plans(old, new)
    assert d["match"] is False
    fd = d["field_diffs"][0]
    assert "数量" in fd["only_old"]
    assert "图标" in fd["only_new"]


def test_summary_counts():
    old = [_it("a", "A"), _it("b", "B")]
    new = [_it("a", "A")]
    d = diff_intent_plans(old, new)
    assert d["summary"] == {"old_count": 2, "new_count": 1, "common": 1}


def test_empty_new_reports_all_missing():
    old = [_it("a", "A"), _it("b", "B")]
    d = diff_intent_plans(old, [])
    assert set(d["missing_tables"]) == {"a/a/add", "b/b/add"}
    assert d["match"] is False

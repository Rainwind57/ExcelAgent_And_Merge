"""现象3 列匹配闸门单测（纯函数 + agent 集成胶水，0 LLM，确定性）。"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.pipeline.column_gate import (
    evaluate_column_match_gate,
    normalize_column_name,
)
from agent.agent import TableAgent, AgentResult


def _gate_agent(pk_cols):
    """构造只绑定 _column_match_gate_abort + 桩 _load_composite_pk_for_sheet 的 agent。"""
    agent = types.SimpleNamespace()
    agent._load_composite_pk_for_sheet = lambda _p, _s: list(pk_cols)
    agent._column_match_gate_abort = TableAgent._column_match_gate_abort.__get__(agent)
    return agent


def test_gate_abort_when_composite_pk_column_unmatched():
    from pathlib import Path
    os.environ.pop("CODEMAKER_COLUMN_MATCH_GATE", None)
    agent = _gate_agent(["法宝id", "法宝等级"])
    res = AgentResult(ok=True, intent=None)
    headers = ["法宝id:int", "法宝等级:int", "攻击力:int"]
    values = {1: 5, 3: 100}  # 覆盖 法宝id + 攻击力，缺 法宝等级
    abort = agent._column_match_gate_abort(
        Path("fabao.xlsx"), "FabaoLevel", headers, values, ["等級"], res)
    assert abort is True
    assert res.ok is False
    assert any(getattr(s, "name", "") == "column_match_gate" for s in res.steps)
    assert res.failures and res.failures[0]["type"] == "COLUMN_MATCH_GATE"


def test_gate_partial_when_only_optional_column_unmatched():
    from pathlib import Path
    os.environ.pop("CODEMAKER_COLUMN_MATCH_GATE", None)
    agent = _gate_agent(["法宝id", "法宝等级"])
    res = AgentResult(ok=True, intent=None)
    headers = ["法宝id:int", "法宝等级:int", "攻击力:int"]
    values = {1: 5, 2: 3}  # 复合主键都覆盖了
    abort = agent._column_match_gate_abort(
        Path("fabao.xlsx"), "FabaoLevel", headers, values, ["图标"], res)
    assert abort is False
    assert res.ok is True  # 不判整行失败
    assert not any(getattr(s, "name", "") == "column_match_gate" for s in res.steps)


def test_gate_noop_without_composite_pk():
    from pathlib import Path
    os.environ.pop("CODEMAKER_COLUMN_MATCH_GATE", None)
    agent = _gate_agent([])  # 无复合主键（单列主键自增）
    res = AgentResult(ok=True, intent=None)
    abort = agent._column_match_gate_abort(
        Path("reward.xlsx"), "Reward", ["reward_id:int", "名称"], {2: "包A"},
        ["乱写键"], res)
    assert abort is False


def test_gate_disabled_by_env_flag():
    from pathlib import Path
    os.environ["CODEMAKER_COLUMN_MATCH_GATE"] = "0"
    try:
        agent = _gate_agent(["法宝id", "法宝等级"])
        res = AgentResult(ok=True, intent=None)
        abort = agent._column_match_gate_abort(
            Path("fabao.xlsx"), "FabaoLevel", ["法宝id:int", "法宝等级:int"],
            {1: 5}, ["等級"], res)
        assert abort is False  # 关闸门后放行
    finally:
        os.environ.pop("CODEMAKER_COLUMN_MATCH_GATE", None)


def test_normalize_strips_suffix_and_separators():
    assert normalize_column_name("法宝id:int") == "法宝id"
    assert normalize_column_name("Skill_Id") == "skillid"
    assert normalize_column_name(" 法宝 等级 ") == "法宝等级"
    assert normalize_column_name(None) == ""


def test_no_unmatched_keys_is_ok():
    r = evaluate_column_match_gate(
        unmatched_keys=[],
        covered_columns=["法宝id", "法宝等级"],
        key_columns=["法宝id", "法宝等级"],
    )
    assert r["action"] == "ok"
    assert r["unmatched_keys"] == []


def test_unmatched_but_all_key_columns_covered_is_partial():
    # 复合主键都覆盖了，未匹配的是可选列 → partial（写其余列，不判整行失败）
    r = evaluate_column_match_gate(
        unmatched_keys=["图标路径拼错的键"],
        covered_columns=["法宝id", "法宝等级", "攻击力"],
        key_columns=["法宝id", "法宝等级"],
    )
    assert r["action"] == "partial"
    assert r["unmatched_keys"] == ["图标路径拼错的键"]
    assert r["uncovered_key_columns"] == []


def test_key_column_uncovered_with_unmatched_key_aborts():
    # 复合主键之一（法宝等级）没覆盖，且有未绑定键 → abort，禁止写脏行
    r = evaluate_column_match_gate(
        unmatched_keys=["等級"],  # 繁体误写，匹配不上"法宝等级"
        covered_columns=["法宝id", "攻击力"],
        key_columns=["法宝id", "法宝等级"],
    )
    assert r["action"] == "abort"
    assert "法宝等级" in r["uncovered_key_columns"]
    assert r["unmatched_keys"] == ["等級"]
    assert r["reason"]


def test_empty_key_columns_never_aborts():
    # 无复合主键声明（单列主键自增）→ 即使有未匹配键也只 partial，绝不 abort
    r = evaluate_column_match_gate(
        unmatched_keys=["乱写的列"],
        covered_columns=["名称"],
        key_columns=[],
    )
    assert r["action"] == "partial"


def test_normalization_makes_covered_match_key_columns():
    # covered 用带后缀表头名，key_columns 用归一名 → 归一后应视为已覆盖
    r = evaluate_column_match_gate(
        unmatched_keys=["x"],
        covered_columns=["法宝id:int", "法宝等级:int"],
        key_columns=["法宝id", "法宝等级"],
    )
    assert r["action"] == "partial"
    assert r["uncovered_key_columns"] == []


def test_blank_unmatched_keys_are_ignored():
    r = evaluate_column_match_gate(
        unmatched_keys=["", "   ", None],
        covered_columns=["法宝id"],
        key_columns=["法宝id", "法宝等级"],
    )
    assert r["action"] == "ok"

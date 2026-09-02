"""Step2 replan_hints 单测（纯函数，0 LLM，确定性）。"""
import types

from agent.excel.core.pipeline.replan_hints import build_replan_hints


def _err(error_type, table="", sheet="", column="", root_cause=""):
    return types.SimpleNamespace(
        error_type=error_type, table=table, sheet=sheet, column=column,
        root_cause=root_cause, message=root_cause)


def test_classifies_col_not_found():
    hints = build_replan_hints([_err("col_not_found", "reward", "Reward", "reward_idd")])
    assert len(hints) == 1
    assert hints[0]["kind"] == "missing_column"
    assert hints[0]["col"] == "reward_idd"
    assert hints[0]["suggestion"]


def test_classifies_missing_producer_and_table():
    hints = build_replan_hints([
        _err("upstream_placeholder_unresolved", "spawn", "Spawn", root_cause="上游依赖未产出"),
        _err("segment_partial_coverage", "interaction", "Interaction"),
    ])
    kinds = {h["kind"] for h in hints}
    assert kinds == {"missing_producer", "missing_table"}


def test_dict_failures_also_supported():
    hints = build_replan_hints([
        {"type": "unique_violation", "table": "r", "sheet": "R", "col": "id",
         "root_cause": "ID 已占用"},
    ])
    assert hints[0]["kind"] == "pk_conflict"


def test_dedup_same_issue():
    e = _err("col_not_found", "r", "R", "c")
    hints = build_replan_hints([e, e, e])
    assert len(hints) == 1


def test_unknown_error_type_ignored():
    hints = build_replan_hints([_err("some_random_soft_tip", "r", "R", "c")])
    assert hints == []


def test_type_mismatch_maps_to_fix_field_type():
    hints = build_replan_hints([_err("type_mismatch", "r", "R", "lvl",
                                     root_cause="type_mismatch: 需 int")])
    assert hints[0]["kind"] == "fix_field_type"

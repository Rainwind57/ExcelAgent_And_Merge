"""repair_playbook 单测：路由表 + 各 ErrorType 定向策略 + Level 1 修复原语。

capability: error-classification-repair
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.repair.error_classifier import ClassifiedError, ErrorType
from agent.excel.repair.repair_playbook import (
    RepairActionKind,
    RepairPlaybook,
    RepairTaskCtx,
    _best_column_match,
    _coerce_value,
)


def _err(error_type, **kw):
    return ClassifiedError(error_type=error_type, **kw)


def _ctx(**kw):
    return RepairTaskCtx(**kw)


def test_select_routes_each_error_type():
    pb = RepairPlaybook()
    assert pb.select(ErrorType.COLUMN_NOT_FOUND).name == "column_candidate_remap"
    assert pb.select(ErrorType.TYPE_MISMATCH).name == "type_coerce"
    assert pb.select(ErrorType.ID_CONFLICT).name == "id_reallocate"
    assert pb.select(ErrorType.PK_MISPLACED).name == "pk_clear_autoinc"
    assert pb.select(ErrorType.CROSS_REF_BROKEN).name == "cascade_dep_check"
    assert pb.select(ErrorType.FORMULA_ERROR).name == "formula_ref_fix"
    assert pb.select(ErrorType.ROW_NOT_FOUND).name == "row_alias_resolve"
    assert pb.select(ErrorType.UNKNOWN).name == "generic_error_feedback"


def test_select_unknown_fallback():
    pb = RepairPlaybook()
    # 覆盖：移除某类型，select 应回退 unknown
    pb._table.pop(ErrorType.ID_CONFLICT, None)
    s = pb.select(ErrorType.ID_CONFLICT)
    assert s.error_type is ErrorType.UNKNOWN


def test_column_not_found_remap_via_difflib():
    pb = RepairPlaybook()
    err = _err(ErrorType.COLUMN_NOT_FOUND, failed_col="灵兽名")
    ctx = _ctx(headers=["灵兽名称", "等级"])
    action = pb.apply(ErrorType.COLUMN_NOT_FOUND, err, ctx)
    assert action.kind is RepairActionKind.RE_EXECUTE
    assert action.fix_payload["column_remap"]["灵兽名"] == "灵兽名称"


def test_column_not_found_via_alias():
    pb = RepairPlaybook()
    err = _err(ErrorType.COLUMN_NOT_FOUND, failed_col="名字")
    ctx = _ctx(headers=["名称"], column_aliases={"名字": "名称"})
    action = pb.apply(ErrorType.COLUMN_NOT_FOUND, err, ctx)
    assert action.fix_payload["column_remap"]["名字"] == "名称"


def test_column_not_found_zero_match_escalates():
    pb = RepairPlaybook()
    err = _err(ErrorType.COLUMN_NOT_FOUND, failed_col="完全不存在的列XYZ")
    ctx = _ctx(headers=["名称", "等级"])
    action = pb.apply(ErrorType.COLUMN_NOT_FOUND, err, ctx)
    assert action.kind is RepairActionKind.ESCALATE_LLM


def test_type_mismatch_coerce_int():
    pb = RepairPlaybook()
    err = _err(ErrorType.TYPE_MISMATCH, failed_col="等级", failed_val="12.0")
    ctx = _ctx(headers=["等级"], col_types={"等级": "int"})
    action = pb.apply(ErrorType.TYPE_MISMATCH, err, ctx)
    assert action.kind is RepairActionKind.RE_EXECUTE
    assert action.fix_payload["value_coerce"]["等级"] == 12


def test_type_mismatch_coerce_fail_escalates():
    pb = RepairPlaybook()
    err = _err(ErrorType.TYPE_MISMATCH, failed_col="等级", failed_val="abc")
    ctx = _ctx(col_types={"等级": "int"})
    action = pb.apply(ErrorType.TYPE_MISMATCH, err, ctx)
    assert action.kind is RepairActionKind.ESCALATE_LLM


def test_id_conflict_requests_new_id():
    pb = RepairPlaybook()
    err = _err(ErrorType.ID_CONFLICT, failed_val="1001")
    action = pb.apply(ErrorType.ID_CONFLICT, err, _ctx())
    assert action.kind is RepairActionKind.RE_EXECUTE
    assert action.fix_payload["allocate_new_id"] is True


def test_pk_misplaced_clears_pk():
    pb = RepairPlaybook()
    err = _err(ErrorType.PK_MISPLACED, failed_val="效果码")
    action = pb.apply(ErrorType.PK_MISPLACED, err, _ctx())
    assert action.kind is RepairActionKind.RE_EXECUTE
    assert action.fix_payload["clear_pk"] is True


def test_row_not_found_with_aliases():
    pb = RepairPlaybook()
    err = _err(ErrorType.ROW_NOT_FOUND, failed_val="小白")
    ctx = _ctx(row_aliases={"小白": ["1001", "1002"]})
    action = pb.apply(ErrorType.ROW_NOT_FOUND, err, ctx)
    assert action.kind is RepairActionKind.RE_EXECUTE
    assert action.fix_payload["row_re_resolve_candidates"] == ["1001", "1002"]


def test_row_not_found_zero_aliases_escalates():
    pb = RepairPlaybook()
    err = _err(ErrorType.ROW_NOT_FOUND, failed_val="小白")
    action = pb.apply(ErrorType.ROW_NOT_FOUND, err, _ctx())
    assert action.kind is RepairActionKind.ESCALATE_LLM


def test_unknown_escalates_llm():
    pb = RepairPlaybook()
    err = _err(ErrorType.UNKNOWN)
    action = pb.apply(ErrorType.UNKNOWN, err, _ctx())
    assert action.kind is RepairActionKind.ESCALATE_LLM


def test_best_column_match_difflib():
    assert _best_column_match("灵兽名", ["灵兽名称", "等级"], {}) == "灵兽名称"
    assert _best_column_match("xyz", ["名称"], {}) is None


def test_coerce_value_helpers():
    assert _coerce_value("12.0", "int") == (12, "")
    assert _coerce_value("3.14", "float") == (3.14, "")
    assert _coerce_value("是", "bool") == (True, "")
    assert _coerce_value("abc", "int")[1] != ""


def test_register_overrides_strategy():
    from agent.excel.repair.repair_playbook import RepairStrategy, RepairLevel
    pb = RepairPlaybook()
    custom = RepairStrategy(name="custom", error_type=ErrorType.TYPE_MISMATCH, level=RepairLevel.RULE)
    pb.register(custom)
    assert pb.select(ErrorType.TYPE_MISMATCH).name == "custom"

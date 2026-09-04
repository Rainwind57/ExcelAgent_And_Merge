"""semantic_gate + SEMANTIC_OUTLIER 修复路径单元测试。

验证 #2 值语义合理性门的新能力（baseline 无此能力）：
- 硬编码范围违例检测（_verify_write 原不做 min/max 检查）
- 列历史分布离群检测（原无分布检测）
- 枚举白名单违例检测（原无白名单校验）
- 正常值不误报（保守策略）
- _h_semantic_outlier 修复 handler 产 value_coerce
- classify 路由 SEMANTIC_OUTLIER
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from agent.excel.core.semantic_gate import (
    run_semantic_gate, clear_cache, _try_numeric, _check_distribution_outlier, ColStats,
)
from agent.excel.repair.error_classifier import ErrorType, VerifyResult, classify, ClassifiedError
from agent.excel.repair.repair_playbook import _h_semantic_outlier, RepairTaskCtx, RepairActionKind


class _MockCLI:
    """模拟 cli.read_sheet 返回固定行。"""
    def __init__(self, rows):
        self._rows = rows

    def read_sheet(self, path, sheet):
        return self._rows


# ── 硬编码范围违例 ──────────────────────────────────────

def test_hardcoded_range_max_violation():
    """值超 vc max → error issue, suggested_fix=max."""
    clear_cache()
    vc = {"攻击力": {"type": "int", "min": 0, "max": 100}}
    result_rows = [{"col_name": "攻击力", "new_value": 10000, "col": 2}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["攻击力"], result_rows, _MockCLI([]), vc)
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert issues[0]["suggested_fix"] == 100
    assert "上限" in issues[0]["reason"]


def test_hardcoded_range_min_violation():
    """值低于 vc min → error, suggested_fix=min."""
    clear_cache()
    vc = {"概率": {"type": "float", "min": 0, "max": 100}}
    result_rows = [{"col_name": "概率", "new_value": -50, "col": 3}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["概率"], result_rows, _MockCLI([]), vc)
    assert len(issues) == 1
    assert issues[0]["suggested_fix"] == 0


# ── 分布离群检测 ────────────────────────────────────────

def test_distribution_outlier_extreme_high():
    """新值远超列历史分布（post-write，新值已在样本中）→ error."""
    clear_cache()
    vc = {"攻击力": {"type": "int"}}  # 无 min/max，靠分布
    headers = ["id", "攻击力"]
    rows = [headers] + [[i, i * 10] for i in range(1, 11)] + [[11, 50000]]  # 末行是新写入的离群值
    cli = _MockCLI(rows)
    result_rows = [{"col_name": "攻击力", "new_value": 50000, "col": 2}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", headers, result_rows, cli, vc)
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert "分布" in issues[0]["reason"]


def test_distribution_outlier_zero_column():
    """列默认 0，新值超大 → error."""
    clear_cache()
    vc = {"暴击率": {"type": "int"}}
    headers = ["id", "暴击率"]
    rows = [headers] + [[i, 0] for i in range(1, 21)] + [[21, 5000]]
    cli = _MockCLI(rows)
    result_rows = [{"col_name": "暴击率", "new_value": 5000, "col": 2}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", headers, result_rows, cli, vc)
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"


def test_distribution_allows_existing_large_values():
    """列历史已有大值 → 新大值不误报（p99 大说明列允许大值）."""
    clear_cache()
    vc = {"伤害": {"type": "int"}}
    headers = ["id", "伤害"]
    rows = [headers] + [[i, 5000] for i in range(1, 21)]  # 全 5000
    cli = _MockCLI(rows)
    result_rows = [{"col_name": "伤害", "new_value": 5000, "col": 2}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", headers, result_rows, cli, vc)
    assert len(issues) == 0


# ── #21 R5：add 跳过分布离群（新值偏离历史分布是 add 的常态）──

def test_add_skips_distribution_outlier():
    """add 操作：median=0 列写大值（合法新 ID）→ 不报分布离群。"""
    clear_cache()
    vc = {"法术编号": {"type": "int"}}
    headers = ["id", "法术编号"]
    rows = [headers] + [[i, 0] for i in range(1, 21)] + [[21, 700010]]
    cli = _MockCLI(rows)
    result_rows = [{"col_name": "法术编号", "new_value": 700010, "col": 2}]
    # set/modify 仍检测（默认 action=""）
    issues_set = run_semantic_gate("spell", "common_spell", "/fake/spell.xlsx",
                                  headers, result_rows, cli, vc, action="set")
    assert len(issues_set) == 1
    assert issues_set[0]["severity"] == "error"
    # add 跳过分布离群
    clear_cache()
    issues_add = run_semantic_gate("spell", "common_spell", "/fake/spell.xlsx",
                                   headers, result_rows, cli, vc, action="add")
    assert len(issues_add) == 0


def test_add_still_checks_hardcoded_range():
    """add 操作：硬范围违例仍检测（min/max 对 add 仍是有效约束）。"""
    clear_cache()
    vc = {"攻击力": {"type": "int", "min": 0, "max": 100}}
    result_rows = [{"col_name": "攻击力", "new_value": 10000, "col": 2}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["攻击力"],
                               result_rows, _MockCLI([]), vc, action="add")
    assert len(issues) == 1
    assert issues[0]["suggested_fix"] == 100


def test_add_still_checks_enum_whitelist(monkeypatch):
    """add 操作：枚举白名单违例仍检测。"""
    clear_cache()
    vc = {"品质": {"type": "int"}}
    result_rows = [{"col_name": "品质", "new_value": 99, "col": 3}]
    monkeypatch.setattr(
        "agent.excel.core.semantic_gate._load_enum_values",
        lambda stem, sheet, col: [1, 2, 3, 4, 5],
    )
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["品质"],
                               result_rows, _MockCLI([]), vc, action="add")
    assert len(issues) == 1
    assert issues[0]["suggested_fix"] == 5


# ── 枚举白名单 ──────────────────────────────────────────

def test_enum_whitelist_violation(monkeypatch):
    """int 列值不在枚举白名单 → error, suggested_fix=最接近枚举值."""
    clear_cache()
    vc = {"品质": {"type": "int"}}
    result_rows = [{"col_name": "品质", "new_value": 99, "col": 3}]
    monkeypatch.setattr(
        "agent.excel.core.semantic_gate._load_enum_values",
        lambda stem, sheet, col: [1, 2, 3, 4, 5],
    )
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["品质"], result_rows, _MockCLI([]), vc)
    assert len(issues) == 1
    assert issues[0]["severity"] == "error"
    assert issues[0]["suggested_fix"] == 5  # 99 最接近 5
    assert "白名单" in issues[0]["reason"]


def test_enum_whitelist_pass(monkeypatch):
    """枚举值命中白名单不报."""
    clear_cache()
    vc = {"品质": {"type": "int"}}
    result_rows = [{"col_name": "品质", "new_value": 3, "col": 3}]
    monkeypatch.setattr(
        "agent.excel.core.semantic_gate._load_enum_values",
        lambda stem, sheet, col: [1, 2, 3, 4, 5],
    )
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["品质"], result_rows, _MockCLI([]), vc)
    assert len(issues) == 0


# ── 不误报 ──────────────────────────────────────────────

def test_normal_value_no_issue():
    """正常值不报."""
    clear_cache()
    vc = {"攻击力": {"type": "int", "min": 0, "max": 100}}
    result_rows = [{"col_name": "攻击力", "new_value": 50, "col": 2}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["攻击力"], result_rows, _MockCLI([]), vc)
    assert len(issues) == 0


def test_empty_result_rows_no_issue():
    """空 result_rows 不报."""
    clear_cache()
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["攻击力"], [], _MockCLI([]), {})
    assert len(issues) == 0


def test_gate_exception_returns_empty():
    """gate 异常降级放行（返回空 issues）."""
    clear_cache()
    # cli 抛异常 → _get_col_stats 返回 None → 不崩
    bad_cli = MagicMock()
    bad_cli.read_sheet.side_effect = RuntimeError("boom")
    vc = {"攻击力": {"type": "int"}}
    result_rows = [{"col_name": "攻击力", "new_value": 50, "col": 2}]
    issues = run_semantic_gate("pet", "Pet", "/fake/pet.xlsx", ["攻击力"], result_rows, bad_cli, vc)
    # 硬范围无违例 + 分布读取失败降级 → 空
    assert isinstance(issues, list)


# ── 修复 handler ────────────────────────────────────────

def test_h_semantic_outlier_with_suggested_fix():
    """handler 有 suggested_fix → RE_EXECUTE + value_coerce."""
    err = ClassifiedError(
        error_type=ErrorType.SEMANTIC_OUTLIER,
        confidence=0.9,
        failed_col="攻击力",
        failed_val=10000,
        verify_signals={"semantic_issues": [{
            "column": "攻击力", "value": 10000,
            "reason": "超上限", "severity": "error", "suggested_fix": 100,
        }]},
    )
    ctx = RepairTaskCtx(table_stem="pet", sheet="Pet", headers=["攻击力"])
    action = _h_semantic_outlier(err, ctx)
    assert action.kind == RepairActionKind.RE_EXECUTE
    assert action.fix_payload == {"value_coerce": {"攻击力": 100}}


def test_h_semantic_outlier_no_suggestion_escalates():
    """handler 无 suggested_fix → ESCALATE_LLM."""
    err = ClassifiedError(
        error_type=ErrorType.SEMANTIC_OUTLIER,
        confidence=0.9,
        failed_col="攻击力",
        failed_val=10000,
        verify_signals={"semantic_issues": [{
            "column": "攻击力", "value": 10000,
            "reason": "分布离群", "severity": "error", "suggested_fix": None,
        }]},
    )
    ctx = RepairTaskCtx(table_stem="pet", sheet="Pet")
    action = _h_semantic_outlier(err, ctx)
    assert action.kind == RepairActionKind.ESCALATE_LLM


def test_h_semantic_outlier_empty_issues_escalates():
    """handler 无明细 → ESCALATE_LLM."""
    err = ClassifiedError(
        error_type=ErrorType.SEMANTIC_OUTLIER,
        confidence=0.9,
        failed_col="攻击力",
        failed_val=10000,
        verify_signals={"semantic_issues": []},
    )
    ctx = RepairTaskCtx(table_stem="pet", sheet="Pet")
    action = _h_semantic_outlier(err, ctx)
    assert action.kind == RepairActionKind.ESCALATE_LLM


# ── classify 路由 ───────────────────────────────────────

def test_classify_routes_semantic_outlier():
    """VerifyResult(failed_kind=SEMANTIC_OUTLIER) → ClassifiedError(SEMANTIC_OUTLIER)."""
    vr = VerifyResult(
        passed=False,
        failed_kind=ErrorType.SEMANTIC_OUTLIER,
        semantic_issues=[{
            "column": "攻击力", "value": 10000,
            "reason": "超上限", "suggested_fix": 100,
        }],
        checked=1,
    )
    classified = classify(None, None, verify_output=vr)
    assert classified.error_type == ErrorType.SEMANTIC_OUTLIER
    assert classified.failed_col == "攻击力"
    assert classified.failed_val == 10000
    assert classified.verify_signals["semantic_issues"] == vr.semantic_issues
    assert classified.confidence == 0.9


# ── 策略表注册 ──────────────────────────────────────────

def test_strategy_table_has_semantic_outlier():
    """_STRATEGY_TABLE 注册了 SEMANTIC_OUTLIER 策略."""
    from agent.excel.repair.repair_playbook import _STRATEGY_TABLE, RepairLevel
    strat = _STRATEGY_TABLE.get(ErrorType.SEMANTIC_OUTLIER)
    assert strat is not None
    assert strat.handler is _h_semantic_outlier
    assert strat.level == RepairLevel.RULE
    assert strat.max_rounds >= 1


# ── 数值解析工具 ────────────────────────────────────────

def test_try_numeric_handles_units():
    """数值解析处理百分号/单位."""
    assert _try_numeric("50%") == 50.0
    assert _try_numeric("100") == 100.0
    assert _try_numeric(3.14) == 3.14
    assert _try_numeric("abc") is None
    assert _try_numeric(None) is None
    assert _try_numeric("") is None


def test_col_stats_robust_to_outlier():
    """ColStats 对单点离群稳健（median/MAD 不被拉偏）."""
    stats = ColStats([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 50000.0])
    # median 应近 6，不被 50000 拉偏
    assert 5.0 <= stats.median <= 7.0
    assert stats.mad < 10.0  # MAD 也不被拉偏

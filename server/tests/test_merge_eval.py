"""merge_eval 单测（capability: merge-evaluation）。

用小种子验证 4 类正确性指标计算正确（确定性，不跑大表）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.merge_eval import run_correctness, aggregate, render_report


def test_correctness_metrics():
    """小种子正确性指标：4 类指标值符合 ground truth 预期。"""
    c = run_correctness()
    # #24 语义相等归一后：id=2 value（100/"100.0"/"1e2"）自动归一为非冲突，
    # 仅 id=1 value（10/20/30）真冲突 → 总冲突 1，假冲突 0，false_conflict_rate 0.0
    assert c["total_conflict_cells"] == 1, f"expected 1 conflict (id=1 only), got {c['total_conflict_cells']}"
    assert c["total_false_conflicts"] == 0, f"expected 0 false conflicts after #24, got {c['total_false_conflicts']}"
    assert abs(c["false_conflict_rate"] - 0.0) < 1e-9
    # merge_success_rate = 1/1 = 1.0（id=1 被 take_max 策略自动解决）
    assert abs(c["merge_success_rate"] - 1.0) < 1e-9
    # id_remap_accuracy = 1.0（id=2000 冲突重映射正确）
    assert abs(c["id_remap_accuracy"] - 1.0) < 1e-9
    # ref_integrity_pass_rate = 1.0（无 dangling）
    assert abs(c["ref_integrity_pass_rate"] - 1.0) < 1e-9
    # id 重映射数 = 1
    assert c["total_id_remapped"] == 1
    assert c["total_id_remap_correct"] == 1


def test_aggregate_summary():
    """aggregate 产出 summary 含白名单指标。"""
    c = run_correctness()
    agg = aggregate(c, {"skipped": "test"}, {"skipped": "test"})
    s = agg["summary"]
    for k in ("merge_success_rate", "false_conflict_rate",
              "id_remap_accuracy", "ref_integrity_pass_rate",
              "bigdata_total_elapsed_ms", "parallel_speedup"):
        assert k in s, f"summary missing {k}"


def test_render_report():
    """render_report 产出含全部指标段的 markdown。"""
    c = run_correctness()
    agg = aggregate(c, {"skipped": "test", "total_rows": 0}, {"skipped": "test"})
    md = render_report(agg)
    assert "merge_success_rate" in md
    assert "false_conflict_rate" in md
    assert "id_remap_accuracy" in md
    assert "ref_integrity_pass_rate" in md
    assert "大表性能" in md
    assert "并行比对加速比" in md


def test_flagged_cells_match_ground_truth():
    """#24 后 flagged cells 仅含 id=1 value（真冲突）；id=2 value 已被语义归一消解。"""
    c = run_correctness()
    sheets = c["sheets"]
    assert len(sheets) == 1
    flagged = {tuple(fc) for fc in sheets[0]["flagged_cells"]}
    assert ("1", "value") in flagged, f"id=1 value 未被标冲突: {flagged}"
    assert ("2", "value") not in flagged, f"id=2 value 应被 #24 语义归一消解，仍被标冲突: {flagged}"

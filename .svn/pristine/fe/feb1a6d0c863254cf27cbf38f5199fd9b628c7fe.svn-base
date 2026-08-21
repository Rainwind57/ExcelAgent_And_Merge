"""multi_intent_eval 单测（capability: multi-intent-evaluation）。

验证纯逻辑层指标计算正确（拓扑/回滚/加速比），不依赖 serve。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.multi_intent_eval import (
    eval_topo_correctness, eval_rollback, eval_speedup,
    aggregate, render_report, load_fixtures,
)


FIXTURES = load_fixtures()


def test_topo_correctness_linear():
    """线性链：拓扑序满足 producer-before-consumer。"""
    r = eval_topo_correctness(FIXTURES)
    cases = {c["id"]: c for c in r["cases"]}
    lin = cases["linear_chain"]
    assert lin["correct"], f"linear_chain 拓扑序错误: order={lin['order']}"
    assert lin["violated"] == []
    # producer 必须在 consumer 之前
    order = lin["order"]
    assert order.index(0) < order.index(1)
    assert order.index(1) < order.index(2)
    assert order.index(2) < order.index(3)


def test_topo_correctness_diamond():
    """菱形依赖：A→B, A→C, B→D, C→D 无违反。"""
    r = eval_topo_correctness(FIXTURES)
    cases = {c["id"]: c for c in r["cases"]}
    dia = cases["diamond_deps"]
    assert dia["correct"]
    assert dia["violated"] == []
    order = dia["order"]
    assert order.index(0) < order.index(1)
    assert order.index(0) < order.index(2)
    assert order.index(1) < order.index(3)
    assert order.index(2) < order.index(3)


def test_topo_cycle_detected():
    """循环依赖用例：检测到环即正确。"""
    r = eval_topo_correctness(FIXTURES)
    cases = {c["id"]: c for c in r["cases"]}
    cyc = cases["cycle_fallback"]
    assert cyc["expect_cycle"]
    assert cyc["cycle_detected"], "循环依赖未检测到"
    assert cyc["correct"]


def test_topo_independent_no_violation():
    """无依赖独立意图：原序合法，无违反。"""
    r = eval_topo_correctness(FIXTURES)
    cases = {c["id"]: c for c in r["cases"]}
    indep = cases["independent"]
    assert indep["correct"]
    assert indep["violated"] == []
    assert indep["order"] == [0, 1, 2]


def test_topo_overall_rate():
    """全部 topo 用例通过率 = 1.0。"""
    r = eval_topo_correctness(FIXTURES)
    assert r["topo_correct_rate"] == 1.0
    assert r["n_correct"] == r["n_cases"]


def test_rollback_mid_chain_fail():
    """3步链第2步失败：第3步跳过 + failed_tables 标记。"""
    r = eval_rollback(FIXTURES)
    scs = {s["id"]: s for s in r["scenarios"]}
    mid = scs["mid_chain_fail"]
    assert mid["correct"], "mid_chain_fail 回滚校验失败"
    assert mid["failed_at_ok"]
    assert mid["skipped_ok"]


def test_rollback_first_step_fail():
    """首步失败：后续全跳过。"""
    r = eval_rollback(FIXTURES)
    scs = {s["id"]: s for s in r["scenarios"]}
    first = scs["first_step_fail"]
    assert first["correct"]
    assert first["failed_at_ok"]


def test_rollback_independent_no_prev_rollback():
    """独立意图失败：前序不回滚。"""
    r = eval_rollback(FIXTURES)
    scs = {s["id"]: s for s in r["scenarios"]}
    indep = scs["independent_fail"]
    assert indep["correct"]
    assert indep["prev_not_rolled_back_ok"]


def test_rollback_overall_rate():
    """全部回滚场景通过率 = 1.0。"""
    r = eval_rollback(FIXTURES)
    assert r["rollback_correct_rate"] == 1.0
    assert r["n_correct"] == r["n_scenarios"]


def test_speedup_positive():
    """并行加速比 > 1（4 worker 独立意图应明显加速）。"""
    r = eval_speedup(FIXTURES)
    assert not r.get("skipped"), f"speedup 跳过: {r.get('skipped')}"
    assert r["multi_intent_speedup"] > 1.5, f"加速比过低: {r['multi_intent_speedup']}"
    assert r["serial_elapsed_ms"] > r["parallel_elapsed_ms"]


def test_aggregate_summary():
    """aggregate summary 含全部白名单指标。"""
    topo = eval_topo_correctness(FIXTURES)
    rb = eval_rollback(FIXTURES)
    sp = eval_speedup(FIXTURES)
    agg = aggregate(topo, rb, sp, {"skipped": "test"})
    s = agg["summary"]
    for k in ("topo_correct_rate", "rollback_correct_rate", "multi_intent_speedup",
              "split_correct_rate", "step2_success_rate", "step3_success_rate",
              "step4_success_rate", "step5_success_rate", "step6_success_rate",
              "placeholder_closure_rate"):
        assert k in s, f"summary missing {k}"


def test_render_report():
    """render_report 产出含全部指标段的 markdown。"""
    topo = eval_topo_correctness(FIXTURES)
    rb = eval_rollback(FIXTURES)
    sp = eval_speedup(FIXTURES)
    agg = aggregate(topo, rb, sp, {"skipped": "test"})
    md = render_report(agg)
    assert "topo_correct_rate" in md
    assert "rollback_correct_rate" in md
    assert "multi_intent_speedup" in md
    assert "拓扑排序明细" in md
    assert "回滚场景明细" in md
    assert "并行加速比" in md

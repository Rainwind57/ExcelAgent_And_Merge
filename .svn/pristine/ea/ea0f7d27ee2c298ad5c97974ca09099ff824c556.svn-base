"""eval_step_aggregate 单测（capability: step-wise-success-aggregation）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.eval_step_aggregate import aggregate_step_success, format_step_report


def _write_json(tmp_path: Path, data: dict) -> str:
    p = tmp_path / "chain.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(p)


def test_aggregate_basic_success():
    data = {"chains": [
        {"steps": [
            {"entry": {"status": "matched"}},
            {"entry": {"status": "matched"}},
            {"entry": {"status": "matched"}},
        ]},
        {"steps": [
            {"entry": {"status": "matched"}},
            {"entry": {"status": "matched"}},
            {"entry": {"status": "missing"}},
        ]},
    ]}
    p = _write_json(Path(__import__("tempfile").mkdtemp()), data)
    agg = aggregate_step_success(p)
    assert agg["chain_count"] == 2
    assert agg["step_wise"][0]["success_rate"] == 1.0
    assert agg["step_wise"][2]["success_rate"] == 0.5
    assert agg["step_wise"][2]["break_count"] == 1


def test_aggregate_short_chain_not_counted():
    """短链后续 step 不计入分母。"""
    data = {"chains": [
        {"steps": [{"entry": {"status": "matched"}}]},  # 1 步
        {"steps": [{"entry": {"status": "matched"}},
                   {"entry": {"status": "missing"}}]},  # 2 步
    ]}
    p = _write_json(Path(__import__("tempfile").mkdtemp()), data)
    agg = aggregate_step_success(p)
    # step_index=1 只有 1 条链有（total=1）
    assert agg["step_wise"][1]["total"] == 1
    assert agg["step_wise"][1]["success_rate"] == 0.0


def test_aggregate_break_count_hot_step():
    """break_count > 30% 标热步。"""
    data = {"chains": [
        {"steps": [{"entry": {"status": "matched"}}, {"entry": {"status": "missing"}}]},
        {"steps": [{"entry": {"status": "matched"}}, {"entry": {"status": "missing"}}]},
        {"steps": [{"entry": {"status": "matched"}}, {"entry": {"status": "matched"}}]},
    ]}
    p = _write_json(Path(__import__("tempfile").mkdtemp()), data)
    agg = aggregate_step_success(p)
    assert agg["step_wise"][1]["break_count"] == 2  # 3 条中 2 条失败
    md = format_step_report(agg)
    assert "热步" in md


def test_aggregate_file_not_found():
    agg = aggregate_step_success("nonexistent.json")
    assert "error" in agg


def test_aggregate_no_chains():
    p = _write_json(Path(__import__("tempfile").mkdtemp()), {"chains": []})
    agg = aggregate_step_success(p)
    assert "error" in agg


def test_format_report_md():
    data = {"chains": [{"steps": [{"entry": {"status": "matched"}}]}]}
    p = _write_json(Path(__import__("tempfile").mkdtemp()), data)
    agg = aggregate_step_success(p)
    md = format_step_report(agg)
    assert "逐步成功率" in md
    assert "100.0%" in md

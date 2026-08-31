"""planner_eval_diff 单测（capability: planner-metric-diff，§8 双底座回归入口）。"""
from __future__ import annotations

import json

from tests.planner_eval_diff import (
    compare_planner, format_planner_diff, _rate, _load,
)


def test_rate_with_expected_denominator():
    assert _rate({"table_hit": 5, "expected": 40}, "table_hit") == 0.125


def test_rate_zero_denominator():
    assert _rate({"table_hit": 5, "expected": 0}, "table_hit") == 0.0


def test_compare_improves_direction():
    before = {"expected": 10, "actual": 3, "table_hit": 1, "sheet_hit": 0,
              "action_hit": 3, "full": 0, "keys_exp": 5, "keys_found": 2,
              "keys_ok": 1, "ph_total": 2, "ph_ok": 1, "ph_unresolved": 2,
              "loc_total": 0, "loc_ok": 0}
    after = {"expected": 10, "actual": 5, "table_hit": 3, "sheet_hit": 1,
             "action_hit": 5, "full": 1, "keys_exp": 6, "keys_found": 4,
             "keys_ok": 3, "ph_total": 2, "ph_ok": 2, "ph_unresolved": 0,
             "loc_total": 0, "loc_ok": 0}
    diff = compare_planner(before, after)
    by_metric = {r["metric"]: r for r in diff["rows"]}
    assert by_metric["table_hit_rate"]["status"] == "improved"
    assert by_metric["full_match_rate"]["status"] == "improved"
    assert by_metric["placeholder_unresolved"]["status"] == "improved"
    assert by_metric["actual_count"]["status"] == "neutral"
    assert diff["improved"] > diff["regressed"]


def test_compare_detects_regression():
    before = {"expected": 10, "actual": 5, "table_hit": 3, "sheet_hit": 0,
              "action_hit": 5, "full": 1, "keys_exp": 5, "keys_found": 4,
              "keys_ok": 3, "ph_total": 2, "ph_ok": 2, "ph_unresolved": 0,
              "loc_total": 0, "loc_ok": 0}
    after = {"expected": 10, "actual": 3, "table_hit": 1, "sheet_hit": 0,
             "action_hit": 3, "full": 0, "keys_exp": 5, "keys_found": 2,
             "keys_ok": 1, "ph_total": 2, "ph_ok": 1, "ph_unresolved": 2,
             "loc_total": 0, "loc_ok": 0}
    diff = compare_planner(before, after)
    by_metric = {r["metric"]: r for r in diff["rows"]}
    assert by_metric["table_hit_rate"]["status"] == "regressed"
    assert by_metric["keys_ok_rate"]["status"] == "regressed"


def test_format_contains_status():
    diff = compare_planner(
        {"expected": 10, "actual": 3, "table_hit": 1, "sheet_hit": 0,
         "action_hit": 3, "full": 0, "keys_exp": 5, "keys_found": 2,
         "keys_ok": 1, "ph_total": 0, "ph_ok": 0, "ph_unresolved": 0,
         "loc_total": 0, "loc_ok": 0},
        {"expected": 10, "actual": 5, "table_hit": 3, "sheet_hit": 1,
         "action_hit": 5, "full": 1, "keys_exp": 6, "keys_found": 4,
         "keys_ok": 3, "ph_total": 0, "ph_ok": 0, "ph_unresolved": 0,
         "loc_total": 0, "loc_ok": 0})
    md = format_planner_diff(diff)
    assert "table_hit_rate" in md
    assert "improved" in md
    assert "改进" in md


def test_load_raw_summary_shape(tmp_path):
    p = tmp_path / "raw.json"
    p.write_text(json.dumps({"summary": {"expected": 10, "actual": 5},
                             "results": []}), encoding="utf-8")
    assert _load(str(p))["expected"] == 10


def test_load_archive_wrapper_shape(tmp_path):
    p = tmp_path / "archived.json"
    p.write_text(json.dumps({"_header": {"run_id": "x"},
                             "results": {"summary": {"expected": 8, "actual": 3},
                                         "results": []}}), encoding="utf-8")
    assert _load(str(p))["expected"] == 8

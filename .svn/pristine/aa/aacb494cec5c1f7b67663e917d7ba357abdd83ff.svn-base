"""eval_baseline 单测（capability: eval-baseline-management）。"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.eval_baseline import (
    METRIC_WHITELIST, archive_run, compare_runs, format_diff_report,
    load_archived, make_run_id, prune_archives,
)


def test_make_run_id_format():
    rid = make_run_id()
    # YYYYMMDD_HHMMSS_<gitshort>
    parts = rid.split("_")
    assert len(parts) >= 3
    assert len(parts[0]) == 8  # date
    assert len(parts[1]) == 6  # time


def test_make_run_id_with_tag():
    rid = make_run_id(tag="before")
    assert rid.endswith("_before")


def test_archive_and_load(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.eval_baseline.ARCHIVE_DIR", tmp_path)
    result = {"summary": {"ok_rate": 0.8, "avg_elapsed_ms": 5000}}
    rid = archive_run("test_script", result, "# report", tag="t1")
    archived = load_archived("test_script", rid)
    assert archived is not None
    assert archived["_header"]["run_id"] == rid
    assert archived["results"]["summary"]["ok_rate"] == 0.8


def test_compare_runs_improved(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.eval_baseline.ARCHIVE_DIR", tmp_path)
    archive_run("s", {"summary": {"ok_rate": 0.80, "avg_elapsed_ms": 5000}}, "", tag="b")
    base_id = [p for p in tmp_path.glob("s_*.json")][0].stem[len("s_"):]
    archive_run("s", {"summary": {"ok_rate": 0.88, "avg_elapsed_ms": 4500}}, "", tag="c")
    curr_id = [p for p in tmp_path.glob("s_*.json") if "c" in p.stem][0].stem[len("s_"):]
    diff = compare_runs(base_id, curr_id, "s")
    ok_diff = [d for d in diff["diffs"] if d["metric"] == "ok_rate"][0]
    assert ok_diff["status"] == "improved"
    assert abs(ok_diff["delta"] - 0.08) < 1e-9
    ms_diff = [d for d in diff["diffs"] if d["metric"] == "avg_elapsed_ms"][0]
    assert ms_diff["status"] == "improved"  # 延迟降低=改进


def test_compare_runs_regressed(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.eval_baseline.ARCHIVE_DIR", tmp_path)
    archive_run("s", {"summary": {"ok_rate": 0.9}}, "", tag="b")
    base_id = [p for p in tmp_path.glob("s_*.json")][0].stem[len("s_"):]
    archive_run("s", {"summary": {"ok_rate": 0.85}}, "", tag="c")
    curr_id = [p for p in tmp_path.glob("s_*.json") if "c" in p.stem][0].stem[len("s_"):]
    diff = compare_runs(base_id, curr_id, "s")
    ok_diff = [d for d in diff["diffs"] if d["metric"] == "ok_rate"][0]
    assert ok_diff["status"] == "regressed"


def test_compare_runs_missing_metric_neutral(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.eval_baseline.ARCHIVE_DIR", tmp_path)
    archive_run("s", {"summary": {"ok_rate": 0.9}}, "", tag="b")
    base_id = [p for p in tmp_path.glob("s_*.json")][0].stem[len("s_"):]
    archive_run("s", {"summary": {"coverage": 0.8}}, "", tag="c")
    curr_id = [p for p in tmp_path.glob("s_*.json") if "c" in p.stem][0].stem[len("s_"):]
    diff = compare_runs(base_id, curr_id, "s")
    ok_diff = [d for d in diff["diffs"] if d["metric"] == "ok_rate"][0]
    assert ok_diff["status"] == "neutral"
    assert ok_diff["delta"] is None


def test_format_diff_report_md(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.eval_baseline.ARCHIVE_DIR", tmp_path)
    archive_run("s", {"summary": {"ok_rate": 0.8}}, "", tag="b")
    archive_run("s", {"summary": {"ok_rate": 0.9}}, "", tag="c")
    ids = sorted(p.stem[len("s_"):] for p in tmp_path.glob("s_*.json"))
    diff = compare_runs(ids[0], ids[1], "s")
    md = format_diff_report(diff)
    assert "ok_rate" in md
    assert "improved" in md


def test_prune_archives(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.eval_baseline.ARCHIVE_DIR", tmp_path)
    for i in range(10):
        archive_run("s", {"summary": {}}, "", run_id=f"20260101_00000{i}_nogit")
    n = prune_archives(keep=5)
    assert n == 5
    remaining = list(tmp_path.glob("s_*.json"))
    assert len(remaining) == 5


def test_error_type_distribution_diff(tmp_path, monkeypatch):
    monkeypatch.setattr("tests.eval_baseline.ARCHIVE_DIR", tmp_path)
    archive_run("s", {"summary": {"ok_rate": 0.8},
                      "error_type_distribution": {"type_mismatch": 15, "unknown": 5}}, "", tag="b")
    base_id = [p for p in tmp_path.glob("s_*.json")][0].stem[len("s_"):]
    archive_run("s", {"summary": {"ok_rate": 0.85},
                      "error_type_distribution": {"type_mismatch": 8, "unknown": 7}}, "", tag="c")
    curr_id = [p for p in tmp_path.glob("s_*.json") if "c" in p.stem][0].stem[len("s_"):]
    diff = compare_runs(base_id, curr_id, "s")
    etd = [d for d in diff["diffs"] if d["metric"] == "error_type_distribution"][0]
    tm = [e for e in etd["delta"] if e["error_type"] == "type_mismatch"][0]
    assert tm["delta"] == -7
    assert tm["status"] == "improved"

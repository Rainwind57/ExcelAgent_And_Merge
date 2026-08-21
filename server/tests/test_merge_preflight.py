"""方法 B pre_commit_hold 漏行预检单测。

验证 preflight_row_manifest：base 行 id 列 ∪ 合并结果行 id 列，
lost_ids = base_ids - mergeset_ids。ca-overview §2.3.1 漏行场景。
apply 路径集成（CODEMAKER_PREFLIGHT_HOLD 开关）由端到端覆盖，此处聚焦预检函数。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from engine.models import MergeRequest, SheetMergeData, RowData
from routers.precommit_hold import (
    preflight_row_manifest, PreflightReport, PreCommitHoldEvent,
)


def _make_mr(group: str, sheet_name: str, headers: list, row_keys: list) -> MergeRequest:
    """构造 MergeRequest：单 sheet，rows 用 row_keys 构造（key 字段）。"""
    rows = [RowData(key=str(k), cells=[], row_type="matched") for k in row_keys]
    s = SheetMergeData(name=sheet_name, headers=headers, rows=rows)
    return MergeRequest(group_name=group, sheets=[s])


def test_no_loss_no_hold():
    """base = {1,2,3}, mr = {1,2,3} → 无漏行，will_silently_drop=False。"""
    mr = _make_mr("g", "S1", ["id", "name"], [1, 2, 3])
    base_pks = {"S1": {"1", "2", "3"}}
    report = preflight_row_manifest(mr, base_pks)
    assert report.will_silently_drop is False
    assert report.lost_rows == []
    assert report.holds == []


def test_loss_detected():
    """base = {1,2,3}, mr = {1,2} → 漏行 id=3，hold 命中。"""
    mr = _make_mr("g", "S1", ["id", "name"], [1, 2])
    base_pks = {"S1": {"1", "2", "3"}}
    report = preflight_row_manifest(mr, base_pks)
    assert report.will_silently_drop is True
    assert len(report.lost_rows) == 1
    assert report.lost_rows[0]["id"] == "3"
    assert report.lost_rows[0]["sheet"] == "S1"
    assert report.lost_rows[0]["was_in_base"] is True
    assert len(report.holds) == 1
    assert report.holds[0].kind == "missing_rows"
    assert report.holds[0].count == 1
    assert "3" in report.holds[0].sheets["S1"]["lost_ids"]


def test_section_2_3_1_scenario():
    """ca-overview §2.3.1：base 有 id=10500，theirs 缺 → 命中 id=10500。"""
    mr = _make_mr("g", "S1", ["id"], [10501, 10502])
    base_pks = {"S1": {"10500", "10501", "10502"}}
    report = preflight_row_manifest(mr, base_pks)
    assert report.will_silently_drop is True
    lost_ids = [r["id"] for r in report.lost_rows]
    assert "10500" in lost_ids
    assert report.holds[0].recommendation  # 有建议文案


def test_multi_sheet_loss():
    """多 sheet 漏行：sheet1 缺 id=5, sheet2 缺 id=9 → holds 有 2 条。"""
    rows1 = [RowData(key="1", cells=[], row_type="matched"),
             RowData(key="2", cells=[], row_type="matched")]
    rows2 = [RowData(key="8", cells=[], row_type="matched")]
    s1 = SheetMergeData(name="S1", headers=["id"], rows=rows1)
    s2 = SheetMergeData(name="S2", headers=["id"], rows=rows2)
    mr = MergeRequest(group_name="g", sheets=[s1, s2])
    base_pks = {"S1": {"1", "2", "5"}, "S2": {"8", "9"}}
    report = preflight_row_manifest(mr, base_pks)
    assert report.will_silently_drop is True
    assert len(report.holds) == 2
    sheets_with_holds = {list(h.sheets.keys())[0] for h in report.holds}
    assert sheets_with_holds == {"S1", "S2"}


def test_base_pks_empty_skip():
    """sheet 不在 base_pks → 跳过，不报漏行。"""
    mr = _make_mr("g", "S1", ["id"], [1])
    base_pks = {}  # 无 S1
    report = preflight_row_manifest(mr, base_pks)
    assert report.will_silently_drop is False
    assert report.lost_rows == []


def test_event_to_dict_shape():
    """PreCommitHoldEvent.to_dict 含 type=pre_commit_hold 字段（SSE payload 格式）。"""
    ev = PreCommitHoldEvent(kind="missing_rows", severity="hold", count=1,
                            sheets={"S1": {"lost_ids": ["3"]}},
                            message="测试", recommendation="override")
    d = ev.to_dict()
    assert d["type"] == "pre_commit_hold"
    assert d["kind"] == "missing_rows"
    assert d["severity"] == "hold"
    assert d["count"] == 1


def test_report_to_dict_serializable():
    """PreflightReport.to_dict 可 JSON 序列化（apply 409 detail 须可序列化）。"""
    import json
    mr = _make_mr("g", "S1", ["id"], [1])
    base_pks = {"S1": {"1", "2"}}
    report = preflight_row_manifest(mr, base_pks)
    d = report.to_dict()
    s = json.dumps(d, ensure_ascii=False)
    assert "pre_commit_hold" in s

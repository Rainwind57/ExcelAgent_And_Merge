"""4.1/4.2/4.3 向量化 compare 正确性:None/混合类型 + sparse 默认 True + 多衍生广播。"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


def test_vectorized_none_and_mixed_types():
    """None/空串/数字/字符串混合:向量化判等与 _semantic_eq 一致,无误判。"""
    from engine.compare import _compare_sheet_vectorized, _semantic_eq

    base = "base.xlsx"
    other = "dev.xlsx"
    all_files = [base, other]
    headers = ["id", "col_a", "col_b", "col_c"]
    # 行1: None vs "" (语义相等) + 100 vs "100"(数值相等) + "x" vs "x"(相等) → 无 diff
    # 行2: None vs 1 (不等) + "y" vs "z"(不等) → 有 diff
    file_rows = {
        base: [["1", None, "100", "x"], ["2", None, "y", "p"]],
        other: [["1", "", 100, "x"], ["2", 1, "z", "p"]],
    }
    result = _compare_sheet_vectorized(
        file_rows, base, [other], all_files, headers,
        structure_diff=None, sparse=False, merge_base_file=None,
        commit_authors=None,
    )
    assert result is not None
    rows = result["rows"]
    # 行1 全语义相等 → matched,无 changed/conflict
    r1 = [c for c in rows[0]["cells"] if c.get("changed") or c.get("conflict")]
    assert r1 == [], "行1 None/空/数值归一应无 diff"
    # 行2 col_a(None vs 1)、col_b(y vs z) 有 diff
    r2_changed = {c["col"]: c for c in rows[1]["cells"] if c.get("changed") or c.get("conflict")}
    assert 1 in r2_changed, "col_a None vs 1 应判 diff"
    assert 2 in r2_changed, "col_b y vs z 应判 diff"
    # col_c(p vs p) 无 diff
    assert 3 not in r2_changed, "col_c 相等应无 diff"
    # 交叉验证 _semantic_eq
    assert _semantic_eq(None, "") is True
    assert _semantic_eq("100", 100) is True
    assert _semantic_eq(None, 1) is False


def test_vectorized_multi_other_broadcast():
    """多衍生文件:has_diff 为任一衍生与 base 不同的 OR。"""
    from engine.compare import _compare_sheet_vectorized

    base = "base.xlsx"
    others = ["d1.xlsx", "d2.xlsx"]
    all_files = [base] + others
    headers = ["id", "col"]
    # 行1: d1 改了(base=a, d1=b, d2=a) → diff;d2 与 base 同
    # 行2: 全等 → 无 diff
    file_rows = {
        base: [["1", "a"], ["2", "k"]],
        "d1.xlsx": [["1", "b"], ["2", "k"]],
        "d2.xlsx": [["1", "a"], ["2", "k"]],
    }
    result = _compare_sheet_vectorized(
        file_rows, base, others, all_files, headers,
        structure_diff=None, sparse=False, merge_base_file=None,
        commit_authors=None,
    )
    r1_changed = [c for c in result["rows"][0]["cells"] if c.get("changed") or c.get("conflict")]
    assert len(r1_changed) >= 1, "行1 d1 改动应检出"
    r2_changed = [c for c in result["rows"][1]["cells"] if c.get("changed") or c.get("conflict")]
    assert r2_changed == [], "行2 全等应无 diff"


def test_compare_sheet_sparse_default_true():
    """compare_sheet 的 sparse 默认 True:未指定 → 全等行只 PK 格。"""
    import inspect
    from engine.compare import compare_sheet

    sig = inspect.signature(compare_sheet)
    assert sig.parameters["sparse"].default is True, "sparse 默认应为 True"

    base = "base.xlsx"
    file_sheets = {
        base: {"S1": [["id", "v"], ["1", "a"]]},
        "d.xlsx": {"S1": [["id", "v"], ["1", "a"]]},
    }
    # 不传 sparse → 默认 True → 全等行只 1 格(PK)
    r = compare_sheet(file_sheets, base, "S1")
    assert r["rows"][0]["row_type"] == "matched"
    assert len(r["rows"][0]["cells"]) == 1, "默认 sparse=True 全等行应只 PK 格"
    # 显式 sparse=False → 全量物化(4 格)
    r2 = compare_sheet(file_sheets, base, "S1", sparse=False)
    assert len(r2["rows"][0]["cells"]) == 2, "sparse=False 应全量物化"

"""matcher 语义校验单测（capability: table-routing-schema D3 / 5.1-5.6）。

验证 ColumnMatcher.match_best key↔value 语义校验：
- 5.1 int 列+字符串值→降权 0.5
- 5.2 int 列+字符串值+枚举命中→不降权
- 5.3 str 列+int 值→不降权
- 5.4 主名称列+值含中文且非枚举→提权 1.2
- 5.5 match_best 返回附 semantic_warning
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.column_matcher import ColumnMatcher, ColumnMatch


class _FakeER:
    def __init__(self, mapping: dict):
        self._map = mapping

    def resolve_label(self, stem, sheet, col, label):
        return self._map.get((stem, sheet, col), {}).get(label)


def _make_matcher(headers, col_type_map, enum_map=None):
    matcher = ColumnMatcher(headers)
    col_type_fn = lambda stem, sheet, col: col_type_map.get((stem, sheet, col), "")
    er = _FakeER(enum_map or {})
    return matcher, col_type_fn, er


class TestSemanticCheck:
    def test_int_col_string_value_downweighted(self):
        matcher, ctf, er = _make_matcher(
            ["类型", "名字"],
            {("pet", "Pet", "类型"): "int"})
        m = matcher.match_best("类型", value="攻击", stem="pet", sheet="Pet",
                               col_type_fn=ctf, enum_resolver=er)
        assert m is not None
        # 原阶段2子串匹配 score=0.9，降权后 0.45
        assert m.score <= 0.5
        assert "int" in m.semantic_warning
        assert "攻击" in m.semantic_warning

    def test_int_col_string_value_enum_hit_not_downweighted(self):
        matcher, ctf, er = _make_matcher(
            ["类型"],
            {("pet", "Pet", "类型"): "int"},
            enum_map={("pet", "Pet", "类型"): {"攻击": 1}})
        m = matcher.match_best("类型", value="攻击", stem="pet", sheet="Pet",
                               col_type_fn=ctf, enum_resolver=er)
        assert m is not None
        # 枚举命中不降权，score 保持 0.9
        assert m.score >= 0.85
        assert m.semantic_warning == ""

    def test_str_col_int_value_not_downweighted(self):
        matcher, ctf, er = _make_matcher(
            ["名字"],
            {("pet", "Pet", "名字"): "str"})
        m = matcher.match_best("名字", value=123, stem="pet", sheet="Pet",
                               col_type_fn=ctf, enum_resolver=er)
        assert m is not None
        # str 列 + int 值不降权
        assert m.score >= 0.85
        assert m.semantic_warning == ""

    def test_name_col_chinese_value_upweighted(self):
        matcher, ctf, er = _make_matcher(
            ["宠物名称"],
            {("pet", "Pet", "宠物名称"): "str"})
        m = matcher.match_best("宠物名称", value="小白龙", stem="pet", sheet="Pet",
                               col_type_fn=ctf, enum_resolver=er)
        assert m is not None
        # 主名称列提权 1.2（阶段2 score 0.9 * 1.2 = 1.08 → cap 1.0）
        assert m.score >= 0.9

    def test_no_value_skips_semantic_check(self):
        """未传 value → 不做语义校验，行为同旧。"""
        matcher, ctf, er = _make_matcher(
            ["类型"], {("pet", "Pet", "类型"): "int"})
        m = matcher.match_best("类型", stem="pet", sheet="Pet",
                               col_type_fn=ctf, enum_resolver=er)
        assert m is not None
        assert m.semantic_warning == ""
        assert m.score >= 0.85  # 未降权

    def test_no_col_type_fn_skips_semantic_check(self):
        matcher, _, er = _make_matcher(["类型"], {})
        m = matcher.match_best("类型", value="攻击", stem="pet", sheet="Pet",
                               col_type_fn=None, enum_resolver=er)
        assert m is not None
        assert m.semantic_warning == ""

    def test_semantic_warning_field_default_empty(self):
        """ColumnMatch.semantic_warning 默认空串。"""
        m = ColumnMatch(column="x", score=0.9, index=1, source="dict")
        assert m.semantic_warning == ""


class TestRapidFuzzMatching:
    """阶段3 rapidfuzz 语义加权匹配（capability: column-matching-accuracy）。"""

    def test_fuzzy_simplified_colname_hits(self):
        """去括号简写/typo 命中表头（阶段3 语义加权，rapidfuzz 不可用回退 similarity）。"""
        from agent.excel.column_matcher import _HAS_RAPIDFUZZ
        m = ColumnMatcher(["销售总额(元)", "名称", "类型"])
        r = m.match("销售总额元")
        assert r is not None
        assert r.column == "销售总额(元)"
        assert r.source in ("rapidfuzz", "similarity")
        if _HAS_RAPIDFUZZ:
            assert r.score >= 0.6

    def test_partial_prefix_colname_hits(self):
        """短前缀/部分匹配命中（如「技能」→「技能id」）。"""
        m = ColumnMatcher(["技能id", "名称", "类型"])
        r = m.match("技能")
        assert r is not None
        assert r.column == "技能id"


# ── 入口 ─────────────────────────────────────────────────────

def _run_all():
    pytest.main([__file__, "-v"])


if __name__ == "__main__":
    _run_all()

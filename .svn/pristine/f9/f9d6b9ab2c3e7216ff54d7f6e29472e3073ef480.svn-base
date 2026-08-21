"""skill_context + schema_infer 枚举发现单测。

覆盖：
- 2.5 法术关键词命中路由；无路由命中退化全表列名
- 3.5 int 列标注类型；主名称列标注
- 8.1-8.4 explain sheet 三种格式识别 + 向后兼容
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.skill_context import (
    pre_route, build_skill_context, _format_column_types_block,
    reset_skill_context_cache,
)


# ── 2.5 路由命中 + 退化 ──────────────────────────────────────

class TestRoute:
    def setup_method(self):
        reset_skill_context_cache()

    def teardown_method(self):
        reset_skill_context_cache()

    def test_spell_keyword_hits_spell_stem(self):
        # D1: "法术" → spell 路由
        stems = pre_route("添加法术 烈火术")
        assert "spell" in stems

    def test_element_keyword_hits_spell(self):
        stems = pre_route("修改元素 火属性")
        assert "spell" in stems

    def test_state_keyword_hits_exclusive_state(self):
        stems = pre_route("添加状态 打坐")
        assert "exclusive_state" in stems

    def test_no_route_hit_returns_empty(self):
        # 无任何路由关键词命中
        stems = pre_route("随机无关文本xyz123")
        assert stems == []

    def test_degrade_to_all_table_columns_when_no_route(self, monkeypatch):
        # mock _all_columns 返回固定表集
        from agent.excel import skill_context as sc_mod
        fake_cols = {
            "aaa_sheet": {"SheetA": ["col1", "col2"]},
            "bbb_sheet": {"SheetB": ["x"]},
        }
        monkeypatch.setattr(sc_mod, "_all_columns", lambda: fake_cols, raising=False)
        # known_stems 会包含 fake stem
        ctx = build_skill_context("随机无关文本xyz123")
        # 退化注入全表列名候选（按字母序前 8，这里 2 张全注入）
        assert "aaa_sheet" in ctx
        assert "col1" in ctx

    def test_sheet_name_registered_as_keyword(self, monkeypatch):
        """2.3: sheet 名补齐为关键词 → 命中路由。"""
        from agent.excel import skill_context as sc_mod
        fake_cols = {"pet": {"Pet": ["宠物id", "名字"]}}
        monkeypatch.setattr(sc_mod, "_all_columns", lambda: fake_cols, raising=False)
        reset_skill_context_cache()
        # "Pet" sheet 名应命中 pet stem
        stems = pre_route("查 Pet 表")
        assert "pet" in stems

    def test_name_column_registered_as_keyword(self, monkeypatch):
        """2.3: 主名称列名补齐为关键词 → 命中路由。"""
        from agent.excel import skill_context as sc_mod
        fake_cols = {"custom_table": {"CustomSheet": ["编号", "道具名称"]}}
        monkeypatch.setattr(sc_mod, "_all_columns", lambda: fake_cols, raising=False)
        reset_skill_context_cache()
        # "道具名称" 列名应命中 custom_table stem
        stems = pre_route("改 道具名称")
        assert "custom_table" in stems

    def test_non_name_column_not_registered(self, monkeypatch):
        """2.3: 普通列名不注册（避免短列名噪声误命中）。"""
        from agent.excel import skill_context as sc_mod
        fake_cols = {"xxx_table": {"XSheet": ["普通字段", "描述"]}}
        monkeypatch.setattr(sc_mod, "_all_columns", lambda: fake_cols, raising=False)
        reset_skill_context_cache()
        # "描述" 非名称列 → 不注册 → 不命中
        stems = pre_route("改 描述")
        assert "xxx_table" not in stems


# ── 3.5 列类型 schema 标注 ───────────────────────────────────

class TestColumnTypesBlock:
    def test_int_column_annotated_with_type(self, monkeypatch):
        from agent.excel import skill_context as sc_mod

        def fake_load_yaml(name):
            if name == "value_constraints.yaml":
                return {"tables": {"pet": {"Pet": {
                    "类型": {"type": "int"},
                    "名字": {"type": "str"},
                }}}}
            if name == "enum_mappings.yaml":
                return {"tables": {}}
            return {}

        monkeypatch.setattr(sc_mod, "_load_yaml", fake_load_yaml, raising=False)
        # _format_column_types_block 内 `from .skill_loader import _load_yaml`
        import agent.excel.skill_loader as sl_mod
        monkeypatch.setattr(sl_mod, "_load_yaml", fake_load_yaml, raising=False)

        block = _format_column_types_block(["pet"])
        assert "类型: int" in block
        assert "pet[Pet]" in block

    def test_name_column_marked(self, monkeypatch):
        from agent.excel import skill_context as sc_mod
        import agent.excel.skill_loader as sl_mod

        def fake_load_yaml(name):
            if name == "value_constraints.yaml":
                return {"tables": {"pet": {"Pet": {
                    "名字": {"type": "str"},
                    "宠物名称": {"type": "str"},
                    "类型": {"type": "int"},
                }}}}
            return {"tables": {}} if name == "enum_mappings.yaml" else {}

        monkeypatch.setattr(sc_mod, "_load_yaml", fake_load_yaml, raising=False)
        monkeypatch.setattr(sl_mod, "_load_yaml", fake_load_yaml, raising=False)

        block = _format_column_types_block(["pet"])
        # 名字/名称 列 + str → 标 [主名称列]
        assert "名字: str [主名称列]" in block
        assert "宠物名称: str [主名称列]" in block
        # int 列不标主名称列
        assert "类型: int [主名称列]" not in block

    def test_enum_values_annotated(self, monkeypatch):
        from agent.excel import skill_context as sc_mod
        import agent.excel.skill_loader as sl_mod

        def fake_load_yaml(name):
            if name == "value_constraints.yaml":
                return {"tables": {"pet": {"Pet": {"类型": {"type": "int"}}}}}
            if name == "enum_mappings.yaml":
                return {"tables": {"pet": {"Pet": {"columns": {"类型": {
                    "type": "int",
                    "values": [{"label": "攻击", "value": 1}, {"label": "治疗", "value": 2}],
                }}}}}}
            return {}

        monkeypatch.setattr(sc_mod, "_load_yaml", fake_load_yaml, raising=False)
        monkeypatch.setattr(sl_mod, "_load_yaml", fake_load_yaml, raising=False)

        block = _format_column_types_block(["pet"])
        assert "类型: int" in block
        assert "枚举" in block
        assert "攻击=1" in block


# ── 8.1-8.4 explain sheet 枚举发现 ───────────────────────────

@pytest.fixture
def has_openpyxl():
    try:
        import openpyxl
        return True
    except ImportError:
        return False


def _build_enum_xlsx(path: Path, fmt: str):
    """构造测试 xlsx。fmt: 'horizontal' | 'vertical_two_col' | 'single_keyval' | 'compat'."""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "说明" if fmt != "non_explain_title" else "EnumSheet"
    if fmt in ("horizontal", "compat"):
        # A1=列名, B1=label1, C1=label2; B2=1, C2=2
        ws["A1"] = "类型"
        ws["B1"] = "攻击"
        ws["C1"] = "治疗"
        ws["B2"] = 1
        ws["C2"] = 2
    elif fmt == "vertical_two_col":
        # A1=列名(表头), A2+=label, B2+=value
        ws["A1"] = "类型"
        ws["A2"] = "攻击"
        ws["B2"] = 1
        ws["A3"] = "治疗"
        ws["B3"] = 2
        ws["A4"] = "防御"
        ws["B4"] = 3
    elif fmt == "single_keyval":
        # A1=列名, A2+="label:value"
        ws["A1"] = "类型"
        ws["A2"] = "攻击:1"
        ws["A3"] = "治疗:2"
        ws["A4"] = "防御:3"
    elif fmt == "single_keyval_eq":
        ws["A1"] = "类型"
        ws["A2"] = "攻击=1"
        ws["A3"] = "治疗=2"
    elif fmt == "non_explain_title":
        # title 不含说明类词但也不是业务 sheet → 仍应被识别为候选
        ws["A1"] = "类型"
        ws["A2"] = "攻击"
        ws["B2"] = 1
        ws["A3"] = "治疗"
        ws["B3"] = 2
    wb.save(path)
    wb.close()


def _make_table_meta(path: Path, stem: str, col_name: str = "类型"):
    """构造最小 TableMeta，含一个 int 列 col_name。"""
    from agent.excel.schema_infer import TableMeta, SheetMeta, ColumnMeta
    tm = TableMeta(stem=stem, path=path)
    sm = SheetMeta(name="Pet")
    sm.columns = [ColumnMeta(index=0, header=col_name, clean_name=col_name, col_type="int")]
    tm.sheets["Pet"] = sm
    # 说明 sheet 也进 sheets（但 data_sheets 会排除"说明"开头）
    tm.sheets["说明"] = SheetMeta(name="说明")
    return tm


class TestExtractEnumsFromSheet:
    def test_horizontal_format_compat(self, tmp_path, has_openpyxl):
        if not has_openpyxl:
            pytest.skip("openpyxl unavailable")
        import openpyxl
        p = tmp_path / "pet.xlsx"
        _build_enum_xlsx(p, "horizontal")
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb["说明"]
        from agent.excel.schema_infer import _extract_enums_from_sheet
        out = _extract_enums_from_sheet(ws)
        wb.close()
        # 应识别 col_name=类型 + 2 条
        assert any(c == "类型" for c, _ in out)
        col, entries = next((c, e) for c, e in out if c == "类型")
        assert {"label": "攻击", "value": 1} in entries
        assert {"label": "治疗", "value": 2} in entries

    def test_vertical_two_col_format(self, tmp_path, has_openpyxl):
        if not has_openpyxl:
            pytest.skip("openpyxl unavailable")
        import openpyxl
        p = tmp_path / "pet.xlsx"
        _build_enum_xlsx(p, "vertical_two_col")
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb["说明"]
        from agent.excel.schema_infer import _extract_enums_from_sheet
        out = _extract_enums_from_sheet(ws)
        wb.close()
        col, entries = next((c, e) for c, e in out if c == "类型")
        assert len(entries) == 3
        labels = {e["label"] for e in entries}
        assert {"攻击", "治疗", "防御"} == labels

    def test_single_keyval_colon_format(self, tmp_path, has_openpyxl):
        if not has_openpyxl:
            pytest.skip("openpyxl unavailable")
        import openpyxl
        p = tmp_path / "pet.xlsx"
        _build_enum_xlsx(p, "single_keyval")
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb["说明"]
        from agent.excel.schema_infer import _extract_enums_from_sheet
        out = _extract_enums_from_sheet(ws)
        wb.close()
        col, entries = next((c, e) for c, e in out if c == "类型")
        assert len(entries) == 3
        assert {"攻击": 1, "治疗": 2, "防御": 3} == {e["label"]: e["value"] for e in entries}

    def test_single_keyval_eq_format(self, tmp_path, has_openpyxl):
        if not has_openpyxl:
            pytest.skip("openpyxl unavailable")
        import openpyxl
        p = tmp_path / "pet.xlsx"
        _build_enum_xlsx(p, "single_keyval_eq")
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb["说明"]
        from agent.excel.schema_infer import _extract_enums_from_sheet
        out = _extract_enums_from_sheet(ws)
        wb.close()
        col, entries = next((c, e) for c, e in out if c == "类型")
        assert len(entries) == 2

    def test_vertical_two_col_dedup(self, tmp_path, has_openpyxl):
        """竖排两列重复 label 行去重。"""
        if not has_openpyxl:
            pytest.skip("openpyxl unavailable")
        import openpyxl
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "说明"
        ws["A1"] = "类型"
        ws["A2"] = "攻击"
        ws["B2"] = 1
        ws["A3"] = "治疗"
        ws["B3"] = 2
        ws["A4"] = "攻击"  # 重复
        ws["B4"] = 1
        p = tmp_path / "pet.xlsx"
        wb.save(p)
        wb.close()
        wb = openpyxl.load_workbook(p, data_only=True)
        ws = wb["说明"]
        from agent.excel.schema_infer import _extract_enums_from_sheet
        out = _extract_enums_from_sheet(ws)
        wb.close()
        col, entries = next((c, e) for c, e in out if c == "类型")
        labels = [e["label"] for e in entries]
        # 攻击只出现一次
        assert labels.count("攻击") == 1
        assert set(labels) == {"攻击", "治疗"}


class TestDiscoverEnumExplainSheet:
    def test_non_explain_title_still_scanned(self, tmp_path, has_openpyxl):
        """8.1: title 不含'说明'但为辅助 sheet（非 data_sheets）→ 仍识别。"""
        if not has_openpyxl:
            pytest.skip("openpyxl unavailable")
        p = tmp_path / "pet.xlsx"
        _build_enum_xlsx(p, "non_explain_title")
        from agent.excel.schema_infer import _discover_enum_from_explain_sheet, TableMeta, SheetMeta
        tm = TableMeta(stem="pet", path=p)
        sm = SheetMeta(name="Pet")
        from agent.excel.schema_infer import ColumnMeta
        sm.columns = [ColumnMeta(index=0, header="类型", clean_name="类型", col_type="int")]
        tm.sheets["Pet"] = sm
        tm.sheets["EnumSheet"] = SheetMeta(name="EnumSheet")  # 辅助 sheet
        tables = {"pet": tm}
        out = _discover_enum_from_explain_sheet(tables)
        assert "pet" in out
        assert "Pet" in out["pet"]
        vals = out["pet"]["Pet"]["columns"]["类型"]["values"]
        assert {"攻击": 1, "治疗": 2} == {v["label"]: v["value"] for v in vals}

    def test_business_data_sheet_not_misjudged(self, tmp_path, has_openpyxl):
        """业务 sheet（在 data_sheets 中 + title 无说明词）不被误扫。"""
        if not has_openpyxl:
            pytest.skip("openpyxl unavailable")
        import openpyxl
        p = tmp_path / "pet.xlsx"
        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Pet"  # 业务 sheet 名，无说明词
        # 伪装成格式2（会误判为枚举若被扫）
        ws["A1"] = "类型"
        ws["A2"] = "攻击"
        ws["B2"] = 1
        ws["A3"] = "治疗"
        ws["B3"] = 2
        wb.save(p)
        wb.close()
        from agent.excel.schema_infer import _discover_enum_from_explain_sheet, TableMeta, SheetMeta, ColumnMeta
        tm = TableMeta(stem="pet", path=p)
        sm = SheetMeta(name="Pet")
        sm.columns = [ColumnMeta(index=0, header="类型", clean_name="类型", col_type="int")]
        tm.sheets["Pet"] = sm  # Pet 既是业务 sheet 又是唯一 sheet
        tables = {"pet": tm}
        out = _discover_enum_from_explain_sheet(tables)
        # Pet 是 data_sheets 成员 + title 无说明词 → 不扫 → 空
        assert out == {}


# ── 入口 ─────────────────────────────────────────────────────

def _run_all():
    pytest.main([__file__, "-v"])


if __name__ == "__main__":
    _run_all()

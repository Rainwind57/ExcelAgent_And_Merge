"""针对 2026-08-17 修复的针对性单测：
- _is_listing_sheet / _is_business_sheet：列举 sheet 守卫
- _col_types_by_header：列类型按前缀匹配到实际表头
- _classify_placeholder_fields：占位符 auto（留空）vs required（弹问）拆分
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from agent.excel.core import agent as agent_mod
from agent.excel.core.agent import (
    _is_listing_sheet,
    _is_business_sheet,
    _col_types_by_header,
    _classify_placeholder_fields,
    _is_auto_placeholder,
)


# ── _col_types_by_header：用 monkeypatch 注入固定 vc，不依赖 auto-gen yaml ──
_MOCK_VC = {
    "building": {
        "BuildingType": {"columns": {
            "建筑类型": {"type": "int"},
            "建筑名称": {"type": "string"},
            "地图图标": {"type": "int"},
        }},
        "BuildingLevelInit": {"columns": {
            "建筑类型": {"type": "int"},
            "城池等级": {"type": "int"},
            "建筑初始等级": {"type": "int"},
            "建筑最大等级": {"type": "int"},
        }},
    },
}


@pytest.fixture
def patched_vc(monkeypatch):
    monkeypatch.setattr(agent_mod, "_load_value_constraints", lambda: _MOCK_VC)
    return _MOCK_VC


# ── _is_listing_sheet ─────────────────────────────────────────
def test_listing_sheet_prefixes_match():
    assert _is_listing_sheet("当前可用的Anim_Montage_Sequence")
    assert _is_listing_sheet("当前可用的外观Graph")
    assert _is_listing_sheet("当前可用的驱动类型")


def test_listing_sheet_list_suffix_matches():
    assert _is_listing_sheet("可用列表")
    assert _is_listing_sheet("可选列表")
    assert _is_listing_sheet("某资源列表")


def test_listing_sheet_business_sheet_not_flagged():
    """裸「可用」「可选」前缀不再误判（避免误伤「可用资源表」等真业务表）。"""
    assert not _is_listing_sheet("BuildingType")
    assert not _is_listing_sheet("ResidenceBuilding")
    assert not _is_listing_sheet("Ability")
    assert not _is_listing_sheet("可用资源表")
    assert not _is_listing_sheet("可选道具")
    assert not _is_listing_sheet("")
    assert not _is_listing_sheet(None)  # type: ignore[arg-type]


def test_business_sheet_still_works():
    assert _is_business_sheet("BuildingType")
    assert not _is_business_sheet("config")
    assert not _is_business_sheet("程序用勿删")


# ── _col_types_by_header ─────────────────────────────────────
def test_col_types_matches_verbose_header_with_newline_suffix(patched_vc):
    """实际表头带 \\n（和代码中...）后缀，vc key 是干净短名 → 按核心名前缀命中。"""
    h = ["建筑类型\n（和代码中枚举值保持一致）", "建筑名称", "地图图标", "建筑初始等级"]
    ct = _col_types_by_header("building", "BuildingType", h)
    assert ct["建筑类型\n（和代码中枚举值保持一致）"] == "int"
    assert ct["建筑名称"] == "string"
    assert ct["地图图标"] == "int"
    # BuildingType sheet 无 建筑初始等级 列 → 不进 dict
    assert "建筑初始等级" not in ct


def test_col_types_building_level_init_all_int(patched_vc):
    h = ["建筑类型\n（和代码中枚举值保持一致）", "城池等级", "建筑初始等级", "建筑最大等级"]
    ct = _col_types_by_header("building", "BuildingLevelInit", h)
    assert all(t == "int" for t in ct.values())
    assert set(ct.values()) == {"int"}


def test_col_types_missing_table_returns_empty(patched_vc):
    assert _col_types_by_header("nonexistent_table", "X", ["a", "b"]) == {}
    assert _col_types_by_header("building", "BuildingType", []) == {}


def test_col_types_effect_code_column_not_matched(patched_vc):
    """效果码列（3001: 战斗ID）不在 vc 干净名内 → 不进类型 dict。"""
    h = ["3001: 战斗ID", "建筑名称"]
    ct = _col_types_by_header("building", "BuildingType", h)
    assert "3001: 战斗ID" not in ct
    assert ct.get("建筑名称") == "string"



# ── _classify_placeholder_fields ─────────────────────────────
def test_classify_pure_auto_fields():
    """全部 <auto> → 全归 auto，required 空。"""
    fields = {"技能id": "<auto>", "图标": "<auto>", "描述": "<auto>"}
    auto, req = _classify_placeholder_fields(fields)
    assert set(auto) == {"技能id", "图标", "描述"}
    assert req == []


def test_classify_pure_required_fields():
    """全部跨表引用占位 → 全归 required，auto 空。"""
    fields = {"建筑类型编号": "<new_buildingtype_id>", "道具id": "<new_item_id>"}
    auto, req = _classify_placeholder_fields(fields)
    assert auto == []
    assert set(req) == {"建筑类型编号", "道具id"}


def test_classify_mixed_fields():
    """混合：auto 列静默，required 列弹问。"""
    fields = {
        "建筑类型编号": "<new_buildingtype_id>",  # 跨表引用 → required
        "角色蒙太奇": "<auto>",                    # 用户没提 → auto 留空
        "建筑状态变量值": "<auto>",                # auto
        "是否软停止": "<auto>",                    # auto
        "名称": "炎爆术",                          # 真实值，不进任一
        "技能等级": 3,                             # 非字符串，跳过
    }
    auto, req = _classify_placeholder_fields(fields)
    assert set(auto) == {"角色蒙太奇", "建筑状态变量值", "是否软停止"}
    assert req == ["建筑类型编号"]


def test_classify_no_placeholders():
    """无占位 → 两边都空。"""
    fields = {"名称": "火球术", "等级": 3}
    auto, req = _classify_placeholder_fields(fields)
    assert auto == []
    assert req == []


def test_is_auto_placeholder_edge_cases():
    assert _is_auto_placeholder("<auto>")
    assert _is_auto_placeholder(" <auto> ")
    assert _is_auto_placeholder("< auto >")
    assert not _is_auto_placeholder("<new_pet_id>")
    assert not _is_auto_placeholder("<auto>（待补）")  # 带注解 → 非 pure auto → required
    assert not _is_auto_placeholder("火球术")
    assert not _is_auto_placeholder(123)  # type: ignore[arg-type]


def test_classify_none_and_non_dict_safe():
    assert _classify_placeholder_fields(None) == ([], [])  # type: ignore[arg-type]
    assert _classify_placeholder_fields("not a dict") == ([], [])  # type: ignore[arg-type]
    assert _classify_placeholder_fields({}) == ([], [])


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v"])

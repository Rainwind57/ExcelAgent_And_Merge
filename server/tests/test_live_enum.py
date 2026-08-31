"""live_enum 现场枚举发现 + rules enum_map 覆盖层单测。

验证零配置可迁移性：不依赖预生成的 L1 enum_mappings.yaml，
label→code 从工作区 xlsx 现场解析；用户业务规则 enum_map 优先级最高。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.live_enum import (
    LiveEnumSource,
    resolve_label_full,
    resolve_enum_label,
    _norm_col,
)


def _build_item_workbook(tmp_path: Path) -> Path:
    import openpyxl
    p = tmp_path / "item.xlsx"
    wb = openpyxl.Workbook()
    # 类型表
    ws = wb.active
    ws.title = "ItemType"
    ws.append(["道具类型", "类型名称"])
    ws.append(["item_type:int", "type_name:string"])
    for code, name in [(1, "资源"), (2, "礼包"), (3, "药品"), (4, "宝石")]:
        ws.append([code, name])
    # 说明表（块状）
    ws2 = wb.create_sheet("道具表说明")
    ws2.append(["道具品质", "凡品", "良品", "上品", "珍品", "绝品"])
    ws2.append(["配置数字", 1, 2, 3, 4, 5])
    wb.save(p)
    wb.close()
    return p


class _Cli:
    def __init__(self, path: Path):
        self.workspace = path.parent
        self._path = path

    def list_tables(self):
        return [self._path]


def test_live_enum_type_sheet_lookup(tmp_path):
    p = _build_item_workbook(tmp_path)
    live = LiveEnumSource(_Cli(p))
    assert live.lookup("item", "ItemBase", "道具类型", "资源") == 1
    assert live.lookup("item", "ItemBase", "道具类型", "药品") == 3


def test_live_enum_explain_sheet_block_lookup(tmp_path):
    p = _build_item_workbook(tmp_path)
    live = LiveEnumSource(_Cli(p))
    assert live.lookup("item", "ItemBase", "品质", "良品") == 2
    assert live.lookup("item", "ItemBase", "品质", "绝品") == 5


def test_live_enum_miss_returns_none(tmp_path):
    p = _build_item_workbook(tmp_path)
    live = LiveEnumSource(_Cli(p))
    assert live.lookup("item", "ItemBase", "品质", "不存在") is None


def test_resolve_chain_rules_override_live(tmp_path):
    p = _build_item_workbook(tmp_path)
    live = LiveEnumSource(_Cli(p))
    # 规则里「良品」被用户显式改映射成 9，应优先于现场发现的 2
    rules = {"item": {"ItemBase": {"品质": {"良品": 9}}}}
    assert resolve_enum_label(
        "item", "ItemBase", "品质", "良品", resolver=None, live=live, rules=rules) == 9
    # 规则未覆盖的标签走现场发现
    assert resolve_enum_label(
        "item", "ItemBase", "品质", "上品", resolver=None, live=live, rules=rules) == 3


def test_resolve_chain_falls_back_to_resolver(tmp_path):
    p = _build_item_workbook(tmp_path)
    live = LiveEnumSource(_Cli(p))

    class _Res:
        def resolve_label(self, stem, sheet, col, label):
            return 42 if label == "某标签" else None

    assert resolve_enum_label(
        "item", "ItemBase", "品质", "某标签", resolver=_Res(), live=live, rules=None) == 42


def test_norm_col_normalization():
    assert _norm_col("道具类型") == "道具类型"
    assert _norm_col("item_type:int") == "item_type"
    assert _norm_col("装备部位\n（和代码一致）") == "装备部位"


def test_rules_enum_map_overlay(monkeypatch, tmp_path):
    from agent.excel.core import rules_loader
    rules_dir = tmp_path / "rules" / "validate"
    rules_dir.mkdir(parents=True)
    (rules_dir / "item.md").write_text(
        "```yaml\n"
        "tables:\n"
        "  item:\n"
        "    ItemBase:\n"
        "      columns:\n"
        "        quality:\n"
        "          type: int\n"
        "          enum_map: {凡品: 1, 良品: 2, 上品: 3}\n"
        "        item_type:\n"
        "          enum_map:\n"
        "            - {label: 资源, value: 1}\n"
        "            - {label: 礼包, value: 2}\n"
        "```\n", encoding="utf-8")
    monkeypatch.setattr(rules_loader, "_VALIDATE_DIR", rules_dir, raising=False)
    rules_loader.reset_cache()
    overlay = rules_loader.get_enum_map_overlay()
    assert overlay["item"]["ItemBase"]["quality"] == {"凡品": 1, "良品": 2, "上品": 3}
    assert overlay["item"]["ItemBase"]["item_type"] == {"资源": 1, "礼包": 2}

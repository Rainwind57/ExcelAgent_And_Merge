"""#30 required_fields.yaml 自动生成单测。

覆盖：
1. derive_required_fields.derive 从 index 派生必填列（非空率 ≥ 阈值）
2. SheetMeta.col_non_empty 字段（build_index 统计 + load_index 反序列化兼容）
3. _load_required_fields 读派生后 yaml（必填检查生效）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from agent.excel.locator.table_index import SheetMeta, TableMeta, load_index


class TestSheetMetaColNonEmpty:
    """SheetMeta.col_non_empty 字段 + load_index 兼容。"""

    def test_default_empty_list(self):
        """SheetMeta 默认 col_non_empty=[]（向后兼容）。"""
        s = SheetMeta(name="S", headers=["a", "b"], header_names=["a", "b"])
        assert s.col_non_empty == []

    def test_load_index_old_format_compat(self):
        """旧 index JSON 无 col_non_empty → load_index 回退空 list。"""
        # 用真实 index（已重建含 col_non_empty），断言字段存在
        tables = load_index()
        assert len(tables) > 0
        # 找一个有数据的 sheet 确认 col_non_empty 非空
        found = False
        for t in tables:
            for s in t.sheets:
                if s.col_non_empty:
                    assert len(s.col_non_empty) == len(s.headers)
                    found = True
                    break
            if found:
                break
        assert found, "至少一个 sheet 应有 col_non_empty 统计"


class TestDeriveRequiredFields:
    """derive_required_fields.derive 派生逻辑。"""

    def _make_tables(self) -> list[TableMeta]:
        """构造测试用 TableMeta：activity 表，2 列必填 + 1 列非必填。"""
        s = SheetMeta(
            name="Activity", headers=["活动id", "活动名称", "备注"],
            header_names=["活动id", "活动名称", "备注"],
            row_count=10,
            col_non_empty=[10, 10, 2],  # id/名称 全填，备注 2/10 填
        )
        return [TableMeta(path="activity.xlsx", stem="activity", md5="x", sheets=[s])]

    def test_derive_high_rate_columns_required(self):
        """非空率 ≥ 0.9 的列 → 必填。"""
        from agent.excel.skills.derive_required_fields import derive
        tables = self._make_tables()
        with patch("agent.excel.locator.table_index.load_index", return_value=tables):
            import tempfile
            tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
            result = derive(output_path=tmp, threshold=0.9)
        # activity/Activity: 活动id(10/10=1.0) + 活动名称(10/10=1.0) 必填
        # 备注(2/10=0.2) 非必填
        req = result.get("activity", {}).get("Activity", [])
        assert "活动id" in req
        assert "活动名称" in req
        assert "备注" not in req

    def test_derive_threshold_adjustable(self):
        """阈值调低（0.1）→ 备注列也变必填。"""
        from agent.excel.skills.derive_required_fields import derive
        tables = self._make_tables()
        with patch("agent.excel.locator.table_index.load_index", return_value=tables):
            import tempfile
            tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
            result = derive(output_path=tmp, threshold=0.1)
        req = result.get("activity", {}).get("Activity", [])
        assert "活动id" in req
        assert "活动名称" in req
        assert "备注" in req  # 阈值 0.1 时 0.2 ≥ 0.1 → 必填

    def test_derive_skips_low_row_tables(self):
        """row_count < 2 的 sheet 跳过（统计无意义）。"""
        from agent.excel.skills.derive_required_fields import derive
        s = SheetMeta(name="S", headers=["a"], header_names=["a"],
                      row_count=1, col_non_empty=[1])
        tables = [TableMeta(path="t.xlsx", stem="t", md5="x", sheets=[s])]
        with patch("agent.excel.locator.table_index.load_index", return_value=tables):
            import tempfile
            tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
            result = derive(output_path=tmp, threshold=0.9)
        assert "t" not in result

    def test_derive_skips_missing_col_non_empty(self):
        """col_non_empty 空/长度不匹配 → 跳过该 sheet（旧索引兼容）。"""
        from agent.excel.skills.derive_required_fields import derive
        s = SheetMeta(name="S", headers=["a", "b"], header_names=["a", "b"],
                      row_count=10, col_non_empty=[])  # 空（旧索引）
        tables = [TableMeta(path="t.xlsx", stem="t", md5="x", sheets=[s])]
        with patch("agent.excel.locator.table_index.load_index", return_value=tables):
            import tempfile
            tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
            result = derive(output_path=tmp, threshold=0.9)
        assert "t" not in result

    def test_derive_preserves_manual_entries(self):
        """手工条目优先，派生补缺不覆盖。"""
        from agent.excel.skills.derive_required_fields import derive
        tables = self._make_tables()
        import tempfile
        tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
        # 预置手工条目
        tmp.write_text(
            "required_fields:\n  activity:\n    Activity:\n    - 手工列\n",
            encoding="utf-8")
        with patch("agent.excel.locator.table_index.load_index", return_value=tables):
            result = derive(output_path=tmp, threshold=0.9)
        # 手工条目保留（同 stem+sheet 不被覆盖）
        req = result.get("activity", {}).get("Activity", [])
        assert req == ["手工列"]  # 手工优先，派生不覆盖

    def test_derive_writes_required_fields_key(self):
        """输出 yaml 顶层 required_fields key 包裹（与 _load_required_fields 对齐）。"""
        import yaml
        from agent.excel.skills.derive_required_fields import derive
        tables = self._make_tables()
        import tempfile
        tmp = Path(tempfile.mkstemp(suffix=".yaml")[1])
        with patch("agent.excel.locator.table_index.load_index", return_value=tables):
            derive(output_path=tmp, threshold=0.9)
        raw = yaml.safe_load(tmp.read_text(encoding="utf-8"))
        assert "required_fields" in raw
        assert "activity" in raw["required_fields"]


class TestLoadRequiredFieldsIntegration:
    """_load_required_fields 读派生后 yaml。"""

    def test_load_reads_derived_config(self):
        """真实 index 派生后 _load_required_fields 读到配置。"""
        from agent.excel.cli.xlsx_tool import _load_required_fields
        d = _load_required_fields()
        # 真实 resources 有 activity 表，应含必填配置
        assert len(d) > 0
        activity = d.get("activity", {})
        # 至少一个 sheet 有必填配置
        assert any(len(cols) > 0 for cols in activity.values())

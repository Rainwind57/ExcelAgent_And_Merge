"""O20g set/delete locator_value 兜底提取单测（"删除名称为X的行"崩溃修复）。

覆盖三模块:
1. DecomposeAgent _to_split_intents 提取 locator_field/locator_value（LLM JSON 含这俩字段）
2. agent.py _fill_locator_from_fields（locator_value 空时从 fields 按 loc_match.column 取值）
3. _run_set/_run_delete 接 _fill_locator_from_fields 兜底（集成验证）
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.parser.nl_parser import NLIntent


# ── DecomposeAgent._to_split_intents 提取 locator ───────────
class TestToSplitIntentsLocator:
    def _make_da(self) -> DecomposeAgent:
        da = object.__new__(DecomposeAgent)
        return da

    def test_locator_field_value_extracted(self):
        """LLM JSON 含 locator_field/locator_value → SplitIntent 提取。"""
        da = self._make_da()
        arr = [{
            "table": "activity", "sheet": "Activity", "action": "delete",
            "fields": {}, "locator_field": "活动名称", "locator_value": "春节活动",
        }]
        intents = da._to_split_intents(arr, "删除春节活动")
        assert len(intents) == 1
        assert intents[0].locator_field == "活动名称"
        assert intents[0].locator_value == "春节活动"

    def test_locator_missing_defaults_none(self):
        """LLM JSON 无 locator_field/locator_value → SplitIntent 这俩为 None。"""
        da = self._make_da()
        arr = [{
            "table": "activity", "sheet": "Activity", "action": "add",
            "fields": {"id": 1},
        }]
        intents = da._to_split_intents(arr, "加活动")
        assert len(intents) == 1
        assert intents[0].locator_field is None
        assert intents[0].locator_value is None

    def test_locator_empty_string_treated_as_none(self):
        """LLM JSON locator_field/locator_value 空串 → 视为 None。"""
        da = self._make_da()
        arr = [{
            "table": "activity", "sheet": "Activity", "action": "add",
            "fields": {"id": 1},
            "locator_field": "", "locator_value": "  ",
        }]
        intents = da._to_split_intents(arr, "加活动")
        assert len(intents) == 1
        assert intents[0].locator_field is None
        assert intents[0].locator_value is None


# ── agent._fill_locator_from_fields 兜底 ───────────────────
@dataclass
class _LocStub:
    column: str = "活动名称"
    index: int = 3


class TestFillLocatorFromFields:
    def _make_agent(self):
        from agent.excel.core.agent import TableAgent
        return object.__new__(TableAgent)

    def test_extract_from_fields_by_column_name(self):
        """locator_value 空 + fields 含定位列值 → 提取填 locator_value。"""
        ag = self._make_agent()
        intent = NLIntent(action="delete", table_hint="activity",
                          sheet_hint="Activity", raw="删除春节活动",
                          extras={"fields": {"活动名称": "春节活动", "id": 1}})
        ag._fill_locator_from_fields(intent, _LocStub(column="活动名称", index=3))
        assert intent.locator_value == "春节活动"
        assert intent.locator_field == "活动名称"
        # delete 操作：定位列从 fields 移除（避免误写）
        assert "活动名称" not in intent.extras["fields"]

    def test_no_override_when_locator_value_present(self):
        """locator_value 已有 → 不覆盖。"""
        ag = self._make_agent()
        intent = NLIntent(action="delete", table_hint="activity",
                          sheet_hint="Activity", raw="删除春节活动",
                          locator_value="原值", locator_field="原列",
                          extras={"fields": {"活动名称": "春节活动"}})
        ag._fill_locator_from_fields(intent, _LocStub(column="活动名称"))
        assert intent.locator_value == "原值"
        assert intent.locator_field == "原列"

    def test_set_action_keeps_field_in_fields(self):
        """set 操作：定位列提取后不从 fields 移除（set 可能改该列）。"""
        ag = self._make_agent()
        intent = NLIntent(action="set", table_hint="activity",
                          sheet_hint="Activity", raw="改春节活动",
                          extras={"fields": {"活动名称": "春节活动", "描述": "新"}})
        ag._fill_locator_from_fields(intent, _LocStub(column="活动名称"))
        assert intent.locator_value == "春节活动"
        # set 操作：定位列保留在 fields（set 可能改该列）
        assert "活动名称" in intent.extras["fields"]

    def test_placeholder_value_not_extracted(self):
        """fields 值为占位符 <...> → 不提取（占位符非真实定位值）。"""
        ag = self._make_agent()
        intent = NLIntent(action="delete", table_hint="activity",
                          sheet_hint="Activity", raw="删",
                          extras={"fields": {"活动名称": "<new_xxx_id>"}})
        ag._fill_locator_from_fields(intent, _LocStub(column="活动名称"))
        assert intent.locator_value is None

    def test_column_with_suffix_stripped(self):
        """loc_match.column 含后缀（如 类型:int）→ 取:前段匹配。"""
        ag = self._make_agent()
        intent = NLIntent(action="delete", table_hint="t",
                          sheet_hint="S", raw="r",
                          extras={"fields": {"活动名称": "春节活动"}})
        ag._fill_locator_from_fields(intent, _LocStub(column="活动名称:string"))
        assert intent.locator_value == "春节活动"

    def test_no_matching_column_noop(self):
        """fields 无定位列 → 不提取（locator_value 保持 None）。"""
        ag = self._make_agent()
        intent = NLIntent(action="delete", table_hint="t",
                          sheet_hint="S", raw="r",
                          extras={"fields": {"id": 1}})
        ag._fill_locator_from_fields(intent, _LocStub(column="活动名称"))
        assert intent.locator_value is None

    def test_empty_fields_noop(self):
        """fields 空 → 不提取。"""
        ag = self._make_agent()
        intent = NLIntent(action="delete", table_hint="t",
                          sheet_hint="S", raw="r",
                          extras={"fields": {}})
        ag._fill_locator_from_fields(intent, _LocStub(column="活动名称"))
        assert intent.locator_value is None

    def test_none_loc_match_noop(self):
        """loc_match=None → 不提取。"""
        ag = self._make_agent()
        intent = NLIntent(action="delete", table_hint="t",
                          sheet_hint="S", raw="r",
                          extras={"fields": {"活动名称": "春节活动"}})
        ag._fill_locator_from_fields(intent, None)
        assert intent.locator_value is None

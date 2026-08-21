"""skill tools 单测：验证 make_skill_tools 工厂产出 7 工具、转发正确、写类返回确认提案。

capability: skill-executor-tools
"""
from __future__ import annotations

import json
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.tools import make_skill_tools


def _fake_executor():
    """构造 fake SkillExecutor，记录调用并返回固定结果。"""
    calls = []

    def call(skill, **kwargs):
        calls.append((skill, kwargs))
        if skill == "locate_table":
            return {"path": "pet/pet.xlsx", "stem": "pet", "ambiguous": []}
        if skill == "fuzzy_search_value":
            return [{"value": "刑天一阶", "score": 0.9}]
        if skill == "get_table_structure":
            return {"path": "pet/pet.xlsx", "stem": "pet", "sheets": [{"name": "Pet"}]}
        if skill == "list_all_tables":
            return [{"path": "pet/pet.xlsx", "stem": "pet", "sheets": ["Pet"]}]
        if skill == "analyze_enum_columns":
            return {"stem": "pet", "columns": [{"col_name": "灵兽品质"}]}
        return {"ok": True}

    exe = types.SimpleNamespace(call=call, _calls=calls)
    return exe


def _invoke(tool_obj, input_dict):
    """LangChain @tool invoke（input 为 dict 映射参数名）。"""
    return tool_obj.invoke(input_dict)


def test_factory_produces_seven_tools():
    exe = _fake_executor()
    tools = make_skill_tools(exe)
    assert len(tools) == 7
    names = {t.name for t in tools}
    expected = {"locate_table", "fuzzy_search_value", "get_table_structure",
                "list_all_tables", "analyze_enum_columns", "add_column", "update_enum_mapping"}
    assert names == expected


def test_locate_table_forwards():
    exe = _fake_executor()
    tools = {t.name: t for t in make_skill_tools(exe)}
    out = json.loads(_invoke(tools["locate_table"], {"text": "灵兽"}))
    assert exe._calls[-1] == ("locate_table", {"text": "灵兽"})
    assert out["stem"] == "pet"


def test_fuzzy_search_value_parses_candidates():
    exe = _fake_executor()
    tools = {t.name: t for t in make_skill_tools(exe)}
    out = json.loads(_invoke(tools["fuzzy_search_value"], {"query": "刑天", "candidates": "刑天一阶,刑天二阶,饕餮"}))
    assert exe._calls[-1][0] == "fuzzy_search_value"
    assert exe._calls[-1][1]["candidates"] == ["刑天一阶", "刑天二阶", "饕餮"]
    assert out[0]["value"] == "刑天一阶"


def test_get_table_structure_forwards():
    exe = _fake_executor()
    tools = {t.name: t for t in make_skill_tools(exe)}
    out = json.loads(_invoke(tools["get_table_structure"], {"path": "pet"}))
    assert exe._calls[-1] == ("get_table_structure", {"path": "pet"})
    assert out["stem"] == "pet"


def test_analyze_enum_columns_forwards_sheet_none_when_empty():
    exe = _fake_executor()
    tools = {t.name: t for t in make_skill_tools(exe)}
    _invoke(tools["analyze_enum_columns"], {"path": "pet"})
    assert exe._calls[-1] == ("analyze_enum_columns", {"path": "pet", "sheet": None, "max_unique": 20})


def test_add_column_returns_confirm_proposal_not_execute():
    """写类 tool 返回 needs_confirm 提案，不调 skill_executor.call。"""
    exe = _fake_executor()
    tools = {t.name: t for t in make_skill_tools(exe)}
    out = json.loads(_invoke(tools["add_column"], {"path": "pet/pet.xlsx", "sheet": "Pet", "name": "新列"}))
    assert out["needs_confirm"] is True
    assert out["skill"] == "add_column"
    assert out["args"]["name"] == "新列"
    assert all(c[0] != "add_column" for c in exe._calls)


def test_update_enum_mapping_returns_confirm_proposal():
    exe = _fake_executor()
    tools = {t.name: t for t in make_skill_tools(exe)}
    out = json.loads(_invoke(tools["update_enum_mapping"],
                             {"stem": "pet", "sheet": "Pet", "col_name": "灵兽品质", "mappings": '{"蓝":1,"紫":2}'}))
    assert out["needs_confirm"] is True
    assert out["args"]["mappings"] == {"蓝": 1, "紫": 2}
    assert all(c[0] != "update_enum_mapping" for c in exe._calls)


def test_update_enum_mapping_invalid_json():
    exe = _fake_executor()
    tools = {t.name: t for t in make_skill_tools(exe)}
    out = json.loads(_invoke(tools["update_enum_mapping"],
                             {"stem": "pet", "sheet": "Pet", "col_name": "灵兽品质", "mappings": "not json"}))
    assert out["ok"] is False

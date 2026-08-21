"""写前枚举预转换单测（capability: enum-mapping-pipeline D10 / 9.1-9.4）。

验证 _precoerce_enum_value / _precoerce_enum_fields：
- 命中枚举→替换为 int
- 未命中→保留原值（走原硬错误）
- 非 int 列→不修改
- 字符串数字直接 int()
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.agent import TableAgent


def _make_agent(monkeypatch, col_type_map: dict, enum_map: dict = None) -> types.SimpleNamespace:
    """轻量 agent：绑定 _get_col_type + _precoerce 方法 + mock enum_resolver。"""
    agent = types.SimpleNamespace()
    agent._get_col_type = lambda stem, sheet, col: col_type_map.get((stem, sheet, col), "")
    # 绑定实例方法
    agent._precoerce_enum_value = TableAgent._precoerce_enum_value.__get__(agent)
    agent._precoerce_enum_fields = TableAgent._precoerce_enum_fields.__get__(agent)
    # mock get_enum_resolver
    class _FakeER:
        def resolve_label(self, stem, sheet, col, label):
            return enum_map.get((stem, sheet, col), {}).get(label) if enum_map else None
    import agent.excel.agent as agent_mod
    monkeypatch.setattr(agent_mod, "get_enum_resolver", lambda: _FakeER(), raising=False)
    return agent


class TestPrecoerceEnumValue:
    def test_hit_enum_replaced_to_int(self, monkeypatch):
        agent = _make_agent(monkeypatch,
                            col_type_map={("pet", "Pet", "类型"): "int"},
                            enum_map={("pet", "Pet", "类型"): {"攻击": 1, "治疗": 2}})
        out = agent._precoerce_enum_value("类型", "攻击", "pet", "Pet")
        assert out == 1
        assert isinstance(out, int)

    def test_miss_enum_keeps_original(self, monkeypatch):
        agent = _make_agent(monkeypatch,
                            col_type_map={("pet", "Pet", "类型"): "int"},
                            enum_map={("pet", "Pet", "类型"): {"攻击": 1}})
        out = agent._precoerce_enum_value("类型", "未知标签", "pet", "Pet")
        assert out == "未知标签"  # 保留原值，走原硬错误

    def test_non_int_column_not_modified(self, monkeypatch):
        agent = _make_agent(monkeypatch,
                            col_type_map={("pet", "Pet", "名字"): "str"},
                            enum_map={})
        out = agent._precoerce_enum_value("名字", "小白", "pet", "Pet")
        assert out == "小白"

    def test_string_digit_direct_int(self, monkeypatch):
        agent = _make_agent(monkeypatch,
                            col_type_map={("pet", "Pet", "等级"): "int"},
                            enum_map={})
        out = agent._precoerce_enum_value("等级", "5", "pet", "Pet")
        assert out == 5
        assert isinstance(out, int)

    def test_already_int_keeps(self, monkeypatch):
        agent = _make_agent(monkeypatch,
                            col_type_map={("pet", "Pet", "等级"): "int"})
        out = agent._precoerce_enum_value("等级", 5, "pet", "Pet")
        assert out == 5

    def test_none_value_passthrough(self, monkeypatch):
        agent = _make_agent(monkeypatch, col_type_map={("pet", "Pet", "x"): "int"})
        assert agent._precoerce_enum_value("x", None, "pet", "Pet") is None

    def test_unknown_col_type_passthrough(self, monkeypatch):
        agent = _make_agent(monkeypatch, col_type_map={})
        assert agent._precoerce_enum_value("x", "任意", "pet", "Pet") == "任意"


class TestPrecoerceEnumFields:
    def test_dict_batch_precoerce(self, monkeypatch):
        agent = _make_agent(monkeypatch,
                            col_type_map={("pet", "Pet", "类型"): "int",
                                          ("pet", "Pet", "名字"): "str"},
                            enum_map={("pet", "Pet", "类型"): {"攻击": 1}})
        fields = {"类型": "攻击", "名字": "小白", "等级": "5"}
        # 等级未在 col_type_map → 未知类型 → 不改
        out = agent._precoerce_enum_fields(fields, "pet", "Pet")
        assert out["类型"] == 1
        assert out["名字"] == "小白"
        assert out["等级"] == "5"  # 未知列类型不改

    def test_empty_dict_passthrough(self, monkeypatch):
        agent = _make_agent(monkeypatch, col_type_map={})
        assert agent._precoerce_enum_fields({}, "pet", "Pet") == {}
        assert agent._precoerce_enum_fields(None, "pet", "Pet") is None

    def test_original_dict_not_mutated(self, monkeypatch):
        agent = _make_agent(monkeypatch,
                            col_type_map={("pet", "Pet", "类型"): "int"},
                            enum_map={("pet", "Pet", "类型"): {"攻击": 1}})
        fields = {"类型": "攻击"}
        out = agent._precoerce_enum_fields(fields, "pet", "Pet")
        assert out["类型"] == 1
        # 原 dict 不变
        assert fields["类型"] == "攻击"


# ── 入口 ─────────────────────────────────────────────────────

def _run_all():
    pytest.main([__file__, "-v"])


if __name__ == "__main__":
    _run_all()

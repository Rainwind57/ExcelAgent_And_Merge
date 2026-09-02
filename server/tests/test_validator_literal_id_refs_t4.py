"""T4 字面量 FK 引用一致性校验。

覆盖点：
- FK 字面量存在于 target 表 PK 集合时不报 warning。
- FK 字面量由本批 target producer 产出时不报 warning。
- JSON 数字型 int literal 也参与校验。
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.locator_agent import FKEdge, LocatorResult
from agent.excel.subagent.validator_agent import ValidatorAgent


def _validator():
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v.parser = SimpleNamespace()
    v._ask_callback = None
    v._required_fields = None
    v._pk_cols_cache = None
    return v


def _intent(table, sheet, fields):
    return SimpleNamespace(
        action="add", table_hint=table, sheet_hint=sheet,
        extras={"fields": dict(fields)})


def _lr():
    return LocatorResult(candidates=[], fk_edges=[
        FKEdge("combat", "Combat", "主怪", "interaction", "Interaction", "编号")
    ])


def test_literal_fk_existing_target_pk_no_issue():
    v = _validator()
    v._get_schema = lambda it, _sg: (["编号"], ["int"])

    def data_getter(intent):
        assert intent.table_hint == "interaction"
        assert intent.sheet_hint == "Interaction"
        return {"existing_values": {"编号": {25083001}}}

    consumer = _intent("combat", "Combat", {"主怪": "25083001"})
    out = v._validate_literal_id_refs([consumer], _lr(), data_getter=data_getter)
    assert out == {}


def test_literal_fk_same_batch_target_producer_no_issue():
    v = _validator()
    v._get_schema = lambda it, _sg: (["编号"], ["int"])
    producer = _intent("interaction", "Interaction", {"编号": 25083001})
    consumer = _intent("combat", "Combat", {"主怪": 25083001})
    out = v._validate_literal_id_refs([producer, consumer], _lr(), data_getter=None)
    assert out == {}


def test_literal_fk_missing_target_warns():
    v = _validator()
    v._get_schema = lambda it, _sg: (["编号"], ["int"])

    def data_getter(intent):
        return {"existing_values": {"编号": {1, 2, 3}}}

    consumer = _intent("combat", "Combat", {"主怪": 25083001})
    out = v._validate_literal_id_refs([consumer], _lr(), data_getter=data_getter)
    issues = out.get(id(consumer), [])
    assert len(issues) == 1
    assert "25083001" in issues[0].suggestion

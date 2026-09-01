"""Step2 列名/必填交互建议回归测试。"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parser.nl_parser import NLIntent
from agent.excel.subagent.validator_agent import ValidatorAgent


HEADERS = [
    "进化id", "填表说明", "宠物id", "灵兽名称", "进化等级",
    "进化后的灵兽ID", "进化后的灵兽名称", "进化要求灵兽1",
]
TYPE_ROW = [
    "evolve_id:int", "_desc:string", "pet_id:int", None, "evolve_level:int",
    "evolved_pet_id:int", None, "evolve_cost_pets.1:list",
]


def test_col_not_found_question_contains_real_columns_and_value_suggestion():
    v = object.__new__(ValidatorAgent)
    v._ask_callback = lambda q: {"mode": "skip", "_question": q}
    intent = NLIntent(
        action="add", table_hint="pet_evolve", sheet_hint="PetEvolveData",
        raw="进化路径：进化为 20998「焚天朱雀·涅槃」，消耗道具 10050×3",
        extras={"fields": {"evolved_pet_id": 20998, "None": "焚天朱雀·涅槃"}},
    )
    v._get_schema = lambda _it, _sg=None: (HEADERS, TYPE_ROW)

    reply = v._ask_col_not_found_batch(
        intent, [{"col": "None", "value": "焚天朱雀·涅槃", "suggestion": ""}],
        schema_getter=None)
    q = reply["_question"]
    row = q["batch_columns"][0]

    assert row["suggested"] == "进化后的灵兽名称"
    assert row["value"] == "焚天朱雀·涅槃"
    assert "进化后的灵兽名称" in q["available_columns"]


def test_business_required_is_editable_and_has_suggested_value():
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v._required_fields = None
    v._pk_cols_cache = None
    intent = NLIntent(
        action="add", table_hint="pet_evolve", sheet_hint="PetEvolveData",
        raw="新增传说灵兽「焚天朱雀」，进化路径：进化为 20998「焚天朱雀·涅槃」",
        extras={"fields": {"宠物id": "<new_pet_id>", "进化后的灵兽ID": 20998}},
    )
    tip = {
        "col": "进化后的灵兽名称",
        "issue_type": "missing_required",
        "expected": "业务必填列「进化后的灵兽名称」（指令明确给出该列值，LLM 漏产）",
    }

    info = v._build_editable_fields(intent, [tip], lambda _it: (HEADERS, TYPE_ROW))

    row = next(r for r in info["fields"] if r["col"] == "进化后的灵兽名称")
    assert row["value"] == "焚天朱雀·涅槃"
    assert row["suggested"] == "焚天朱雀·涅槃"
    assert row["invalid"] is True


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

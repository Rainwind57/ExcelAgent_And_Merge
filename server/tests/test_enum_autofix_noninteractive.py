# -*- coding: utf-8 -*-
"""验证 Step2 枚举歧义 LLM 自动推断（`_auto_resolve_enum`）在【非交互场景】
（批量执行/CI，没有 `_ask_callback`）下也能被触发，而不是只有带前端会话时
才生效。

背景（真实覆盖盲区，非新增功能）：`_auto_resolve_enum` 本身早就是 LLM 实现
且默认开启，但原代码把"无 _ask_callback 直接 continue"这一判断排在调用它
之前——批量/CI 场景永远走不到这段自动推断，中文枚举标签既不会被纠正，又
因为 ENUM_INVALID 不是硬阻断类型不会被标 skipped，最终原样写盘。修复后把
调用顺序提前，同一次自动纠正机会不再要求必须有前端会话。
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import NLIntent


def _make_intent():
    it = NLIntent(
        action="add", table_hint="pet", sheet_hint="Pet",
        raw="新增一个灵兽，属性写火",
    )
    it.extras = {
        "fields": {"pet_id": "<new_pet_id>", "属性": "火"},
        "produces": "new_pet_id",
    }
    it.produces_label = "new_pet_id"
    return it


def _schema_getter(intent):
    return ["pet_id", "属性"], ["int", "int"]


def _data_getter(intent):
    return {
        "existing_values": {}, "enum_set": {"属性": {"1", "2", "3"}},
        "result_rows": None, "cli": None, "path": None, "vc": None,
        "stem": "pet", "sheet": "Pet",
    }


def test_enum_autoresolve_triggers_without_ask_callback():
    """无 _ask_callback（非交互）时，命中中文枚举值的 tip 也应尝试自动推断。"""
    va = ValidatorAgent()
    va._ask_callback = None
    va._pk_cols_cache = {}
    it = _make_intent()
    intents = [it]

    with mock.patch.object(ValidatorAgent, "_auto_resolve_enum", return_value=2) as mocked:
        result = va.validate_two_layer(
            intents, schema_getter=_schema_getter,
            data_getter=_data_getter, locator_result=None)

    assert mocked.called, (
        "非交互模式下也应尝试调用 _auto_resolve_enum，"
        "覆盖盲区修复前这里永远不会被调用")
    fields = it.extras.get("fields", {})
    assert fields.get("属性") == 2, f"自动推断结果应被写回字段，实际 {fields}"


def test_enum_autoresolve_low_confidence_falls_back_without_crash():
    """LLM 推断不出（返回 None）时，非交互路径应优雅回落，不抛异常、不写脏值。"""
    va = ValidatorAgent()
    va._ask_callback = None
    va._pk_cols_cache = {}
    it = _make_intent()
    intents = [it]

    with mock.patch.object(ValidatorAgent, "_auto_resolve_enum", return_value=None) as mocked:
        result = va.validate_two_layer(
            intents, schema_getter=_schema_getter,
            data_getter=_data_getter, locator_result=None)

    assert mocked.called
    fields = it.extras.get("fields", {})
    assert fields.get("属性") == "火", (
        f"推断不出时字段应保持原值不被乱改，实际 {fields}")


if __name__ == "__main__":
    test_enum_autoresolve_triggers_without_ask_callback()
    test_enum_autoresolve_low_confidence_falls_back_without_crash()
    print("[PASS] enum autofix non-interactive coverage tests")

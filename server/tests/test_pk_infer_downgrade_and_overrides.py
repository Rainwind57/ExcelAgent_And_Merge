# -*- coding: utf-8 -*-
"""验证"PK 硬规则不要卡太死"的三点修复：

1. `primary_key: []` 显式声明"该 sheet 无主键" -> `_get_pk_cols` 不再回退到
   表头启发式猜测，`_is_pk_missing` 对任何列都不再硬阻断。
2. `required: false` 显式摘除 -> 从（自动派生/启发式凑出的）必填名单里精确
   删掉被误判的列。
3. 未声明主键的 sheet，纯命名启发式猜出来的"主键"列缺失时，若现有数据本身
   已有空值（经验证据）-> 自动降级为非阻断，不再硬拦本可留空的列。
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import NLIntent, IssueType


def _make_schema_getter(headers_map):
    def _sg(intent):
        stem = (getattr(intent, "table_hint", "") or "").lower()
        sheet = (getattr(intent, "sheet_hint", "") or "").lower()
        return headers_map.get((stem, sheet), ([], []))
    return _sg


def _make_data_getter(existing_values=None):
    def _dg(intent):
        return {
            "existing_values": existing_values or {},
            "enum_set": {}, "result_rows": None, "cli": None,
            "path": None, "vc": None,
            "stem": getattr(intent, "table_hint", ""),
            "sheet": getattr(intent, "sheet_hint", ""),
        }
    return _dg


def test_explicit_empty_primary_key_disables_heuristic_pk_guess():
    """primary_key: [] 显式声明 -> _get_pk_cols 不再猜首列/含id列。"""
    va = ValidatorAgent()
    va._pk_cols_cache = {"sometable": {"somesheet": []}}
    it = NLIntent(action="add", table_hint="sometable", sheet_hint="somesheet")
    cols = va._get_pk_cols(it, None)
    assert cols == [], f"显式声明无主键，应返回空列表，实际 {cols}"


def test_explicit_empty_primary_key_no_hard_block_on_missing():
    """声明无主键的 sheet，任何列缺失都不应硬阻断（ok=True，不 skipped）。"""
    va = ValidatorAgent()
    va._ask_callback = None
    va._pk_cols_cache = {"sometable": {"somesheet": []}}
    va._required_fields = {"sometable": {"somesheet": ["关联ID"]}}

    it = NLIntent(action="add", table_hint="sometable", sheet_hint="somesheet",
                  raw="新增一条记录")
    it.extras = {"fields": {"名称": "测试"}, "produces": ""}
    intents = [it]

    headers = ["关联ID", "名称"]
    type_row = ["int", "string"]
    schema_getter = _make_schema_getter({("sometable", "somesheet"): (headers, type_row)})
    data_getter = _make_data_getter()

    result = va.validate_two_layer(intents, schema_getter=schema_getter,
                                   data_getter=data_getter, locator_result=None)
    assert result.get("ok") is True, \
        f"该 sheet 已显式声明无主键，缺失「关联ID」不应硬阻断，实际 ok={result.get('ok')}"
    skipped = getattr(it.validation, "skipped", False)
    assert skipped is False


def test_required_false_overlay_removes_column():
    """required:false 覆盖 -> 从必填名单里精确摘除该列。

    注意：既有 P26 原则是"每个 sheet 只保留第一个 id/编号 列作为唯一必填候选，
    其余非主键必填一律删除"（见 `_load_required_fields` 的 PK 过滤逻辑），
    所以未声明真实 PK 时，「名称」这类非 id 列本来就不会进最终必填名单——
    这里验证的是：一旦被选中的那个"疑似主键"列（「关联ID」）被 required:false
    显式摘除，它就不再出现在必填名单里（该 sheet 变成"无强制必填列"，
    不再对任何列触发 MISSING_REQUIRED 硬阻断）。
    """
    va = ValidatorAgent()
    va._pk_cols_cache = {}
    va._required_fields = None  # 触发懒加载

    with mock.patch(
        "agent.excel.core.rules_loader.get_required_fields_overlay",
        return_value={"mytable": {"mysheet": ["关联ID", "名称"]}},
    ), mock.patch(
        "agent.excel.core.rules_loader.get_required_false_overlay",
        return_value={"mytable": {"mysheet": ["关联ID"]}},
    ):
        rf = va._load_required_fields()

    kept = rf.get("mytable", {}).get("mysheet", [])
    assert "关联ID" not in kept, f"required:false 应摘除「关联ID」，实际 {kept}"


def test_required_false_overlay_removes_declared_pk_required_column():
    """required:false 也能摘除【真实声明主键】列，不局限于启发式猜测列。"""
    va = ValidatorAgent()
    va._pk_cols_cache = {"mytable2": {"mysheet2": ["主键列"]}}
    va._required_fields = None

    with mock.patch(
        "agent.excel.core.rules_loader.get_required_fields_overlay",
        return_value={"mytable2": {"mysheet2": ["主键列"]}},
    ), mock.patch(
        "agent.excel.core.rules_loader.get_required_false_overlay",
        return_value={"mytable2": {"mysheet2": ["主键列"]}},
    ):
        rf = va._load_required_fields()

    kept = rf.get("mytable2", {}).get("mysheet2", [])
    assert "主键列" not in kept, f"required:false 应能摘除已声明主键列，实际 {kept}"


def test_inferred_pk_missing_downgrades_when_existing_data_has_blank():
    """未声明主键、命名像 id 的列缺失：现有数据里该列已有空值 -> 降级不阻断。"""
    va = ValidatorAgent()
    va._ask_callback = None
    va._pk_cols_cache = {}  # 未声明任何主键，走纯命名启发式

    it = NLIntent(action="add", table_hint="newtable", sheet_hint="NewSheet",
                  raw="新增一条记录")
    it.extras = {"fields": {"名称": "测试"}, "produces": ""}
    intents = [it]

    headers = ["关联ID", "名称"]
    type_row = ["int", "string"]
    schema_getter = _make_schema_getter({("newtable", "newsheet"): (headers, type_row)})
    # 现有数据「关联ID」列已出现过空值 -> 经验证据证明它不是真正必填主键
    data_getter = _make_data_getter(existing_values={"关联id": {1, 2, ""}})

    result = va.validate_two_layer(intents, schema_getter=schema_getter,
                                   data_getter=data_getter, locator_result=None)

    tips = result.get("tips") or []
    missing_req = [t for t in tips
                   if (t.get("issue_type") if isinstance(t, dict)
                       else getattr(t, "issue_type", "")) == IssueType.MISSING_REQUIRED.value]
    print(f"ok={result.get('ok')} tips={[ (t.get('col'), t.get('issue_type')) for t in tips]}")
    assert result.get("ok") is True, \
        f"经验证据证明「关联ID」可空，不应硬阻断，实际 ok={result.get('ok')}"


if __name__ == "__main__":
    test_explicit_empty_primary_key_disables_heuristic_pk_guess()
    test_explicit_empty_primary_key_no_hard_block_on_missing()
    test_required_false_overlay_removes_column()
    test_inferred_pk_missing_downgrades_when_existing_data_has_blank()
    print("[PASS] all pk-infer-downgrade-and-overrides tests")

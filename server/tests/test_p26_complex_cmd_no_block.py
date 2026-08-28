# -*- coding: utf-8 -*-
"""验证 P26 修复：复杂指令「焚天赤龙」场景的 3 类校验错误都不再硬阻断。

用户日志出现的 3 类错误：
1. interaction/InteractionConv 列[编号] missing_required
2. interaction/Interaction 列[effect.data.3005.to_pos] type_mismatch
3. entity_prefab/Base 列[编号] col_not_found

修复后：
- MISSING_REQUIRED 仅主键缺失才硬阻断；非主键缺失降级 warning
- COL_NOT_FOUND 降级 warning（不硬阻断），Step3 列映射兜底
- TYPE_MISMATCH 仍硬阻断（类型出错是用户明确要求保留的）
- required_fields.yaml 每个表每个 sheet 只保留第一个 id 列
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import NLIntent, IssueType


def _make_intent(table, sheet, fields, produces=None):
    it = NLIntent(
        action="add",
        table_hint=table,
        sheet_hint=sheet,
        raw="复杂指令焚天赤龙",
    )
    it.extras = {"fields": fields}
    if produces:
        it.extras["produces"] = produces
        it.produces_label = produces
    return it


def _make_schema_getter(headers_map):
    def _sg(intent):
        stem = (getattr(intent, "table_hint", "") or "").lower()
        sheet = (getattr(intent, "sheet_hint", "") or "").lower()
        return headers_map.get((stem, sheet), (None, None))
    return _sg


def _make_data_getter(existing_values_map):
    def _dg(intent):
        stem = (getattr(intent, "table_hint", "") or "").lower()
        sheet = (getattr(intent, "sheet_hint", "") or "").lower()
        return {
            "existing_values": existing_values_map.get((stem, sheet), {}),
            "enum_set": {},
            "result_rows": None,
            "cli": None,
            "path": None,
            "vc": None,
            "stem": stem,
            "sheet": sheet,
        }
    return _dg


def test_col_not_found_no_hard_block():
    """B 升硬阻 + UX 自动解决：COL_NOT_FOUND 单列在无 _ask_callback 时自动收敛。

    entity_class 无相似真实列（_closest_header 无建议）→ 自动从 fields 删该列，
    其余有效列（编号/名字/model_prefab）照常写盘；intent 不被 skip；ok=True
    （硬 issue 已自动解决，无失败收尾）。"""
    # entity_prefab/Base 真实表头（编号 含尾注）
    _pk_col = "编号 （按序递增，不要分段）"
    headers = [_pk_col, "填表说明(不导出)", "实体类型",
               "model_prefab", "名字", "地图UI图标id", "移动速度", "交互id"]
    type_row = ["int", "string", "string", "int", "string", "int", "int", "int"]

    # LLM 产了真实主键 + 一个 COL_NOT_FOUND 列「entity_class」（用别名）
    intent = _make_intent("entity_prefab", "Base", {
        _pk_col: 9999,
        "名字": "赤龙指引人",
        "model_prefab": 1020,
        "entity_class": "WorldNonPlayer",  # 这个键不在表头里 → COL_NOT_FOUND
    })
    intents = [intent]

    va = ValidatorAgent()
    va._ask_callback = None
    va._pk_cols_cache = {"entity_prefab": {_pk_col}}

    schema_getter = _make_schema_getter(
        {("entity_prefab", "base"): (headers, type_row)})
    data_getter = _make_data_getter({})

    result = va.validate_two_layer(
        intents, schema_getter=schema_getter,
        data_getter=data_getter, locator_result=None)

    # UX 自动解决后：entity_class 已从 fields 删除（幻觉列收敛）
    assert "entity_class" not in intent.extras["fields"], \
        f"entity_class 应被自动删除，实际 fields={list(intent.extras['fields'].keys())}"
    # 有效列保留
    assert _pk_col in intent.extras["fields"]
    assert "名字" in intent.extras["fields"]
    assert "model_prefab" in intent.extras["fields"]
    # 不 skip（自动解决，不丢整条 intent）
    assert getattr(intent.validation, "skipped", False) is False, \
        f"自动解决不应 skip，实际 skipped={getattr(intent.validation, 'skipped', None)}"
    # 硬 issue 已自动解决 → ok=True（无失败收尾）
    assert result.get("ok") is True, \
        f"COL_NOT_FOUND 自动解决后应 ok=True，实际 ok={result.get('ok')}"


def test_required_fields_only_first_id():
    """required_fields.yaml 每个表每个 sheet 只保留第一个 id 列。"""
    va = ValidatorAgent()
    va._required_fields = None  # 重置缓存
    rf = va._load_required_fields()

    print("=== required_fields 只保留第一个 id 列 ===")
    # 检查几个关键表
    for stem in ["activity", "interaction", "reward", "entity_prefab"]:
        sheets = rf.get(stem, {})
        for sheet, cols in sheets.items():
            print(f"  {stem}/{sheet}: {cols}")
            # 每个 sheet 最多 1 个必填列（主键）
            assert len(cols) <= 1, \
                f"{stem}/{sheet} 应只保留 1 个主键列，实际 {len(cols)} 个: {cols}"

    # activity 应只剩 活动id（不再有 活动名称）
    act_cols = rf.get("activity", {}).get("activity", [])
    print(f"  activity/Activity 最终: {act_cols}")
    assert "活动名称" not in act_cols, \
        f"activity 不应再必填 活动名称，实际 {act_cols}"
    assert "活动id" in act_cols, f"activity 应必填 活动id，实际 {act_cols}"

    # interaction 三个 sheet 各只保留 编号
    for sheet in ["interaction", "interactionconv", "interactionconvoption"]:
        cols = rf.get("interaction", {}).get(sheet, [])
        print(f"  interaction/{sheet}: {cols}")
        assert len(cols) <= 1, \
            f"interaction/{sheet} 应只 1 个主键，实际 {cols}"

    print("[PASS] required_fields 只保留第一个 id 列\n")


def test_missing_required_pk_block_with_suggestion():
    """主键缺失仍硬阻断，但提供建议值（max+1）。"""
    # interaction/InteractionConv 编号 缺失
    headers = ["编号", "对话内容", "选项1", "选项2"]
    type_row = ["int", "string", "int", "int"]

    intent = _make_intent("interaction", "InteractionConv", {
        "对话内容": "焚天赤龙正在肆虐人间...",
        # 故意不给 编号
    })
    intents = [intent]

    va = ValidatorAgent()
    va._ask_callback = None
    va._pk_cols_cache = {"interaction": {"编号"}}

    schema_getter = _make_schema_getter(
        {("interaction", "interactionconv"): (headers, type_row)})
    # 模拟已有数据：编号 1001, 1002 → 建议 1003
    data_getter = _make_data_getter({
        ("interaction", "interactionconv"): {"编号": {1001, 1002}}
    })

    result = va.validate_two_layer(
        intents, schema_getter=schema_getter,
        data_getter=data_getter, locator_result=None)

    tips = result.get("tips") or []
    missing_tips = [t for t in tips
                    if (t.get("issue_type") if isinstance(t, dict)
                        else getattr(t, "issue_type", "")) == IssueType.MISSING_REQUIRED.value]

    print("=== 主键缺失硬阻断 + 建议值 ===")
    print(f"ok: {result.get('ok')}")
    print(f"MISSING_REQUIRED tips: {len(missing_tips)}")
    print(f"skipped: {getattr(intent.validation, 'skipped', None)}")

    # 主键缺失 → 硬阻断 → skipped
    assert len(missing_tips) >= 1, "应有 MISSING_REQUIRED"
    assert result.get("ok") is False or getattr(intent.validation, "skipped", False), \
        "主键缺失应硬阻断或 skipped"

    print("[PASS] 主键缺失仍硬阻断（符合用户要求：主键缺失要校验）\n")


def test_non_pk_missing_required_no_block():
    """非主键 MISSING_REQUIRED 不硬阻断。"""
    # activity 活动开始时间 缺失（非主键）
    headers = ["活动id", "活动类型", "活动名称", "活动开始时间", "活动结束时间"]
    type_row = ["int", "int", "string", "string", "string"]

    intent = _make_intent("activity", "Activity", {
        "活动id": 3001,
        "活动名称": "焚天赤龙降临",
        "活动类型": 3,
        # 不给活动开始时间/结束时间
    })
    intents = [intent]

    va = ValidatorAgent()
    va._ask_callback = None
    va._pk_cols_cache = {"activity": {"活动id"}}
    va._required_fields = None  # 重置缓存强制重新加载

    schema_getter = _make_schema_getter(
        {("activity", "activity"): (headers, type_row)})
    data_getter = _make_data_getter({})

    result = va.validate_two_layer(
        intents, schema_getter=schema_getter,
        data_getter=data_getter, locator_result=None)

    tips = result.get("tips") or []
    missing_tips = [t for t in tips
                    if (t.get("issue_type") if isinstance(t, dict)
                        else getattr(t, "issue_type", "")) == IssueType.MISSING_REQUIRED.value]

    print("=== 非主键 MISSING_REQUIRED 不硬阻断 ===")
    print(f"ok: {result.get('ok')}")
    print(f"tips count: {len(tips)}")
    print(f"MISSING_REQUIRED tips: {len(missing_tips)}")
    print(f"skipped: {getattr(intent.validation, 'skipped', None)}")

    # 非主键 MISSING_REQUIRED 不硬阻断
    assert result.get("ok") is True, \
        f"非主键 MISSING_REQUIRED 不应硬阻断，ok={result.get('ok')}"
    assert not getattr(intent.validation, "skipped", False), \
        f"非主键 MISSING_REQUIRED 不应 skipped"

    print("[PASS] 非主键 MISSING_REQUIRED 不硬阻断\n")


if __name__ == "__main__":
    test_col_not_found_no_hard_block()
    test_required_fields_only_first_id()
    test_missing_required_pk_block_with_suggestion()
    test_non_pk_missing_required_no_block()
    print("=" * 50)
    print("[ALL PASS] P26 复杂指令场景校验修复验证通过")

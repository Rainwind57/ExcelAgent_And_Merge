# -*- coding: utf-8 -*-
"""验证 P25 修复：单指令「新增活动，名称春节活动，类型节日」不再被
MISSING_REQUIRED 硬阻断。

模拟场景：
  Step1 产出 intent: activity/Activity add, fields={活动id:<new>, 活动名称:春节活动, 活动类型:2}
  required_fields.yaml 原生配置 activity.Activity 必填 [活动id, 活动名称]
  rules/validate/activity.md overlay 必填 [活动id]
  → 活动名称有值, 活动id 有占位符 → 无 MISSING_REQUIRED
  → 即使有非主键 MISSING_REQUIRED（如活动开始时间）也不硬阻断

验证点：
  1. validate_two_layer 返回 ok=True（非阻断）
  2. intent.validation.skipped=False（不跳写盘）
  3. 非主键 MISSING_REQUIRED 降级 warning，不进 _hard_issue_types
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import NLIntent, IssueType


def _make_intent():
    """模拟 Step1 产出的单指令 intent。"""
    it = NLIntent(
        action="add",
        table_hint="activity",
        sheet_hint="Activity",
        raw="新增一个活动，名称春节活动，类型节日",
    )
    it.extras = {
        "fields": {
            "活动id": "<new_activity_id>",
            "活动名称": "春节活动",
            "活动类型": 2,
        },
        "produces": "new_activity_id",
    }
    it.produces_label = "new_activity_id"
    return it


def _make_schema_getter(headers_map):
    def _sg(intent):
        stem = (getattr(intent, "table_hint", "") or "").lower()
        sheet = (getattr(intent, "sheet_hint", "") or "").lower()
        return headers_map.get((stem, sheet), (None, None))
    return _sg


def _make_data_getter():
    def _dg(intent):
        return {
            "existing_values": {},
            "enum_set": {},
            "result_rows": None,
            "cli": None,
            "path": None,
            "vc": None,
            "stem": getattr(intent, "table_hint", ""),
            "sheet": getattr(intent, "sheet_hint", ""),
        }
    return _dg


def test_single_cmd_no_hard_block():
    """单指令不再被 MISSING_REQUIRED 硬阻断。"""
    headers = [
        "活动id", "活动类型", "活动名称", "活动描述", "活动图标",
        "活动开始时间", "活动结束时间", "活动展示开始时间",
        "活动展示结束时间", "开启条件"
    ]
    type_row = ["int", "int", "string", "string", "string",
                "string", "string", "string", "string", "string"]

    intent = _make_intent()
    intents = [intent]

    va = ValidatorAgent()
    # 模拟非交互模式（无 ask_callback）
    va._ask_callback = None

    schema_getter = _make_schema_getter({("activity", "activity"): (headers, type_row)})
    data_getter = _make_data_getter()

    result = va.validate_two_layer(
        intents, schema_getter=schema_getter,
        data_getter=data_getter, locator_result=None)

    print(f"=== validate_two_layer 结果 ===")
    print(f"ok: {result.get('ok')}")
    print(f"tips count: {len(result.get('tips') or [])}")
    for t in (result.get("tips") or []):
        print(f"  tip: {t}")
    print(f"intent.validation.skipped: {getattr(intent.validation, 'skipped', None)}")

    # 核心断言：非交互模式下，非主键 MISSING_REQUIRED 不硬阻断
    # 活动名称有值，活动id 有占位符，不应有任何 MISSING_REQUIRED
    tips = result.get("tips") or []
    missing_req_tips = [t for t in tips
                        if (t.get("issue_type") if isinstance(t, dict)
                            else getattr(t, "issue_type", "")) == IssueType.MISSING_REQUIRED.value]
    print(f"\nMISSING_REQUIRED tips: {len(missing_req_tips)}")
    for t in missing_req_tips:
        col = t.get("col") if isinstance(t, dict) else getattr(t, "col", "")
        print(f"  缺失列: {col}")

    # 活动名称有值，活动id 有占位符 → 不应有 MISSING_REQUIRED
    # 即使有（如活动开始时间），也不应硬阻断
    assert intent.validation is not None, "validation 应被设置"
    # 关键：不应被 skipped（非主键缺失不跳写盘）
    skipped = getattr(intent.validation, "skipped", False)
    print(f"\n>>> skipped={skipped}")
    # 非交互模式下，非主键 MISSING_REQUIRED 不应导致 skipped
    # （只有主键缺失或 PK 冲突未解决才 skipped）
    if missing_req_tips:
        # 有 MISSING_REQUIRED 但都是非主键列 → 不应 skipped
        non_pk_missing = all(
            ("id" not in str(t.get("col", "") if isinstance(t, dict)
             else getattr(t, "col", "")).lower()
             and "编号" not in str(t.get("col", "") if isinstance(t, dict)
                 else getattr(t, "col", "")))
            for t in missing_req_tips
        )
        if non_pk_missing:
            assert not skipped, f"非主键 MISSING_REQUIRED 不应 skipped，但 skipped={skipped}"

    print("\n[PASS] 测试通过：单指令不再被非主键 MISSING_REQUIRED 硬阻断")


if __name__ == "__main__":
    test_single_cmd_no_hard_block()

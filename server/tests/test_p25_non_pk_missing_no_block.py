# -*- coding: utf-8 -*-
"""验证 P25 修复：非主键 MISSING_REQUIRED 不硬阻断。

模拟场景：
  intent fields 缺少「活动开始时间」（required_fields.yaml 原生配置里该列非空率高，
  可能被派生为必填）。修复前 → MISSING_REQUIRED 硬阻断 → skipped 跳写盘。
  修复后 → 非主键 MISSING_REQUIRED 降级 warning → 不 skipped → Step3 照常写盘。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import NLIntent, IssueType


def _make_intent_missing_time():
    """模拟 Step1 产出但缺活动开始时间的 intent。

    required_fields.yaml 原生 activity.Activity 必填 [活动id, 活动名称]，
    若 rules/validate 或派生配置加了活动开始时间，会触发 MISSING_REQUIRED。
    """
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
            # 故意不给活动开始时间/活动结束时间
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


def test_non_pk_missing_required_no_block():
    """非主键 MISSING_REQUIRED 不硬阻断。"""
    # 直接在 ValidatorAgent 实例上注入 required_fields，模拟 required_fields.yaml
    # 派生了活动开始时间为必填的情况
    headers = [
        "活动id", "活动类型", "活动名称", "活动描述", "活动图标",
        "活动开始时间", "活动结束时间", "活动展示开始时间",
        "活动展示结束时间", "开启条件"
    ]
    type_row = ["int", "int", "string", "string", "string",
                "string", "string", "string", "string", "string"]

    intent = _make_intent_missing_time()
    intents = [intent]

    va = ValidatorAgent()
    va._ask_callback = None  # 非交互模式

    # 注入 required_fields：模拟活动开始时间也被标必填
    va._required_fields = {
        "activity": {
            "Activity": ["活动id", "活动名称", "活动开始时间", "活动结束时间"]
        }
    }
    # 注入 PK cols cache（活动id 是主键）
    va._pk_cols_cache = {"activity": {"活动id"}}

    schema_getter = _make_schema_getter(
        {("activity", "activity"): (headers, type_row)})
    data_getter = _make_data_getter()

    result = va.validate_two_layer(
        intents, schema_getter=schema_getter,
        data_getter=data_getter, locator_result=None)

    tips = result.get("tips") or []
    missing_req_tips = [t for t in tips
                        if (t.get("issue_type") if isinstance(t, dict)
                            else getattr(t, "issue_type", "")) == IssueType.MISSING_REQUIRED.value]

    print("=== 非主键 MISSING_REQUIRED 降级测试 ===")
    print(f"ok: {result.get('ok')}")
    print(f"tips count: {len(tips)}")
    for t in tips:
        col = t.get("col") if isinstance(t, dict) else getattr(t, "col", "")
        itype = t.get("issue_type") if isinstance(t, dict) else getattr(t, "issue_type", "")
        print(f"  tip: col={col}, type={itype}")
    print(f"MISSING_REQUIRED tips: {len(missing_req_tips)}")
    print(f"intent.validation.skipped: {getattr(intent.validation, 'skipped', None)}")

    # 核心断言：
    # 1. 有 MISSING_REQUIRED tips（活动开始时间/结束时间缺失）
    assert len(missing_req_tips) >= 2, \
        f"应有至少 2 条 MISSING_REQUIRED（活动开始/结束时间），实际 {len(missing_req_tips)}"

    # 2. 但 ok=True（非主键 MISSING_REQUIRED 不硬阻断）
    assert result.get("ok") is True, \
        f"非主键 MISSING_REQUIRED 不应硬阻断，但 ok={result.get('ok')}"

    # 3. intent 不被 skipped
    skipped = getattr(intent.validation, "skipped", False)
    assert skipped is False, \
        f"非主键 MISSING_REQUIRED 不应 skipped，但 skipped={skipped}"

    # 4. 所有 MISSING_REQUIRED 都是非主键列（活动开始时间/结束时间）
    for t in missing_req_tips:
        col = t.get("col") if isinstance(t, dict) else getattr(t, "col", "")
        assert col in ("活动开始时间", "活动结束时间"), \
            f"缺失列应为活动开始/结束时间，实际 {col}"

    print("\n[PASS] 非主键 MISSING_REQUIRED 降级 warning，不硬阻断，不 skipped")


if __name__ == "__main__":
    test_non_pk_missing_required_no_block()

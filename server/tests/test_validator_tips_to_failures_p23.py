"""validator P23 单测（OPTIMIZATION_LEDGER §4 第二批）。

覆盖 P22/P23：tips → failures 通道统一。O3 后 validate_two_layer 非阻断
（ok=True 恒），tips 供 thinking 展示但不上报 → CI/非交互 continue 带病
照样落盘、不上报（违 D6「失败必上报不静默吞」）。attach_tips_as_soft_failures
把遗留 tips 转 #40 形状软失败 dict 追加 intent.failures，让下游 partition
创建时 transfer 到 res.failures → all_failures 聚合 + _phase_summarize 上报。

运行: python -m pytest server/tests/test_validator_tips_to_failures_p23.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parser.nl_parser import NLIntent
from agent.excel.subagent.validator_agent import attach_tips_as_soft_failures


def _intent(table="pet", sheet="Pet", raw="加灵兽"):
    return NLIntent(action="add", table_hint=table, sheet_hint=sheet, raw=raw)


def _tip(intent, col="pet_id", issue_type="type_mismatch", expected="int",
         suggestion="改值", value="非数"):
    return {"subtask_id": id(intent), "col": col, "issue_type": issue_type,
            "expected": expected, "suggestion": suggestion, "value": value}


# ── attach_tips_as_soft_failures ────────────────────────────


class TestAttachTipsAsSoftFailuresP23:
    def test_tips_attached_to_matching_intent(self):
        """tip.subtask_id == id(intent) → 软失败 dict 追加 intent.failures。"""
        it = _intent()
        tips = [_tip(it, col="成长率", issue_type="type_mismatch", expected="float")]
        n = attach_tips_as_soft_failures([it], tips)
        assert n == 1
        assert len(it.failures) == 1
        f = it.failures[0]
        assert f["type"] == "validation_tip"
        assert f["table"] == "pet"
        assert f["sheet"] == "Pet"
        assert f["col"] == "成长率"
        assert "type_mismatch" in f["root_cause"]
        assert "float" in f["root_cause"]
        assert f["suggestion"] == "改值"
        assert f["status"] == "soft"
        assert "加灵兽" in f["snip"]

    def test_no_tips_zero_attached(self):
        """空 tips → 0 附加，intent.failures 不变。"""
        it = _intent()
        n = attach_tips_as_soft_failures([it], [])
        assert n == 0
        assert it.failures == []

    def test_unknown_subtask_id_skipped(self):
        """tip.subtask_id 不匹配任何 intent → 跳过。"""
        it = _intent()
        tip = {"subtask_id": 999999, "col": "x", "issue_type": "type_mismatch",
               "expected": "int", "suggestion": ""}
        n = attach_tips_as_soft_failures([it], [tip])
        assert n == 0
        assert it.failures == []

    def test_multiple_tips_same_intent_all_appended(self):
        """多 tips 指向同一 intent → 全部追加（保 D6 上报不静默吞）。"""
        it = _intent()
        tips = [
            _tip(it, col="pet_id", issue_type="type_mismatch", expected="int"),
            _tip(it, col="成长率", issue_type="range_outlier", expected="0~100"),
            _tip(it, col="名称", issue_type="unique_violation", expected="唯一"),
        ]
        n = attach_tips_as_soft_failures([it], tips)
        assert n == 3
        assert len(it.failures) == 3
        cols = [f["col"] for f in it.failures]
        assert cols == ["pet_id", "成长率", "名称"]

    def test_multiple_intents_tips_routed_correctly(self):
        """多 intent + 多 tip → 各 tip 路由到对应 intent。"""
        it1 = _intent("pet", "Pet")
        it2 = _intent("quest", "Quest", raw="加任务")
        tips = [
            _tip(it1, col="pet_id", issue_type="type_mismatch", expected="int"),
            _tip(it2, col="quest_id", issue_type="missing_required", expected="必填"),
        ]
        n = attach_tips_as_soft_failures([it1, it2], tips)
        assert n == 2
        assert len(it1.failures) == 1
        assert len(it2.failures) == 1
        assert it1.failures[0]["table"] == "pet"
        assert it2.failures[0]["table"] == "quest"
        assert it1.failures[0]["col"] == "pet_id"
        assert it2.failures[0]["col"] == "quest_id"

    def test_soft_failure_shape_has_all_40_fields(self):
        """软失败 dict 含 #40 形状全字段（type/table/sheet/col/root_cause/
        suggestion/status/snip），供 _phase_summarize + 前端渲染。"""
        it = _intent("mail", "Mail", raw="加邮件")
        tips = [_tip(it, col="标题", issue_type="col_not_found",
                     expected="列存在于表头", suggestion="检查列名")]
        attach_tips_as_soft_failures([it], tips)
        f = it.failures[0]
        for key in ("type", "table", "sheet", "col", "root_cause",
                    "suggestion", "status", "snip"):
            assert key in f, f"缺字段 {key}"
        assert f["type"] == "validation_tip"
        assert f["status"] == "soft"  # 区别于 hard failure

    def test_empty_expected_root_cause_only_issue_type(self):
        """expected 为空 → root_cause 仅 issue_type（不拼空冒号）。"""
        it = _intent()
        tips = [_tip(it, expected="")]
        attach_tips_as_soft_failures([it], tips)
        f = it.failures[0]
        assert f["root_cause"] == "type_mismatch"  # 不带 ": "

    def test_idempotent_append(self):
        """同一 tips 两次 attach → intent.failures 累加（不幂等,调用方负责去重）。"""
        it = _intent()
        tips = [_tip(it)]
        attach_tips_as_soft_failures([it], tips)
        attach_tips_as_soft_failures([it], tips)
        assert len(it.failures) == 2  # 累加,不幂等


# ── NLIntent.failures 字段默认值 ──────────────────────────────


class TestNLIntentFailuresField:
    def test_default_empty_list(self):
        """NLIntent 新增 failures 字段默认空 list（dataclass default_factory）。"""
        it = NLIntent(action="add", table_hint="pet", raw="x")
        assert it.failures == []
        assert isinstance(it.failures, list)

    def test_independent_default_per_instance(self):
        """dataclass field(default_factory=list) → 各实例独立 list（不共享）。"""
        it1 = NLIntent(action="add", raw="x")
        it2 = NLIntent(action="add", raw="y")
        it1.failures.append({"type": "x"})
        assert it1.failures == [{"type": "x"}]
        assert it2.failures == []  # 不受 it1 影响

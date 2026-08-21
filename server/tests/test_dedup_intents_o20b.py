"""O20b 同表同 sheet 同字段去重单测（S1 Quest 18-23 6 条重复修复）。

覆盖 _dedup_intents + validate_two_layer 接线：
- 6 条相同 Quest 配置 → 去重留 1 条
- 同表同 sheet 不同字段（BuildingInteract idle/collect）→ 不误杀
- 同表同 sheet 不同 locator_value（改不同行）→ 不误杀
- 不同表 → 不互影响
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import NLIntent


def _make_validator():
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v._parser = None
    v._ask_callback = None
    v._required_fields = None
    return v


def _quest_intent(fields=None, action="add", table="quest", sheet="Quest"):
    """Quest intent（无 produces，消费者，S1 重复写入场景）。"""
    if fields is None:
        fields = {"任务类型": "Combat", "战斗id": 25060001, "奖励包id": 100600}
    it = NLIntent(action=action, table_hint=table, sheet_hint=sheet,
                  raw="任务目标", extras={"fields": fields})
    return it


class TestDedupIntents:
    def test_six_identical_quest_dedup_to_one(self):
        """S1 场景：6 条相同 Quest 配置 → 去重留 1。"""
        v = _make_validator()
        intents = [_quest_intent() for _ in range(6)]
        n = v._dedup_intents(intents)
        assert n == 5
        assert len(intents) == 1

    def test_different_fields_not_dedup(self):
        """同表同 sheet 但 fields 不同（BuildingInteract idle/collect）→ 不误杀。"""
        v = _make_validator()
        intents = [
            NLIntent(action="add", table_hint="building", sheet_hint="BuildingInteract",
                     raw="r1", extras={"fields": {"state": "idle", "效果": "待机"}}),
            NLIntent(action="add", table_hint="building", sheet_hint="BuildingInteract",
                     raw="r2", extras={"fields": {"state": "collect", "效果": "采集"}}),
        ]
        n = v._dedup_intents(intents)
        assert n == 0
        assert len(intents) == 2

    def test_different_locator_not_dedup(self):
        """同表同 sheet 同 fields 但 locator_value 不同（改不同行）→ 不误杀。"""
        v = _make_validator()
        intents = [
            NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                     locator_value="白虎", raw="改白虎", extras={"fields": {"名称": "白虎王"}}),
            NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                     locator_value="饕餮", raw="改饕餮", extras={"fields": {"名称": "饕餮王"}}),
        ]
        n = v._dedup_intents(intents)
        assert n == 0
        assert len(intents) == 2

    def test_different_table_not_dedup(self):
        """不同表同 fields → 不互影响。"""
        v = _make_validator()
        intents = [
            NLIntent(action="add", table_hint="quest", sheet_hint="Quest",
                     raw="r1", extras={"fields": {"id": 1}}),
            NLIntent(action="add", table_hint="combat", sheet_hint="Combat",
                     raw="r2", extras={"fields": {"id": 1}}),
        ]
        n = v._dedup_intents(intents)
        assert n == 0
        assert len(intents) == 2

    def test_empty_intents_noop(self):
        v = _make_validator()
        assert v._dedup_intents([]) == 0

    def test_single_intent_kept(self):
        v = _make_validator()
        intents = [_quest_intent()]
        n = v._dedup_intents(intents)
        assert n == 0
        assert len(intents) == 1

    def test_partial_dup_mixed(self):
        """3 条相同 + 1 条不同 + 2 条相同 → 留 2 条（去 4）。"""
        v = _make_validator()
        intents = [
            _quest_intent(fields={"a": 1}),
            _quest_intent(fields={"a": 1}),  # 重复
            _quest_intent(fields={"a": 1}),  # 重复
            _quest_intent(fields={"b": 2}),  # 不同
            _quest_intent(fields={"a": 1}),  # 重复
            _quest_intent(fields={"b": 2}),  # 重复（与第 4 条同）
        ]
        n = v._dedup_intents(intents)
        assert n == 4
        assert len(intents) == 2


class TestValidateTwoLayerDedupWiring:
    def test_validate_two_layer_invokes_dedup(self):
        """validate_two_layer 调 _dedup_intents（4-step 路径接去重）。"""
        v = _make_validator()
        intents = [_quest_intent() for _ in range(3)]
        # mock validate_field_layer/fk_layer 返空 dict（非阻断，ok 恒 True）
        v.validate_field_layer = lambda its, sg=None, dg=None: {}
        v.validate_fk_layer = lambda its, lr=None: {}
        v.add_thinking = lambda *a, **kw: None  # 静默 thinking
        result = v.validate_two_layer(intents)
        assert result["ok"] is True
        assert len(intents) == 1  # 去重后剩 1


class TestDedupPlaceholderNormalizationO20e:
    """O20e S1 重复写入根治：占位符值归一为 <ph> 去重。

    场景：6 候选表各产 1 条 Quest intent，consumes 引用不同 producer label
    → fields 占位符不同（<new_combat_id> vs <new_reward_id> 等）→
    原 _dedup_intents 不去重。O20e 占位符归一为 <ph> 后 sig 相同去重。
    """
    def test_placeholder_only_diff_dedup(self):
        """fields 仅占位符值不同 → 归一为 <ph> 后去重（S1 6 条 Quest 残留根治）。"""
        v = _make_validator()
        intents = [
            _quest_intent(fields={"id": 1, "战斗id": "<new_combat_id>"}),
            _quest_intent(fields={"id": 1, "战斗id": "<new_reward_id>"}),
            _quest_intent(fields={"id": 1, "战斗id": "<new_item_id>"}),
        ]
        n = v._dedup_intents(intents)
        assert n == 2  # 3 条占位符仅不同 → 归一后相同 → 留 1
        assert len(intents) == 1

    def test_real_field_diff_not_dedup(self):
        """fields 真实值不同（state=idle/collect）→ 保留（不归一非占位符值）。"""
        v = _make_validator()
        intents = [
            NLIntent(action="add", table_hint="building", sheet_hint="BuildingInteract",
                     raw="r1", extras={"fields": {"state": "idle", "效果": "待机"}}),
            NLIntent(action="add", table_hint="building", sheet_hint="BuildingInteract",
                     raw="r2", extras={"fields": {"state": "collect", "效果": "采集"}}),
        ]
        n = v._dedup_intents(intents)
        assert n == 0
        assert len(intents) == 2

    def test_mixed_placeholder_and_real_diff_kept(self):
        """占位符不同 + 真实字段不同 → 真实差异保留，纯占位符差异去重。

        3 条 intent：2 条仅占位符不同（去重留 1），1 条真实字段不同（保留）。
        """
        v = _make_validator()
        intents = [
            _quest_intent(fields={"id": 1, "战斗id": "<new_combat_id>", "类型": "主线"}),
            _quest_intent(fields={"id": 1, "战斗id": "<new_reward_id>", "类型": "主线"}),  # 占位符差异 → 去重
            _quest_intent(fields={"id": 1, "战斗id": "<new_combat_id>", "类型": "支线"}),  # 真实字段差异 → 保留
        ]
        n = v._dedup_intents(intents)
        assert n == 1
        assert len(intents) == 2

    def test_multiple_placeholder_fields_normalized(self):
        """多占位符字段（战斗id + 奖励id）→ 各归一为 <ph>，组合相同去重。"""
        v = _make_validator()
        intents = [
            _quest_intent(fields={"id": 1, "战斗id": "<new_combat_id>", "奖励id": "<new_reward_id>"}),
            _quest_intent(fields={"id": 1, "战斗id": "<new_reward_id>", "奖励id": "<new_combat_id>"}),
        ]
        # 注意：占位符归一后两条完全相同（id=1, 战斗id=<ph>, 奖励id=<ph>）→ 去重
        n = v._dedup_intents(intents)
        assert n == 1
        assert len(intents) == 1

    def test_non_string_field_not_normalized_kept(self):
        """非字符串字段（数字 id=1）不受占位符归一影响，正常参与 sig。"""
        v = _make_validator()
        intents = [
            _quest_intent(fields={"id": 1, "战斗id": "<new_combat_id>"}),
            _quest_intent(fields={"id": 2, "战斗id": "<new_combat_id>"}),  # id 真实不同
        ]
        n = v._dedup_intents(intents)
        assert n == 0  # id=1 vs id=2 真实差异 → 不去重
        assert len(intents) == 2


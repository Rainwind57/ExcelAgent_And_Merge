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
    v._pk_cols_cache = None
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


class TestInterPkDupPack2:
    """Pack 2：批内同表同 sheet 同 PK 列同值但 fields 不同（含 name 冲突）的去重。

    实证 bench 样例：reward_id=100608 复用 3 次（首通/冰封首通/冰封里程碑 不同
    name），item_id=29012 复用 3 次（冰魄碎片/之戒 name 冲突）→ Step3 first 写
    入，rest 撞 pk_conflict 入 failures 噪音。改前移 Step2 处理。
    """

    def _make_reward_it(self, name, pk=100608, action="add"):
        return NLIntent(action=action, table_hint="reward", sheet_hint="Reward",
                        raw=name, extras={"fields": {"reward_id": pk, "name": name}})

    def _make_data_getter(self, existing_pks=None):
        if existing_pks is None:
            existing_pks = {"100001", "100002", "100003"}
        return lambda it: {"existing_values": {"reward_id": existing_pks},
                           "stem": getattr(it, "table_hint", ""),
                           "sheet": getattr(it, "sheet_hint", "")}

    def _make_validator_with_cache(self):
        v = _make_validator()
        v._pk_cols_cache = {"reward": {"reward_id"}, "item": {"item_id"}}
        return v

    def test_three_same_pk_snowball_increment(self):
        """同 PK 3 个不同 name → first 保持 100608，2/3 雪球递增 100609/100610，name 保留。"""
        v = self._make_validator_with_cache()
        intents = [self._make_reward_it("首通奖励包"),
                   self._make_reward_it("冰封首通"),
                   self._make_reward_it("冰封里程碑")]
        n = v._dedup_inter_pk_dup(
            intents, data_getter=self._make_data_getter(),
            schema_getter=None)
        assert n == 2
        pks = [it.extras["fields"]["reward_id"] for it in intents]
        names = [it.extras["fields"]["name"] for it in intents]
        assert pks == [100608, 100609, 100610]
        assert names == ["首通奖励包", "冰封首通", "冰封里程碑"]

    def test_no_pk_cache_uses_schema_getter(self):
        """_pk_cols_cache 空 → fallback schema_getter 表头含 id 列定位 PK。"""
        v = _make_validator()  # _pk_cols_cache=None → _load_pk_cols_cache 可能为空
        v._pk_cols_cache = {}  # 显式空 → 不 fallback load
        intents = [self._make_reward_it("first"),
                   self._make_reward_it("second")]
        schema_getter = lambda it: (
            ["reward_id", "reward_name", "reward_value"], ["int", "string", "int"]
            if getattr(it, "table_hint", "").lower() == "reward"
            else ([], []))
        n = v._dedup_inter_pk_dup(
            intents, data_getter=self._make_data_getter({"100001"}),
            schema_getter=schema_getter)
        assert n == 1
        assert intents[0].extras["fields"]["reward_id"] == 100608
        assert intents[1].extras["fields"]["reward_id"] == 100609

    def test_no_callback_skip_for_non_numeric_pk(self):
        """无 cb + first PK 非数字 → _next_n=None → mark skipped（无可建议改号）。"""
        v = self._make_validator_with_cache()
        intents = [
            NLIntent(action="add", table_hint="reward", sheet_hint="Reward",
                     raw="first", extras={"fields": {"reward_id": "FROZEN_PACK",
                                                     "name": "first"}}),
            NLIntent(action="add", table_hint="reward", sheet_hint="Reward",
                     raw="second", extras={"fields": {"reward_id": "FROZEN_PACK",
                                                      "name": "second"}}),
        ]
        n = v._dedup_inter_pk_dup(
            intents, data_getter=self._make_data_getter(set()),
            schema_getter=None)
        assert n == 1
        assert intents[0].extras["fields"]["reward_id"] == "FROZEN_PACK"
        assert getattr(intents[1].validation, "skipped", False) is True

    def test_callback_ask_accept_suggest_changes_pk(self):
        """有 _ask_callback → 走 _ask_pk_conflict ask；accept_suggest=True 自动改号。"""
        v = self._make_validator_with_cache()
        v._ask_callback = lambda q: {"mode": "field", "accept_suggest": True}
        intents = [self._make_reward_it("first"),
                   self._make_reward_it("second")]
        n = v._dedup_inter_pk_dup(
            intents, data_getter=self._make_data_getter(),
            schema_getter=None)
        assert n == 1
        assert intents[0].extras["fields"]["reward_id"] == 100608
        assert intents[1].extras["fields"]["reward_id"] == 100609

    def test_callback_skip_marks_skipped(self):
        """用户 mode=skip → mark_intent_skipped 阻断 Step3 写盘。"""
        v = self._make_validator_with_cache()
        v._ask_callback = lambda q: {"mode": "skip"}
        intents = [self._make_reward_it("first"),
                   self._make_reward_it("second")]
        n = v._dedup_inter_pk_dup(
            intents, data_getter=self._make_data_getter(),
            schema_getter=None)
        assert n == 1
        assert getattr(intents[1].validation, "skipped", False) is True

    def test_single_pk_dup_intra_batch_no_op(self):
        """组内只有 1 个成员（无重复）→ noop 返回 0。"""
        v = self._make_validator_with_cache()
        intents = [self._make_reward_it("solo")]
        n = v._dedup_inter_pk_dup(
            intents, data_getter=self._make_data_getter(), schema_getter=None)
        assert n == 0

    def test_different_pks_not_grouped(self):
        """同表同 sheet 但 PK 值不同 → 不分组（无 inter_pk_dup 触发）。"""
        v = self._make_validator_with_cache()
        intents = [self._make_reward_it("a", pk=100001),
                   self._make_reward_it("b", pk=100002)]
        n = v._dedup_inter_pk_dup(
            intents, data_getter=self._make_data_getter(), schema_getter=None)
        assert n == 0
        assert intents[0].extras["fields"]["reward_id"] == 100001
        assert intents[1].extras["fields"]["reward_id"] == 100002


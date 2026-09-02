"""candidate_grouping 单测（文档「候选分层」MVP #3 / StepTrace §P0）。

验证 required/dependency/context 三级分层的纯函数逻辑：
  - 规则强命中 → required；FK 派生 → dependency；弱信号 → context。
  - LLM 复核判定涉及 → required（覆盖 level）。
  - context 若与 required 有 FK 边相连 → 提升为 dependency。
  - 保序、去重、降级安全。

运行: python -m pytest server/tests/test_candidate_grouping.py -q
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.locator.candidate_grouping import classify_candidates, group_counts


def _c(stem, level):
    return SimpleNamespace(stem=stem, level=level)


def _e(from_stem, to_stem):
    return SimpleNamespace(from_stem=from_stem, from_sheet="", from_column="",
                           to_stem=to_stem, to_sheet="", to_column="")


class TestClassify:
    def test_rule_strong_is_required(self):
        g = classify_candidates([_c("pet", "exact"), _c("npc", "alias")])
        assert g["required"] == ["pet", "npc"]
        assert g["dependency"] == [] and g["context"] == []

    def test_fk_levels_are_dependency(self):
        g = classify_candidates([_c("pet", "exact"),
                                 _c("pet_evolve", "fk_expanded"),
                                 _c("reward", "fk_inferred")])
        assert g["required"] == ["pet"]
        assert g["dependency"] == ["pet_evolve", "reward"]

    def test_weak_levels_are_context(self):
        g = classify_candidates([_c("pet", "exact"),
                                 _c("school", "column_extract"),
                                 _c("combat", "substring"),
                                 _c("item", "column_reverse")])
        assert g["required"] == ["pet"]
        assert g["context"] == ["school", "combat", "item"]

    def test_llm_relevant_overrides_to_required(self):
        g = classify_candidates(
            [_c("pet", "exact"), _c("activity", "column_extract")],
            llm_relevant=["activity"])
        assert set(g["required"]) == {"pet", "activity"}
        assert g["context"] == []

    def test_context_linked_to_required_promoted_to_dependency(self):
        # school 弱命中(context)，但与 required(interaction) 有 FK 边 → dependency
        g = classify_candidates(
            [_c("interaction", "exact"), _c("school", "column_extract")],
            fk_edges=[_e("interaction", "school")])
        assert g["required"] == ["interaction"]
        assert g["dependency"] == ["school"]
        assert g["context"] == []

    def test_context_not_linked_stays_context(self):
        g = classify_candidates(
            [_c("interaction", "exact"), _c("school", "column_extract")],
            fk_edges=[_e("reward", "mail")])  # 无关边
        assert g["context"] == ["school"]

    def test_dedup_and_order_preserved(self):
        g = classify_candidates([_c("pet", "exact"), _c("pet", "alias"),
                                 _c("npc", "exact")])
        assert g["required"] == ["pet", "npc"]

    def test_empty_and_bad_input(self):
        assert classify_candidates([]) == {"required": [], "dependency": [], "context": []}
        g = classify_candidates([_c(None, "exact"), _c("", "exact"), _c("ok", "exact")])
        assert g["required"] == ["ok"]

    def test_group_counts(self):
        g = {"required": ["a", "b"], "dependency": ["c"], "context": []}
        assert group_counts(g) == {"required_count": 2, "dependency_count": 1,
                                   "context_count": 0}
        assert group_counts({}) == {"required_count": 0, "dependency_count": 0,
                                    "context_count": 0}

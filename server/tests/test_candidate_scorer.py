"""§9.4 候选表通用 scorer 单测（路线图 §3/§9.4，0 LLM）。

覆盖：
  - 动词邻近度：动作词附近出现的实体名词候选获得加权。
  - 强命中保护：conf>=0.80 的规则命中不被改动。
  - 封顶不跨强命中分界线（<=0.79）。
  - 无动作词/无实体 → 不加权，保序。
"""
from __future__ import annotations

from agent.excel.locator.candidate_scorer import (
    rescore_candidates, verb_proximity_hits,
)
from agent.excel.subagent.locator_agent import CandidateTable


def _c(stem, conf, level="alias", matched=""):
    return CandidateTable(stem=stem, sheet="", confidence=conf,
                          level=level, matched_term=matched)


class TestVerbProximityHits:
    def test_entity_near_action_verb(self):
        c = _c("activity", 0.6, matched="活动")
        assert verb_proximity_hits("新增一个活动叫春节活动", c) >= 1

    def test_entity_far_from_action_verb(self):
        c = _c("mail", 0.6, matched="邮件")
        assert verb_proximity_hits("新增一个活动叫春节活动，再把它绑定到邮件模板", c) >= 1

    def test_no_action_no_hits(self):
        c = _c("activity", 0.6, matched="活动")
        assert verb_proximity_hits("这是活动的描述文本", c) == 0

    def test_no_term_no_hits(self):
        c = _c("activity", 0.6)
        assert verb_proximity_hits("新增一个活动", c) == 0


class TestRescoreCandidates:
    def test_weak_candidate_boosted_near_verb(self):
        cands = [_c("activity", 0.6, matched="活动"),
                 _c("mail", 0.6, matched="邮件")]
        out = rescore_candidates("新增一个活动叫春节活动", cands)
        # 活动邻近动作词 → 加权后应排在邮件前
        assert out[0].stem == "activity"
        assert out[0].confidence > 0.6

    def test_strong_candidate_untouched(self):
        c = _c("pet", 0.9, matched="灵兽")
        out = rescore_candidates("新增一个灵兽叫朱雀", [c])
        assert out[0].confidence == 0.9

    def test_boost_capped_below_strong_band(self):
        c = _c("activity", 0.78, matched="活动")
        out = rescore_candidates("新增活动并配置活动并绑定活动", [c])
        assert out[0].confidence <= 0.79

    def test_no_candidates_passthrough(self):
        assert rescore_candidates("x", []) == []

    def test_stable_order_when_no_signal(self):
        a = _c("a", 0.6)
        b = _c("b", 0.6)
        out = rescore_candidates("随便说一句", [a, b])
        assert [c.stem for c in out] == ["a", "b"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

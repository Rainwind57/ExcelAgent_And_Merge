"""§9.6 Step4 修复经验沉淀单测（路线图 §7/§9.6）。

覆盖：
  - _collect_field_corrections：从 user_resolved_fields 台账重建「改名对」。
  - 只取 source="user" 黄金信号；source="auto" 不算别名候选。
  - SkillUpdater.ingest_field_corrections：追加候选 jsonl（user_corrected=True），
    不写死代码，走既有 try_promote 门控。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.pipeline.step4_conclude_subagent import (
    _collect_field_corrections,
)
from agent.excel.parser.nl_parser import NLIntent


def _intent(table="pet", sheet="Pet", book=None):
    it = NLIntent(action="add", table_hint=table, sheet_hint=sheet, raw="x")
    it.extras = {"fields": {}, "user_resolved_fields": book or {}}
    return it


class TestCollectFieldCorrections:
    def test_rename_pair_reconstructed(self):
        """旧列删除(值保留)+新列新增(同值) → 重建改名对 (query, resolved)。"""
        it = _intent(table="activity", sheet="Activity", book={
            "名称": {"old": "春节活动", "new": "", "source": "user"},
            "name": {"old": "", "new": "春节活动", "source": "user"},
        })
        out = _collect_field_corrections([it])
        assert any(
            c["query"] == "名称" and c["resolved"] == "name"
            and c["table_stem"] == "activity"
            for c in out)

    def test_auto_source_excluded(self):
        """source=auto（AI 建议）不产别名候选。"""
        it = _intent(book={
            "名称": {"old": "春节活动", "new": "", "source": "auto"},
            "name": {"old": "", "new": "春节活动", "source": "auto"},
        })
        assert _collect_field_corrections([it]) == []

    def test_value_mismatch_no_pair(self):
        """删除列值与新增列值不一致 → 不重建改名对。"""
        it = _intent(book={
            "名称": {"old": "春节活动", "new": "", "source": "user"},
            "name": {"old": "", "new": "别的值", "source": "user"},
        })
        assert _collect_field_corrections([it]) == []

    def test_no_book_returns_empty(self):
        assert _collect_field_corrections([_intent()]) == []
        assert _collect_field_corrections([]) == []


class TestIngestFieldCorrections:
    def test_ingest_appends_candidate(self, tmp_path):
        from agent.excel.core.skill_updater import SkillUpdater, _read_jsonl
        updater = SkillUpdater(tmp_path / "skills", tmp_path / "evidence")
        n = updater.ingest_field_corrections([{
            "table_stem": "activity", "sheet": "Activity",
            "query": "活动名称", "resolved": "name",
        }])
        assert n == 1
        cands = _read_jsonl(updater.candidates_path)
        assert len(cands) == 1
        assert cands[0]["query"] == "活动名称"
        assert cands[0]["resolved"] == "name"
        assert cands[0]["user_corrected"] is True
        assert cands[0]["source"] == "step2_field_edit"

    def test_ingest_skips_same_and_empty(self, tmp_path):
        from agent.excel.core.skill_updater import SkillUpdater, _read_jsonl
        updater = SkillUpdater(tmp_path / "skills", tmp_path / "evidence")
        n = updater.ingest_field_corrections([
            {"query": "x", "resolved": "x"},   # 同名跳过
            {"query": "", "resolved": "y"},    # 空 query 跳过
            {"query": "a", "resolved": "b"},   # 合法
        ])
        assert n == 1
        cands = _read_jsonl(updater.candidates_path)
        assert cands[0]["query"] == "a"

    def test_ingest_empty_returns_zero(self, tmp_path):
        from agent.excel.core.skill_updater import SkillUpdater
        updater = SkillUpdater(tmp_path / "skills", tmp_path / "evidence")
        assert updater.ingest_field_corrections([]) == 0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

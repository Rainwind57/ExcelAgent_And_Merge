"""方案2 修正学习单测：anti-pattern fix 字段 + apply。

验证：
- AntiPattern.fix 字段 to_dict/load 往返
- ai_induce_anti_pattern 解析 LLM 输出的 fix（白名单键过滤）
- _apply_anti_pattern_fix_filter：命中 fix.skip_outlier_check → 滤离群 issue

运行: python -m pytest server/tests/test_anti_pattern_fix.py -v
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.agent import TableAgent  # noqa: E402
from agent.excel.core.skill_updater import SkillUpdater, AntiPattern  # noqa: E402
from agent.excel.core.step_ai_enhancer import StepAIEnhancer  # noqa: E402


# ── AntiPattern.fix 往返 ────────────────────────────────────────


class TestAntiPatternFixRoundTrip:
    def test_to_dict_includes_fix_when_set(self):
        ap = AntiPattern(
            id="ap_x", type="semantic_pattern", table_stem="pet", sheet="Pet",
            trigger_pattern="新增,编号", action="warn_only",
            fix={"skip_outlier_check": True},
        )
        d = ap.to_dict()
        assert d["fix"] == {"skip_outlier_check": True}

    def test_to_dict_omits_fix_when_empty(self):
        ap = AntiPattern(id="ap_x", type="semantic_pattern",
                         table_stem="pet", sheet="Pet")
        assert "fix" not in ap.to_dict()

    def test_load_anti_patterns_reads_fix(self, tmp_path):
        import yaml
        su = SkillUpdater(tmp_path / "skills", tmp_path / "evidence")
        su.anti_pattern_dir.mkdir(parents=True, exist_ok=True)
        su.anti_patterns_path.write_text(yaml.safe_dump({
            "anti_patterns": [{
                "id": "ap_x", "type": "semantic_pattern",
                "table_stem": "pet", "sheet": "Pet",
                "trigger_pattern": "新增,编号", "action": "warn_only",
                "status": "active", "fix": {"skip_outlier_check": True},
            }],
        }, allow_unicode=True), encoding="utf-8")
        aps = su.load_anti_patterns()
        assert len(aps) == 1
        assert aps[0].fix == {"skip_outlier_check": True}

    def test_upsert_persists_fix_to_staging(self, tmp_path):
        """ai_induction + pending_review 带 fix → 写暂存，fix 保留。"""
        su = SkillUpdater(tmp_path / "skills", tmp_path / "evidence")
        ap = AntiPattern(
            id="ap_ind_pet_Pet_abcd1234", type="semantic_pattern",
            table_stem="pet", sheet="Pet",
            trigger_pattern="新增,编号", action="warn_only",
            status="pending_review", source="ai_induction",
            fix={"skip_outlier_check": True},
        )
        su._apply_anti_pattern_upsert(ap)
        pends = su.load_pending_anti_patterns()
        assert len(pends) == 1
        assert pends[0].fix == {"skip_outlier_check": True}


# ── ai_induce_anti_pattern 解析 fix ────────────────────────────


class TestInduceParsesFix:
    def _make_enhancer(self, llm_raw: str):
        enh = MagicMock()
        enh.ai_induce_anti_pattern.return_value = None
        # 直接造 client.extract_json_from_response 的返回
        enh._call_llm = MagicMock(return_value=llm_raw)
        enh._think = MagicMock()
        enh.client = MagicMock()
        return enh

    def test_fix_whitelist_keys_kept(self, tmp_path):
        """ai_induce_anti_pattern 解析器：fix 白名单键过滤（evil_key 滤除）。"""
        enh = object.__new__(StepAIEnhancer)
        enh._call_llm = MagicMock(return_value="raw")
        enh._think = MagicMock()
        enh.client = MagicMock()
        enh.client.extract_json_from_response = MagicMock(return_value=[{
            "type": "semantic_pattern", "trigger_pattern": "新增,编号",
            "action": "warn_only", "rationale": "r",
            "table_stem": "pet", "sheet": "Pet",
            "fix": {"skip_outlier_check": True, "evil_key": "hack"},
        }])
        out = enh.ai_induce_anti_pattern(
            [{"input": "x", "error_type": "y", "error_detail": "z",
              "entries_summary": "w"}])
        assert out is not None and len(out) == 1
        assert out[0]["fix"] == {"skip_outlier_check": True}  # evil_key 滤除

    def test_no_fix_field_ok(self, tmp_path):
        su = SkillUpdater(tmp_path / "skills", tmp_path / "evidence")
        enh = MagicMock()
        enh.ai_induce_anti_pattern.return_value = [{
            "type": "semantic_pattern", "trigger_pattern": "新增,编号",
            "action": "warn_only", "rationale": "r",
            "table_stem": "pet", "sheet": "Pet",
        }]
        produced = su.induce_anti_patterns(
            [{"input": "x", "error_type": "y", "error_detail": "z",
              "entries_summary": "w"}], enh)
        assert len(produced) == 1
        assert produced[0].fix == {}


# ── _apply_anti_pattern_fix_filter apply ───────────────────────


def _make_agent_for_filter(ap_return: dict | None):
    """轻量 agent：绑 _apply_anti_pattern_fix_filter + mock _check_anti_pattern。"""
    ag = types.SimpleNamespace()
    ag._apply_anti_pattern_fix_filter = TableAgent._apply_anti_pattern_fix_filter.__get__(ag)
    ag._check_anti_pattern = lambda *a, **kw: ap_return
    return ag


def _intent(raw="新增 NPC 编号 10013112007"):
    return types.SimpleNamespace(raw=raw)


class TestApplyFixFilter:
    def test_skip_outlier_check_filters_outlier_issues(self):
        """命中 fix.skip_outlier_check → 滤掉含离群关键词的 issue。"""
        ap = {"fix": {"skip_outlier_check": True}}
        ag = _make_agent_for_filter(ap)
        issues = [
            {"column": "编号", "reason": "值 10013112007 远高于列历史分布（median=10001）"},
            {"column": "名称", "reason": "值为空"},  # 非离群，保留
        ]
        out = ag._apply_anti_pattern_fix_filter("pet", "Pet", _intent(), issues)
        assert len(out) == 1
        assert "分布" not in (out[0].get("reason") or "")

    def test_no_fix_passes_through(self):
        """anti-pattern 无 fix → issue 全保留。"""
        ap = {"action": "warn_only"}  # 无 fix
        ag = _make_agent_for_filter(ap)
        issues = [{"reason": "值远高于分布 median"}]
        out = ag._apply_anti_pattern_fix_filter("pet", "Pet", _intent(), issues)
        assert out == issues

    def test_no_match_passes_through(self):
        """无 anti-pattern 命中 → issue 全保留。"""
        ag = _make_agent_for_filter(None)
        issues = [{"reason": "值远高于分布"}]
        out = ag._apply_anti_pattern_fix_filter("pet", "Pet", _intent(), issues)
        assert out == issues

    def test_empty_issues_noop(self):
        ag = _make_agent_for_filter({"fix": {"skip_outlier_check": True}})
        assert ag._apply_anti_pattern_fix_filter("pet", "Pet", _intent(), []) == []

    def test_non_outlier_issue_kept_when_skip_active(self):
        """fix.skip_outlier_check 命中但 issue 非离群 → 保留。"""
        ap = {"fix": {"skip_outlier_check": True}}
        ag = _make_agent_for_filter(ap)
        issues = [{"reason": "值为空"}, {"reason": "类型不符 int"}]
        out = ag._apply_anti_pattern_fix_filter("pet", "Pet", _intent(), issues)
        assert len(out) == 2


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

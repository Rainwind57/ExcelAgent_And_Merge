"""repair 成功捕获（经验真值层）单测。

验证：
- ingest_repair_success → staging jsonl（可泛化 fix 种类，case-specific 滤除）
- promote_repair_recipes → 达 RECIPE_PROMOTE_HITS 合并入 committed active + 清 staging
- lookup_repair_recipe → active recipe 查
- _capture_repair_recipe（agent helper）→ 调 ingest
- _try_recipe_fast_path → 命中 recipe 直接 apply + re-verify 通过 → 快路径返回

运行: python -m pytest server/tests/test_repair_recipes.py -v
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.skill_updater import (  # noqa: E402
    SkillUpdater, RECIPE_PROMOTE_HITS,
)
import agent.excel.core.agent as agent_mod  # noqa: E402
from agent.excel.core.agent import TableAgent  # noqa: E402


def _new_updater(tmp_path):
    return SkillUpdater(tmp_path / "skills", tmp_path / "evidence")


class _Cls:
    """轻量 ClassifiedError 替身。"""
    def __init__(self, et="type_mismatch", col="名称"):
        from agent.excel.repair.error_classifier import ErrorType
        self.error_type = ErrorType(et) if isinstance(et, str) else et
        self.failed_col = col
        self.failed_val = "x"
        self.root_cause = "r"
        self.confidence = 0.9


# ── skill_updater 侧 ──────────────────────────────────────────


class TestIngestRepairSuccess:
    def test_generalizable_fix_kinds_recorded(self, tmp_path):
        """column_remap/clear_pk/allocate_new_id/add_dependency 入 staging。"""
        up = _new_updater(tmp_path)
        up.ingest_repair_success(
            "column_not_found", "pet", "Pet", "名称",
            {"column_remap": {"名": "名称"}, "value_coerce": {"名": 1}},
            "level1")
        # 只记 generalizable（column_remap），value_coerce 滤
        from agent.excel.core.skill_updater import _read_jsonl
        recs = _read_jsonl(up.repair_recipes_staging_path)
        assert len(recs) == 1
        assert "column_remap" in recs[0]["fix_payload"]
        assert "value_coerce" not in recs[0]["fix_payload"]

    def test_only_case_specific_skipped(self, tmp_path):
        """纯 value_coerce（case-specific）→ 不记。"""
        up = _new_updater(tmp_path)
        up.ingest_repair_success("type_mismatch", "pet", "Pet", "x",
                                 {"value_coerce": {"x": 1}}, "level1")
        assert not up.repair_recipes_staging_path.exists()

    def test_empty_or_no_error_skipped(self, tmp_path):
        up = _new_updater(tmp_path)
        up.ingest_repair_success("", "pet", "Pet", "x",
                                 {"column_remap": {"a": "b"}}, "level1")
        up.ingest_repair_success("type_mismatch", "pet", "Pet", "x", {}, "level1")
        assert not up.repair_recipes_staging_path.exists()


class TestPromoteRepairRecipes:
    def test_below_threshold_not_promoted(self, tmp_path):
        up = _new_updater(tmp_path)
        for _ in range(RECIPE_PROMOTE_HITS - 1):
            up.ingest_repair_success("column_not_found", "pet", "Pet", "名称",
                                     {"column_remap": {"名": "名称"}}, "level1")
        promoted = up.promote_repair_recipes()
        assert promoted == []
        assert not up.repair_recipes_path.exists()

    def test_threshold_met_promoted_to_committed(self, tmp_path):
        up = _new_updater(tmp_path)
        for _ in range(RECIPE_PROMOTE_HITS):
            up.ingest_repair_success("column_not_found", "pet", "Pet", "名称",
                                     {"column_remap": {"名": "名称"}}, "level1")
        promoted = up.promote_repair_recipes()
        assert len(promoted) == 1
        assert promoted[0]["fix_kind"] == "column_remap"
        # committed active
        recipes = up.load_repair_recipes()
        assert len(recipes) == 1
        assert recipes[0]["status"] == "active"
        assert recipes[0]["fix_payload"]["column_remap"] == {"名": "名称"}
        # staging 清空
        from agent.excel.core.skill_updater import _read_jsonl
        assert _read_jsonl(up.repair_recipes_staging_path) == []

    def test_two_signatures_both_promoted(self, tmp_path):
        up = _new_updater(tmp_path)
        for _ in range(RECIPE_PROMOTE_HITS):
            up.ingest_repair_success("column_not_found", "pet", "Pet", "名称",
                                     {"column_remap": {"名": "名称"}}, "level1")
            up.ingest_repair_success("id_conflict", "item", "ItemBase", "iID",
                                     {"allocate_new_id": True}, "level1")
        promoted = up.promote_repair_recipes()
        assert len(promoted) == 2


class TestLookupRepairRecipe:
    def test_active_recipe_found(self, tmp_path):
        up = _new_updater(tmp_path)
        import yaml
        up.repair_recipes_path.write_text(yaml.safe_dump({
            "recipes": [{
                "id": "r1", "error_type": "column_not_found",
                "table_stem": "pet", "sheet": "Pet", "column": "名称",
                "fix_kind": "column_remap",
                "fix_payload": {"column_remap": {"名": "名称"}},
                "status": "active",
            }],
        }, allow_unicode=True), encoding="utf-8")
        r = up.lookup_repair_recipe("column_not_found", "pet", "Pet", "名称")
        assert r is not None
        assert r["fix_payload"]["column_remap"] == {"名": "名称"}

    def test_pending_not_returned(self, tmp_path):
        up = _new_updater(tmp_path)
        import yaml
        up.repair_recipes_path.write_text(yaml.safe_dump({
            "recipes": [{
                "id": "r1", "error_type": "x", "table_stem": "t", "sheet": "s",
                "column": "c", "status": "pending_review",
                "fix_payload": {"column_remap": {"a": "b"}},
            }],
        }, allow_unicode=True), encoding="utf-8")
        assert up.lookup_repair_recipe("x", "t", "s", "c") is None

    def test_no_match_returns_none(self, tmp_path):
        up = _new_updater(tmp_path)
        assert up.lookup_repair_recipe("x", "t", "s", "c") is None


# ── agent helpers ──────────────────────────────────────────────


class TestCaptureRepairRecipe:
    def test_calls_ingest_with_classified_fields(self, monkeypatch, tmp_path):
        up = _new_updater(tmp_path)
        monkeypatch.setattr(agent_mod, "get_skill_updater", lambda: up)
        ag = types.SimpleNamespace(enable_skill=True)
        ag._capture_repair_recipe = TableAgent._capture_repair_recipe.__get__(ag)
        ag._capture_repair_recipe(_Cls("column_not_found", "名称"), "pet", "Pet",
                                  {"column_remap": {"名": "名称"}}, "level1")
        from agent.excel.core.skill_updater import _read_jsonl
        recs = _read_jsonl(up.repair_recipes_staging_path)
        assert len(recs) == 1
        assert recs[0]["error_type"] == "column_not_found"
        assert recs[0]["col"] == "名称"

    def test_skill_off_skips(self, monkeypatch, tmp_path):
        up = _new_updater(tmp_path)
        monkeypatch.setattr(agent_mod, "get_skill_updater", lambda: up)
        ag = types.SimpleNamespace(enable_skill=False)
        ag._capture_repair_recipe = TableAgent._capture_repair_recipe.__get__(ag)
        ag._capture_repair_recipe(_Cls(), "pet", "Pet",
                                  {"column_remap": {"a": "b"}}, "level1")
        assert not up.repair_recipes_staging_path.exists()


class TestTryRecipeFastPath:
    def _make_agent(self, recipe, apply_ok=True, verify_pass=True):
        up = MagicMock()
        up.lookup_repair_recipe.return_value = recipe
        agent_mod.get_skill_updater = lambda: up  # patch（直接改 module attr）
        ag = types.SimpleNamespace(enable_skill=True)
        ag._try_recipe_fast_path = TableAgent._try_recipe_fast_path.__get__(ag)
        ag._rollback_write = MagicMock()
        ag._apply_repair_fix = MagicMock(return_value=apply_ok)
        ag._safe_redispatch = MagicMock(return_value=MagicMock(ok=verify_pass))
        vr = MagicMock(); vr.passed = verify_pass
        ag._verify_write = MagicMock(return_value=vr)
        return ag

    def test_recipe_hit_apply_verify_pass_returns_out(self, monkeypatch):
        recipe = {"id": "r1", "fix_kind": "column_remap",
                  "fix_payload": {"column_remap": {"名": "名称"}}}
        ag = self._make_agent(recipe, apply_ok=True, verify_pass=True)
        res = types.SimpleNamespace(
            ok=False, message="", _skip_summarize=False,
            add_thinking=lambda p, d: None)
        out = ag._try_recipe_fast_path(
            _Cls("column_not_found", "名称"), MagicMock(), Path("pet.xlsx"),
            "Pet", res, None, True)
        assert out is not None
        assert res.ok is True
        assert res._skip_summarize is True

    def test_no_recipe_returns_none(self, monkeypatch):
        ag = self._make_agent(None)
        res = types.SimpleNamespace(
            ok=False, message="", _skip_summarize=False,
            add_thinking=lambda p, d: None)
        out = ag._try_recipe_fast_path(
            _Cls(), MagicMock(), Path("p.xlsx"), "s", res, None, True)
        assert out is None

    def test_apply_fails_returns_none(self, monkeypatch):
        recipe = {"fix_kind": "column_remap",
                  "fix_payload": {"column_remap": {"a": "b"}}}
        ag = self._make_agent(recipe, apply_ok=False)
        res = types.SimpleNamespace(
            ok=False, message="", _skip_summarize=False,
            add_thinking=lambda p, d: None)
        out = ag._try_recipe_fast_path(
            _Cls(), MagicMock(), Path("p.xlsx"), "s", res, None, True)
        assert out is None

    def test_verify_still_fails_returns_none(self, monkeypatch):
        recipe = {"fix_kind": "column_remap",
                  "fix_payload": {"column_remap": {"a": "b"}}}
        ag = self._make_agent(recipe, apply_ok=True, verify_pass=False)
        res = types.SimpleNamespace(
            ok=False, message="", _skip_summarize=False,
            add_thinking=lambda p, d: None)
        out = ag._try_recipe_fast_path(
            _Cls(), MagicMock(), Path("p.xlsx"), "s", res, None, True)
        assert out is None

    def test_skill_off_returns_none(self, monkeypatch):
        recipe = {"fix_kind": "column_remap",
                  "fix_payload": {"column_remap": {"a": "b"}}}
        up = MagicMock(); up.lookup_repair_recipe.return_value = recipe
        agent_mod.get_skill_updater = lambda: up
        ag = types.SimpleNamespace(enable_skill=False)
        ag._try_recipe_fast_path = TableAgent._try_recipe_fast_path.__get__(ag)
        ag._apply_repair_fix = MagicMock()
        res = types.SimpleNamespace(ok=False, add_thinking=lambda p, d: None)
        out = ag._try_recipe_fast_path(_Cls(), MagicMock(), Path("p.xlsx"), "s", res, None, True)
        assert out is None
        ag._apply_repair_fix.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

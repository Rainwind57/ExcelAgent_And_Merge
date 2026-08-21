"""skill-ab-gating 单测（capability: skill-ab-gating）。

验证 D9 / D11：
- 1.3 门控：enable_skill=False 不注入 skill_context；on/off prompt 不同
- 1.4/1.6 lift 门禁：_passes_lift_gate 阈值判定
- 1.5 mini 回归抽样器降级（非项目根 → None）
- 1.7 __init__ 创建 runtime alias 空骨架
- 1.8 decay_scan 高命中别名（hits >= DECAY_KEEP_THRESHOLD）跳过衰减
- promote_with_guard：lift 达标 promote / 不达标回滚+隔离
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.skill_updater import (
    SkillUpdater,
    RELATIVE_LIFT_THRESHOLD,
    DECAY_KEEP_THRESHOLD,
)
from agent.excel.codemaker_parser import CodemakerNLParser


# ── 1.4/1.6 lift 门禁纯判定 ──────────────────────────────────

class TestLiftGate:
    def test_passes_when_lift_meets_threshold(self):
        assert SkillUpdater._passes_lift_gate(0.08) is True

    def test_passes_at_exact_threshold(self):
        assert SkillUpdater._passes_lift_gate(RELATIVE_LIFT_THRESHOLD) is True

    def test_fails_when_lift_below_threshold(self):
        assert SkillUpdater._passes_lift_gate(0.02) is False

    def test_fails_when_lift_negative(self):
        assert SkillUpdater._passes_lift_gate(-0.1) is False

    def test_none_treated_as_degrade_pass(self):
        # None = 环境不可用降级，不阻断
        assert SkillUpdater._passes_lift_gate(None) is True


# ── 1.7 __init__ 创建 runtime alias 空骨架 ──────────────────

class TestRuntimeSkeleton:
    def test_init_creates_skeleton_when_absent(self, tmp_path):
        skills_dir = tmp_path / "skills"
        updater = SkillUpdater(skills_dir)
        assert updater.runtime_aliases_path.exists()
        import yaml
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or {}
        assert data.get("columns") == {}

    def test_init_preserves_existing_skeleton(self, tmp_path):
        skills_dir = tmp_path / "skills"
        runtime_dir = skills_dir / "L2_runtime"
        runtime_dir.mkdir(parents=True)
        import yaml
        existing = {"columns": {"pet": {"Pet": {"名字": "name"}}}}
        (runtime_dir / "column_aliases.runtime.yaml").write_text(
            yaml.safe_dump(existing, allow_unicode=True), encoding="utf-8")
        updater = SkillUpdater(skills_dir)
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or {}
        assert data.get("columns", {}).get("pet", {}).get("Pet", {}).get("名字") == "name"


# ── 1.5 mini 回归抽样器降级 ────────────────────────────────────────

class TestMiniRegressionDegrade:
    def test_degrades_when_skills_dir_outside_project_root(self, tmp_path):
        # tmp_path 不在项目根下 → 降级 None
        updater = SkillUpdater(tmp_path / "skills")
        result = updater._run_mini_regression(sample_size=5)
        assert result is None


# ── 1.8 decay_scan 高命中跳过衰减 ────────────────────────────

class TestDecayKeepHighHits:
    def _make_updater_with_aliases(self, tmp_path, aliases_meta: dict) -> SkillUpdater:
        import yaml
        skills_dir = tmp_path / "skills"
        runtime_dir = skills_dir / "L2_runtime"
        runtime_dir.mkdir(parents=True)
        # columns: {table: {sheet: {query: resolved}}}
        # _meta:   {table: {sheet: {query: {hits, last_seen}}}}
        columns = {}
        meta = {}
        for key, m in aliases_meta.items():
            t, s, q, resolved = key
            columns.setdefault(t, {}).setdefault(s, {})[q] = resolved
            meta.setdefault(t, {}).setdefault(s, {})[q] = m
        data = {"columns": columns, "_meta": meta}
        (runtime_dir / "column_aliases.runtime.yaml").write_text(
            yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
        return SkillUpdater(skills_dir)

    def test_high_hits_alias_kept_active(self, tmp_path):
        from datetime import datetime, timedelta
        old_ts = (datetime.now().astimezone() - timedelta(days=90)).isoformat(timespec="seconds")
        updater = self._make_updater_with_aliases(tmp_path, {
            ("pet", "Pet", "名字", "name"): {"hits": DECAY_KEEP_THRESHOLD, "last_seen": old_ts},
        })
        stats = updater.decay_scan()
        import yaml
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or {}
        # hits>=3 即使 90 天前也保留 active
        assert "名字" in data["columns"]["pet"]["Pet"]
        m = data["_meta"]["pet"]["Pet"]["名字"]
        assert m["status"] == "active"
        assert stats["active"] >= 1

    def test_low_hits_old_alias_decayed(self, tmp_path):
        from datetime import datetime, timedelta
        old_ts = (datetime.now().astimezone() - timedelta(days=90)).isoformat(timespec="seconds")
        updater = self._make_updater_with_aliases(tmp_path, {
            ("pet", "Pet", "别名X", "col_x"): {"hits": 1, "last_seen": old_ts},
        })
        updater.decay_scan()
        import yaml
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or {}
        # hits=1 + 90 天 → 移除
        sheet = data["columns"].get("pet", {}).get("Pet", {})
        assert "别名X" not in sheet


# ── promote_with_guard lift 门禁 ─────────────────────────────

class TestPromoteGuardLift:
    def _make_updater(self, tmp_path) -> SkillUpdater:
        return SkillUpdater(tmp_path / "skills")

    def _alias_candidate(self) -> dict:
        return {
            "kind": "column_alias",
            "table_stem": "pet", "sheet": "Pet",
            "query": "名字", "resolved": "name",
            "items": [{"ts": "2026-08-01T00:00:00+08:00", "confidence": 0.5}],
            "avg_conf": 0.5,
        }

    def test_promote_passes_on_high_lift(self, tmp_path, monkeypatch):
        updater = self._make_updater(tmp_path)
        monkeypatch.setattr(updater, "_run_mini_regression",
                            lambda: {"pass_off": 0.10, "pass_on": 0.18, "lift": 0.08})
        ok = updater.promote_with_guard(self._alias_candidate())
        assert ok is True
        import yaml
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or {}
        assert data["columns"]["pet"]["Pet"]["名字"] == "name"

    def test_promote_rolls_back_on_low_lift(self, tmp_path, monkeypatch):
        updater = self._make_updater(tmp_path)
        monkeypatch.setattr(updater, "_run_mini_regression",
                            lambda: {"pass_off": 0.50, "pass_on": 0.52, "lift": 0.02})
        ok = updater.promote_with_guard(self._alias_candidate())
        assert ok is False
        import yaml
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or {}
        # 回滚后 columns 无该别名
        assert data.get("columns", {}).get("pet", {}).get("Pet", {}).get("名字") is None
        # 隔离区有记录
        assert updater.quarantine_dir.exists()
        qfiles = list(updater.quarantine_dir.glob("*.json"))
        assert len(qfiles) >= 1

    def test_promote_degrades_when_regression_none(self, tmp_path, monkeypatch):
        updater = self._make_updater(tmp_path)
        monkeypatch.setattr(updater, "_run_mini_regression", lambda: None)
        ok = updater.promote_with_guard(self._alias_candidate())
        # 降级直接通过
        assert ok is True
        import yaml
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or {}
        assert data["columns"]["pet"]["Pet"]["名字"] == "name"


# ── 1.3 门控 on/off prompt ───────────────────────────────────

class TestEnableSkillGating:
    def _make_parser(self, enable_skill: bool, monkeypatch) -> CodemakerNLParser:
        # 绕过真实 client / model 构造
        parser = CodemakerNLParser(client=None, enable_skill=enable_skill)
        # 固定 _build_prompt 返回，便于断言 skill_context 注入差异
        monkeypatch.setattr(parser, "_build_prompt",
                            lambda text, context="", mode=None: f"BASE:{text}")
        return parser

    def test_off_returns_base_only(self, monkeypatch):
        parser = self._make_parser(enable_skill=False, monkeypatch=monkeypatch)
        # 即便 build_skill_context 能产出，off 也不注入
        out = parser._build_prompt_with_skills("添加宠物 名字=小白")
        assert out == "BASE:添加宠物 名字=小白"

    def test_on_injects_skill_context_when_available(self, monkeypatch):
        parser = self._make_parser(enable_skill=True, monkeypatch=monkeypatch)
        # build_skill_context 在函数内 `from .skill_context import build_skill_context`
        # 局部 import，patch 源模块属性
        import agent.excel.skill_context as sc_mod
        fake_ctx = "## 目标表列名候选\npet.Pet: 名字/类型"
        monkeypatch.setattr(sc_mod, "build_skill_context",
                            lambda text: fake_ctx, raising=False)
        out = parser._build_prompt_with_skills("添加宠物 名字=小白")
        assert fake_ctx in out
        assert "BASE:添加宠物" in out

    def test_on_off_prompt_differ(self, monkeypatch):
        import agent.excel.skill_context as sc_mod
        fake_ctx = "## 目标表列名候选\npet.Pet: 名字"
        monkeypatch.setattr(sc_mod, "build_skill_context",
                            lambda text: fake_ctx, raising=False)
        on = self._make_parser(True, monkeypatch)._build_prompt_with_skills("x")
        off = self._make_parser(False, monkeypatch)._build_prompt_with_skills("x")
        assert on != off


# ── 入口 ─────────────────────────────────────────────────────

def _run_all():
    pytest.main([__file__, "-v"])


if __name__ == "__main__":
    _run_all()

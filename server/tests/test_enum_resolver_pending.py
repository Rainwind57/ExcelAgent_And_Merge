"""enum_resolver 动态补充单测（capability: enum-mapping-pipeline D10.1-D10.4）。

验证：
- 10.1 register_label 写 pending
- 10.2 pending yaml 读写（不修改 L1_derived/enum_mappings.yaml）
- 10.3 promote 达标合并 L1 + 移除 pending；未达标保留 pending
- 7.5 confidence < 0.7 拒绝
- resolve_label 查 pending 命中
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel import enum_resolver as er_mod
from agent.excel.enum_resolver import EnumResolver
from agent.excel.skill_updater import SkillUpdater


@pytest.fixture
def isolated_paths(tmp_path, monkeypatch):
    """隔离 _SKILLS_DIR + pending 路径到 tmp_path。"""
    skills_dir = tmp_path / "skills"
    l1_dir = skills_dir / "L1_derived"
    l1_dir.mkdir(parents=True)
    pending_dir = skills_dir / "_pending"
    pending_dir.mkdir(parents=True)
    # L1 enum_mappings.yaml 空骨架
    l1_path = l1_dir / "enum_mappings.yaml"
    l1_path.write_text("version: '1.0'\ntables: {}\n", encoding="utf-8")
    # monkeypatch 模块级路径
    monkeypatch.setattr(er_mod, "_SKILLS_DIR", skills_dir, raising=False)
    pending_path = pending_dir / "enum_candidates.yaml"
    monkeypatch.setattr(er_mod, "_PENDING_ENUM_PATH", pending_path, raising=False)
    # _load_yaml 读 L1_derived（因 monkeypatch _SKILLS_DIR，_load_yaml 内 _SKILLS_DIR 动态读）
    return {"skills_dir": skills_dir, "l1_path": l1_path, "pending_path": pending_path}


# ── 10.1/10.2 register 写 pending ────────────────────────────

class TestRegisterLabel:
    def test_register_writes_pending_not_l1(self, isolated_paths):
        resolver = EnumResolver.load()
        ok = resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.9)
        assert ok is True
        # pending 文件有记录
        data = yaml.safe_load(isolated_paths["pending_path"].read_text(encoding="utf-8"))
        cands = data.get("candidates", [])
        assert any(c["stem"] == "pet" and c["label"] == "攻击" and c["value"] == 1
                   for c in cands)
        # L1 不变（仍是空 tables）
        l1 = yaml.safe_load(isolated_paths["l1_path"].read_text(encoding="utf-8"))
        assert l1.get("tables") == {}

    def test_low_confidence_rejected(self, isolated_paths):
        resolver = EnumResolver.load()
        ok = resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.5)
        assert ok is False
        # pending 无记录（文件可能未创建，用 _load_pending_raw 兼容）
        data = resolver._load_pending_raw()
        assert data.get("candidates", []) == []

    def test_resolve_label_hits_pending(self, isolated_paths):
        resolver = EnumResolver.load()
        resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.9)
        # resolve_label 应能查到 pending
        assert resolver.resolve_label("pet", "Pet", "类型", "攻击") == 1

    def test_resolve_label_miss_returns_none(self, isolated_paths):
        resolver = EnumResolver.load()
        assert resolver.resolve_label("pet", "Pet", "类型", "不存在") is None

    def test_dedup_keeps_higher_confidence(self, isolated_paths):
        resolver = EnumResolver.load()
        resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.8)
        resolver.register_label("pet", "Pet", "类型", "攻击", 2, 0.95)  # 更高 → 替换
        data = yaml.safe_load(isolated_paths["pending_path"].read_text(encoding="utf-8"))
        cands = [c for c in data["candidates"] if c["label"] == "攻击"]
        assert len(cands) == 1
        assert cands[0]["value"] == 2
        assert cands[0]["confidence"] == 0.95

    def test_already_in_l1_skips_pending(self, isolated_paths):
        # L1 已有 pet.Pet.类型=攻击1
        l1_data = {"version": "1.0", "tables": {"pet": {"Pet": {"columns": {
            "类型": {"type": "int", "values": [{"label": "攻击", "value": 1}]}}}}}}
        isolated_paths["l1_path"].write_text(
            yaml.safe_dump(l1_data, allow_unicode=True), encoding="utf-8")
        resolver = EnumResolver.load()
        # 再 register 同映射 → 跳过
        ok = resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.9)
        assert ok is False


# ── 10.3 promote 合并 L1 ─────────────────────────────────────

class TestPromoteEnum:
    def _make_updater(self, skills_dir) -> SkillUpdater:
        return SkillUpdater(skills_dir)

    def test_promote达标_merges_l1_clears_pending(self, isolated_paths, monkeypatch):
        resolver = EnumResolver.load()
        resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.9)
        resolver.register_label("pet", "Pet", "类型", "治疗", 2, 0.85)
        updater = self._make_updater(isolated_paths["skills_dir"])
        # mock mini 回归高 lift
        monkeypatch.setattr(updater, "_run_mini_regression",
                            lambda: {"pass_off": 0.10, "pass_on": 0.18, "lift": 0.08})
        merged = updater.try_promote_enum()
        assert len(merged) == 2
        # L1 已合并
        l1 = yaml.safe_load(isolated_paths["l1_path"].read_text(encoding="utf-8"))
        cols = l1["tables"]["pet"]["Pet"]["columns"]["类型"]["values"]
        labels = {v["label"]: v["value"] for v in cols}
        assert labels == {"攻击": 1, "治疗": 2}
        # pending 已清空
        pdata = yaml.safe_load(isolated_paths["pending_path"].read_text(encoding="utf-8"))
        assert pdata.get("candidates", []) == []

    def test_promote未达标_keeps_pending_l1_unchanged(self, isolated_paths, monkeypatch):
        resolver = EnumResolver.load()
        resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.9)
        updater = self._make_updater(isolated_paths["skills_dir"])
        # mock 低 lift
        monkeypatch.setattr(updater, "_run_mini_regression",
                            lambda: {"pass_off": 0.50, "pass_on": 0.51, "lift": 0.01})
        merged = updater.try_promote_enum()
        assert merged == []
        # L1 不变（空 tables）
        l1 = yaml.safe_load(isolated_paths["l1_path"].read_text(encoding="utf-8"))
        assert l1.get("tables") == {}
        # pending 保留
        pdata = yaml.safe_load(isolated_paths["pending_path"].read_text(encoding="utf-8"))
        assert len(pdata.get("candidates", [])) == 1

    def test_promote_degrades_when_regression_none(self, isolated_paths, monkeypatch):
        resolver = EnumResolver.load()
        resolver.register_label("pet", "Pet", "类型", "攻击", 1, 0.9)
        updater = self._make_updater(isolated_paths["skills_dir"])
        monkeypatch.setattr(updater, "_run_mini_regression", lambda: None)
        merged = updater.try_promote_enum()
        # 降级直接通过
        assert len(merged) == 1
        l1 = yaml.safe_load(isolated_paths["l1_path"].read_text(encoding="utf-8"))
        assert "pet" in l1["tables"]

    def test_promote_empty_pending_noop(self, isolated_paths, monkeypatch):
        updater = self._make_updater(isolated_paths["skills_dir"])
        called = {"n": 0}
        def fake():
            called["n"] += 1
            return {"lift": 0.9}
        monkeypatch.setattr(updater, "_run_mini_regression", fake)
        merged = updater.try_promote_enum()
        assert merged == []
        assert called["n"] == 0  # 无候选不跑回归


# ── 入口 ─────────────────────────────────────────────────────

def _run_all():
    pytest.main([__file__, "-v"])


if __name__ == "__main__":
    _run_all()

"""B 成功学习通道单测：runtime alias → committed column_short_form.yaml。

验证 promote_runtime_aliases_to_committed：
- 达阈值（hits ≥ ALIAS_COMMIT_HITS + conf ≥ ALIAS_COMMIT_MIN_CONF）的 runtime alias
  → 追加到 committed column_short_form.yaml 的短形式列表（去重）。
- 未达阈值的不动。
- runtime meta 标 committed=True 避免重复提升。
- committed 只增不删，curated 既有条目保留。

运行: python -m pytest server/tests/test_alias_commit_promotion.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.skill_updater import (  # noqa: E402
    SkillUpdater, ALIAS_COMMIT_HITS, ALIAS_COMMIT_MIN_CONF,
)


def _new_updater(tmp_path):
    return SkillUpdater(tmp_path / "skills", tmp_path / "evidence")


def _seed_committed(updater: SkillUpdater, columns: dict) -> None:
    """写 committed column_short_form.yaml 骨架。"""
    import yaml
    updater.committed_aliases_path.parent.mkdir(parents=True, exist_ok=True)
    data = {"version": "1.0", "columns": columns}
    updater.committed_aliases_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def _seed_runtime(updater: SkillUpdater, table: str, sheet: str,
                  query: str, resolved: str, hits: int, conf: float) -> None:
    import yaml
    data = {"columns": {}, "_meta": {}}
    if updater.runtime_aliases_path.exists():
        data = yaml.safe_load(updater.runtime_aliases_path.read_text(encoding="utf-8")) or data
    data.setdefault("columns", {}).setdefault(table, {}).setdefault(sheet, {})[query] = resolved
    data.setdefault("_meta", {}).setdefault(table, {}).setdefault(sheet, {})[query] = {
        "hits": hits, "confidence_avg": conf, "source": "runtime",
    }
    updater.runtime_aliases_path.parent.mkdir(parents=True, exist_ok=True)
    updater.runtime_aliases_path.write_text(
        yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


class TestPromoteRuntimeAliasesToCommitted:
    def test_threshold_met_alias_appended_to_committed(self, tmp_path):
        """hits=5 conf=0.7 ≥ 阈值 → query 追加到 committed 短形式列表。"""
        up = _new_updater(tmp_path)
        _seed_committed(up, {"pet": {"Pet": {"名称": ["名", "名字"]}}})
        _seed_runtime(up, "pet", "Pet", "称呼", "名称",
                      hits=ALIAS_COMMIT_HITS, conf=0.7)
        promoted = up.promote_runtime_aliases_to_committed()
        assert len(promoted) == 1
        assert promoted[0]["query"] == "称呼"
        assert promoted[0]["resolved"] == "名称"
        # committed 短形式列表含新 query + 去重（既有条目保留）
        import yaml
        cdata = yaml.safe_load(up.committed_aliases_path.read_text(encoding="utf-8"))
        short_list = cdata["columns"]["pet"]["Pet"]["名称"]
        assert "称呼" in short_list
        assert "名" in short_list  # 既有 curated 保留
        assert short_list.count("称呼") == 1  # 去重

    def test_below_threshold_not_promoted(self, tmp_path):
        """hits=2 < ALIAS_COMMIT_HITS → 不动。"""
        up = _new_updater(tmp_path)
        _seed_committed(up, {"pet": {"Pet": {"名称": ["名"]}}})
        _seed_runtime(up, "pet", "Pet", "称呼", "名称",
                      hits=ALIAS_COMMIT_HITS - 1, conf=0.9)
        promoted = up.promote_runtime_aliases_to_committed()
        assert promoted == []
        import yaml
        cdata = yaml.safe_load(up.committed_aliases_path.read_text(encoding="utf-8"))
        assert "称呼" not in cdata["columns"]["pet"]["Pet"]["名称"]

    def test_low_conf_not_promoted(self, tmp_path):
        """hits 达标但 conf < ALIAS_COMMIT_MIN_CONF → 不动。"""
        up = _new_updater(tmp_path)
        _seed_committed(up, {"pet": {"Pet": {"名称": ["名"]}}})
        _seed_runtime(up, "pet", "Pet", "称呼", "名称",
                      hits=ALIAS_COMMIT_HITS, conf=ALIAS_COMMIT_MIN_CONF - 0.1)
        promoted = up.promote_runtime_aliases_to_committed()
        assert promoted == []

    def test_runtime_meta_marked_committed(self, tmp_path):
        """提升后 runtime meta 标 committed=True，二次调不重复。"""
        up = _new_updater(tmp_path)
        _seed_committed(up, {"pet": {"Pet": {"名称": ["名"]}}})
        _seed_runtime(up, "pet", "Pet", "称呼", "名称",
                      hits=ALIAS_COMMIT_HITS, conf=0.8)
        up.promote_runtime_aliases_to_committed()
        import yaml
        rdata = yaml.safe_load(up.runtime_aliases_path.read_text(encoding="utf-8"))
        assert rdata["_meta"]["pet"]["Pet"]["称呼"]["committed"] is True
        # 二次调 → 无新提升（已 committed）
        promoted2 = up.promote_runtime_aliases_to_committed()
        assert promoted2 == []

    def test_new_column_creates_entry(self, tmp_path):
        """resolved 列在 committed 不存在 → 新建 [query] 条目。"""
        up = _new_updater(tmp_path)
        _seed_committed(up, {"pet": {"Pet": {"名称": ["名"]}}})
        _seed_runtime(up, "pet", "Pet", "颜色", "毛色",
                      hits=ALIAS_COMMIT_HITS + 2, conf=0.9)
        promoted = up.promote_runtime_aliases_to_committed()
        assert len(promoted) == 1
        import yaml
        cdata = yaml.safe_load(up.committed_aliases_path.read_text(encoding="utf-8"))
        assert cdata["columns"]["pet"]["Pet"]["毛色"] == ["颜色"]

    def test_empty_runtime_noop(self, tmp_path):
        up = _new_updater(tmp_path)
        _seed_committed(up, {"pet": {"Pet": {"名称": ["名"]}}})
        # runtime skeleton 空（__init__ _ensure_runtime_aliases_skeleton）
        assert up.promote_runtime_aliases_to_committed() == []

    def test_existing_short_form_not_duplicated(self, tmp_path):
        """runtime query 已在 committed 短形式列表 → 不重复 append。"""
        up = _new_updater(tmp_path)
        _seed_committed(up, {"pet": {"Pet": {"名称": ["名", "称呼"]}}})
        _seed_runtime(up, "pet", "Pet", "称呼", "名称",
                      hits=ALIAS_COMMIT_HITS, conf=0.8)
        promoted = up.promote_runtime_aliases_to_committed()
        assert len(promoted) == 1  # 仍提升（标 committed）
        import yaml
        cdata = yaml.safe_load(up.committed_aliases_path.read_text(encoding="utf-8"))
        short_list = cdata["columns"]["pet"]["Pet"]["名称"]
        assert short_list.count("称呼") == 1  # 去重


if __name__ == "__main__":
    pytest.main([__file__, "-v"])

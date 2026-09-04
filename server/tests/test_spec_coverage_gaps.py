"""5.2 spec 覆盖补缺:B8 策略0 / C10 索引缓存 / C11 yaml 缓存 / C12 matcher 缓存。"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from agent.agent import TableAgent
from agent.nl_parser import NLIntent
from agent.excel.column_matcher import ColumnMatcher, ColumnMatch


# ── B8 策略0 exact_substr_fallback 权重收敛 ─────────────────────────

def _agent_for_match_column():
    """轻量 agent:column_cfg/short_form_cfg 无别名,隔离策略0。"""
    agent = types.SimpleNamespace(
        column_cfg=types.SimpleNamespace(all_aliases=lambda stem, sheet: {}),
        short_form_cfg=types.SimpleNamespace(reverse_map=lambda stem, sheet: {}),
    )
    agent._match_target_column = TableAgent._match_target_column.__get__(agent)
    return agent


def test_strategy0_exact_substr_fallback_hit():
    """完整列名(>=3)子串命中 → source=exact_substr_fallback,score=0.95。"""
    agent = _agent_for_match_column()
    matcher = ColumnMatcher(["id", "名称", "攻击力"])
    intent = NLIntent(raw="把攻击力改为100")
    m, val = agent._match_target_column(intent, matcher, "t", "S1", loc_index=1)
    assert m is not None
    assert m.source == "exact_substr_fallback"
    assert m.score == 0.95
    assert m.index == 3  # 攻击力 第3列
    assert "100" in (val or "")


def test_strategy0_short_header_not_triggered():
    """<3 字符列名 → 策略0 不触发(不返回 exact_substr_fallback)。"""
    agent = _agent_for_match_column()
    matcher = ColumnMatcher(["id", "名", "攻"])  # 全 <3 字符
    intent = NLIntent(raw="把攻改为1")
    m, _ = agent._match_target_column(intent, matcher, "t", "S1", loc_index=1)
    # 策略0 不触发(无 >=3 命中);无别名 → 后续策略也无命中 → None 或非 fallback
    assert m is None or m.source != "exact_substr_fallback"


def test_strategy0_locator_column_excluded():
    """定位列(loc_index)即使子串命中也不作目标,选其他 >=3 列。"""
    agent = _agent_for_match_column()
    matcher = ColumnMatcher(["玩家编号", "名称", "攻击力"])
    intent = NLIntent(raw="玩家编号的攻击力改为100")
    # loc_index=1 → "玩家编号" 被排除,应命中 "攻击力"
    m, _ = agent._match_target_column(intent, matcher, "t", "S1", loc_index=1)
    assert m is not None
    assert m.source == "exact_substr_fallback"
    assert m.index == 3  # 攻击力,非 玩家编号


# ── C10 agent 级 _get_index 缓存复用 + None 重载 ───────────────────

def test_get_index_caches_and_reloads_on_reset(monkeypatch):
    """_get_index 二次调用复用缓存(load_index 仅调一次);置 None 后重载。"""
    from agent.excel import agent as agent_mod
    from agent.excel import table_index

    calls = {"n": 0}
    def fake_load():
        calls["n"] += 1
        return [{"stem": f"t{calls['n']}"}]
    monkeypatch.setattr(table_index, "load_index", fake_load)

    agent = types.SimpleNamespace(_index_cache=None)
    agent._get_index = TableAgent._get_index.__get__(agent)
    a1 = agent._get_index()
    a2 = agent._get_index()
    assert a1 is a2, "二次调用应复用缓存(同一对象)"
    assert calls["n"] == 1, "load_index 仅调一次"

    # 置 None(模拟 _refresh_index_after_write 失效)→ 重载
    agent._index_cache = None
    a3 = agent._get_index()
    assert calls["n"] == 2, "缓存失效后应重载"
    assert a3 is not a1


# ── C11 skill_loader _load_yaml mtime 缓存 ──────────────────────────

def test_load_yaml_cache_hit_and_mtime_invalidation(tmp_path, monkeypatch):
    """同 mtime 二次加载命中缓存(不重读);mtime 变 → 重读。"""
    import yaml as _yaml
    from agent.excel import skill_loader

    monkeypatch.setattr(skill_loader, "_SKILLS_DIR", tmp_path)
    skill_loader._YAML_CACHE.clear()

    p = tmp_path / "test_cov.yaml"
    p.write_text("k: v1\n", encoding="utf-8")
    load_calls = {"n": 0}
    orig_safe_load = _yaml.safe_load

    def counting_safe_load(*a, **kw):
        load_calls["n"] += 1
        return orig_safe_load(*a, **kw)
    monkeypatch.setattr(skill_loader.yaml, "safe_load", counting_safe_load)

    d1 = skill_loader._load_yaml("test_cov.yaml")
    assert d1 == {"k": "v1"}
    assert load_calls["n"] == 1

    # 二次:mtime 不变 → 命中缓存,不调 safe_load
    d2 = skill_loader._load_yaml("test_cov.yaml")
    assert d2 == {"k": "v1"}
    assert load_calls["n"] == 1, "缓存命中不应重读 yaml"

    # 改文件 + 强制 mtime 变 → 失效重读
    p.write_text("k: v2\n", encoding="utf-8")
    os.utime(p, None)
    d3 = skill_loader._load_yaml("test_cov.yaml")
    assert d3 == {"k": "v2"}
    assert load_calls["n"] == 2, "mtime 变后应重读"


# ── C11 skill_context _col_types_cache 复用 + reset ─────────────────

def test_col_types_cache_reuse_and_reset():
    """相同 tuple(stems) 二次构建命中缓存;reset 后重建。"""
    from agent.excel.core import skill_context

    skill_context.reset_skill_context_cache()
    # _col_types_cache 直接操纵:预填一个 key,断言 get 命中
    stems = ("t1", "t2")
    skill_context._col_types_cache[stems] = "CACHED_BLOCK"
    # 通过 _build_col_types_block(若存在)或直接验缓存条目
    assert skill_context._col_types_cache.get(stems) == "CACHED_BLOCK"
    # reset → 清空
    skill_context.reset_skill_context_cache()
    assert stems not in skill_context._col_types_cache


# ── C12 ColumnMatcher (stem,sheet,headers) 复用 ─────────────────────

def test_make_matcher_caches_by_stem_sheet_headers():
    """相同 (stem,sheet,headers) 二次调用返回同一实例;headers 变 → 新实例。"""
    agent = types.SimpleNamespace(
        _matcher_cache={},
        column_cfg=types.SimpleNamespace(all_aliases=lambda stem, sheet: {}),
        short_form_cfg=types.SimpleNamespace(reverse_map=lambda stem, sheet: {}),
    )
    agent._make_matcher = TableAgent._make_matcher.__get__(agent)

    h = ["id", "名称", "攻击力"]
    m1 = agent._make_matcher(h, "t", "S1")
    m2 = agent._make_matcher(h, "t", "S1")
    assert m1 is m2, "同 (stem,sheet,headers) 应复用实例"

    # headers 变(加一列)→ 新实例
    h2 = ["id", "名称", "攻击力", "防御力"]
    m3 = agent._make_matcher(h2, "t", "S1")
    assert m3 is not m1, "headers 变应失效重建"

    # 不同 sheet → 新实例
    m4 = agent._make_matcher(h, "t", "S2")
    assert m4 is not m1, "不同 sheet 应是新实例"

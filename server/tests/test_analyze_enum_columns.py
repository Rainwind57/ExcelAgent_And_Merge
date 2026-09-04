"""LLM 辅助枚举发现单测（capability: enum-mapping-pipeline D10 / 7.1-7.6）。

验证：
- 7.1/7.2 analyze_enum_column 模块 + prompt 模板
- 7.3 _coerce_value 硬错误→LLM 推断→register_label→重试成功
- 7.4 每列每会话缓存
- 7.5 confidence < 0.7 拒绝
- 7.6 硬错误→LLM 推断 confidence=0.9→重试成功；confidence=0.5→拒绝
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.excel.core.analyze_enum_columns as ae_mod
from agent.excel.core import enum_resolver as er_mod
from agent.excel.agent import TableAgent


@pytest.fixture
def isolated_enum(tmp_path, monkeypatch):
    """隔离 enum_resolver 路径 + analyze 缓存。"""
    skills_dir = tmp_path / "skills"
    (skills_dir / "L1_derived").mkdir(parents=True)
    (skills_dir / "_pending").mkdir(parents=True)
    (skills_dir / "L1_derived" / "enum_mappings.yaml").write_text(
        "version: '1.0'\ntables: {}\n", encoding="utf-8")
    monkeypatch.setattr(er_mod, "_SKILLS_DIR", skills_dir, raising=False)
    monkeypatch.setattr(er_mod, "_PENDING_ENUM_PATH",
                        skills_dir / "_pending" / "enum_candidates.yaml", raising=False)
    ae_mod.reset_session_cache()
    # 重置 enum_resolver 单例（避免跨测试内存 pending 污染）
    from agent.excel.core.enum_resolver import reset_enum_resolver
    reset_enum_resolver()
    return skills_dir


# ── 7.1/7.2/7.4 analyze_enum_column ─────────────────────────

class TestAnalyzeEnumColumn:
    def test_llm_high_confidence_returns_mapping(self, isolated_enum):
        def llm_call(prompt):
            assert "类型" in prompt
            assert "攻击" in prompt
            return "```yaml\n攻击:\n  value: 1\n  confidence: 0.9\n```"
        out = ae_mod.analyze_enum_column("pet", "Pet", "类型", "攻击", llm_call_fn=llm_call)
        assert "攻击" in out
        assert out["攻击"]["value"] == 1
        assert out["攻击"]["confidence"] == 0.9

    def test_no_llm_fn_returns_empty(self, isolated_enum):
        out = ae_mod.analyze_enum_column("pet", "Pet", "类型", "攻击", llm_call_fn=None)
        assert out == {}

    def test_llm_failure_returns_empty(self, isolated_enum):
        def llm_call(prompt):
            raise RuntimeError("LLM down")
        out = ae_mod.analyze_enum_column("pet", "Pet", "类型", "攻击", llm_call_fn=llm_call)
        assert out == {}

    def test_session_cache_avoids_repeat_llm_call(self, isolated_enum):
        calls = {"n": 0}
        def llm_call(prompt):
            calls["n"] += 1
            return "```yaml\n攻击:\n  value: 1\n  confidence: 0.9\n```"
        ae_mod.analyze_enum_column("pet", "Pet", "类型", "攻击", llm_call_fn=llm_call)
        ae_mod.analyze_enum_column("pet", "Pet", "类型", "攻击", llm_call_fn=llm_call)
        assert calls["n"] == 1  # 缓存命中，第二次不调 LLM

    def test_parse_yaml_without_codeblock(self, isolated_enum):
        def llm_call(prompt):
            return "攻击:\n  value: 1\n  confidence: 0.8\n"
        out = ae_mod.analyze_enum_column("pet", "Pet", "类型", "攻击", llm_call_fn=llm_call)
        assert out["攻击"]["value"] == 1


# ── 7.3/7.5/7.6 _try_analyze_enum + _coerce_value 集成 ──────

def _make_agent(monkeypatch, llm_resp: str, isolated_enum):
    """轻量 agent 绑定 _try_analyze_enum + _coerce_value，mock parser.client.prompt。"""
    resp_obj = types.SimpleNamespace(response_text=llm_resp)
    client = types.SimpleNamespace(prompt=lambda sid, prompt, model=None, cancel_event=None: resp_obj)
    parser = types.SimpleNamespace(client=client, model=None,
                                  _ensure_session=lambda: "sid1")
    agent = types.SimpleNamespace(parser=parser)
    agent._try_analyze_enum = TableAgent._try_analyze_enum.__get__(agent)
    agent._coerce_value = TableAgent._coerce_value.__get__(agent)
    return agent


class TestTryAnalyzeEnum:
    def test_high_confidence_registers_and_returns_int(self, isolated_enum, monkeypatch):
        agent = _make_agent(monkeypatch,
                            "```yaml\n攻击:\n  value: 1\n  confidence: 0.9\n```",
                            isolated_enum)
        out = agent._try_analyze_enum("pet", "Pet", "类型", "攻击")
        assert out == 1
        # pending 已写
        from agent.excel.core.enum_resolver import EnumResolver
        resolver = EnumResolver.load()
        assert resolver.resolve_label("pet", "Pet", "类型", "攻击") == 1

    def test_low_confidence_rejected(self, isolated_enum, monkeypatch):
        agent = _make_agent(monkeypatch,
                            "```yaml\n攻击:\n  value: 1\n  confidence: 0.5\n```",
                            isolated_enum)
        out = agent._try_analyze_enum("pet", "Pet", "类型", "攻击")
        assert out is None  # confidence < 0.7 拒绝
        # pending 无记录
        from agent.excel.core.enum_resolver import EnumResolver
        resolver = EnumResolver.load()
        assert resolver.resolve_label("pet", "Pet", "类型", "攻击") is None

    def test_no_client_returns_none(self, isolated_enum, monkeypatch):
        agent = types.SimpleNamespace(parser=types.SimpleNamespace(client=None))
        agent._try_analyze_enum = TableAgent._try_analyze_enum.__get__(agent)
        assert agent._try_analyze_enum("pet", "Pet", "类型", "攻击") is None


class TestCoerceValueIntegration:
    def test_hard_error_triggers_llm_then_success(self, isolated_enum, monkeypatch):
        """7.6: 硬错误→LLM 推断 confidence=0.9→重试成功。"""
        agent = _make_agent(monkeypatch,
                            "```yaml\n攻击:\n  value: 1\n  confidence: 0.9\n```",
                            isolated_enum)
        val, _, warn, error = agent._coerce_value("int", "攻击", "pet", "Pet", "类型", _allow_rejudge=False)
        assert val == 1
        assert error is None

    def test_hard_error_llm_low_confidence_keeps_error(self, isolated_enum, monkeypatch):
        """7.6: confidence=0.5→拒绝→走原硬错误。"""
        agent = _make_agent(monkeypatch,
                            "```yaml\n攻击:\n  value: 1\n  confidence: 0.5\n```",
                            isolated_enum)
        val, _, warn, error = agent._coerce_value("int", "攻击", "pet", "Pet", "类型", _allow_rejudge=False)
        assert error is not None
        assert "无法转为整数" in error


# ── 入口 ─────────────────────────────────────────────────────

def _run_all():
    pytest.main([__file__, "-v"])


if __name__ == "__main__":
    _run_all()

"""4-Step V2 流水线配置开关单测（§1.1 / §1.3）。

验证：
  - Configuration V2 字段默认值（默认 ON，单一开关）
  - env 覆盖（CODEMAKER_EXCEL_PIPELINE_V2 / SCHEMADRIVEN_DECOMPOSE /
    SCHEMA_FETCH_CONCURRENCY / SCHEMA_FETCH_SHEET_LIMIT /
    SPLITTER_DECOMPOSE_THRESHOLD）
  - agent.py splitter fast-path 阈值 env 读取逻辑等价
  - 关闭时退回旧 6 步 pipeline（=0 显式降级）

不依赖 codemaker serve：仅测配置加载与 env 解析逻辑。
"""
from __future__ import annotations

import os
import sys

# 收口 sys.path：server/ 在 path → agent.* 命名空间（与 conftest 一致）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.configuration import Configuration


class Test4StepConfigDefaults:
    """V2 默认 ON（单一开关），=0 显式降级到 6 步。"""

    def test_excel_pipeline_v2_default_on(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_EXCEL_PIPELINE_V2", raising=False)
        cfg = Configuration()
        assert cfg.excel_pipeline_v2 is True, "默认 V2 ON（统一单一开关）"

    def test_schema_driven_decompose_default_off(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_SCHEMADRIVEN_DECOMPOSE", raising=False)
        cfg = Configuration()
        assert cfg.schema_driven_decompose is False, "默认必须关（splitter 为主、LLM 兜底现状）"

    def test_schema_fetch_concurrency_default_8(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_SCHEMA_FETCH_CONCURRENCY", raising=False)
        cfg = Configuration()
        assert cfg.schema_fetch_concurrency == 8

    def test_schema_fetch_sheet_limit_default_15(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_SCHEMA_FETCH_SHEET_LIMIT", raising=False)
        cfg = Configuration()
        assert cfg.schema_fetch_sheet_limit == 15

    def test_splitter_decompose_threshold_default_2(self, monkeypatch):
        """默认 2 = 现状（cross_intents_nl 长度 < 2 触发 _llm_chain_decompose）。"""
        monkeypatch.delenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", raising=False)
        cfg = Configuration()
        assert cfg.splitter_decompose_threshold == 2

    def test_execute_no_llm_default_off(self, monkeypatch):
        """§3 ExecuteAgent 去 LLM 默认关（保持现状含 LLM 诊断+重试+修复）。"""
        monkeypatch.delenv("CODEMAKER_EXECUTE_NO_LLM", raising=False)
        cfg = Configuration()
        assert cfg.execute_no_llm is False

    def test_defaults_excel_pipeline_v2(self, monkeypatch):
        """默认 V2 ON（excel_pipeline_v2=True）。schema_driven_decompose 仍默认关。"""
        for k in ("CODEMAKER_EXCEL_PIPELINE_V2",):
            monkeypatch.delenv(k, raising=False)
        cfg = Configuration()
        assert cfg.excel_pipeline_v2 is True
        assert cfg.schema_driven_decompose is False


class Test4StepConfigEnvOverride:
    """env 覆盖默认值（降级 / 调参）。"""

    def test_excel_pipeline_v2_explicit_off(self, monkeypatch):
        """=0 显式降级到旧 6 步路径。"""
        monkeypatch.setenv("CODEMAKER_EXCEL_PIPELINE_V2", "0")
        assert Configuration().excel_pipeline_v2 is False

    def test_excel_pipeline_v2_on(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_EXCEL_PIPELINE_V2", "1")
        assert Configuration().excel_pipeline_v2 is True

    def test_schema_driven_decompose_on(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_SCHEMADRIVEN_DECOMPOSE", "1")
        assert Configuration().schema_driven_decompose is True

    def test_schema_fetch_concurrency_override(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_SCHEMA_FETCH_CONCURRENCY", "4")
        assert Configuration().schema_fetch_concurrency == 4

    def test_schema_fetch_sheet_limit_override(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_SCHEMA_FETCH_SHEET_LIMIT", "20")
        assert Configuration().schema_fetch_sheet_limit == 20

    def test_splitter_decompose_threshold_override_99(self, monkeypatch):
        """调到 99 强制 DecomposeAgent 接管所有命中 fast-path 的输入。"""
        monkeypatch.setenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", "99")
        assert Configuration().splitter_decompose_threshold == 99

    def test_execute_no_llm_on(self, monkeypatch):
        """§3 开启 ExecuteAgent 去 LLM 模式。"""
        monkeypatch.setenv("CODEMAKER_EXECUTE_NO_LLM", "1")
        assert Configuration().execute_no_llm is True


class TestAgentThresholdEnvRead:
    """agent.py 内 _decompose_threshold 读取逻辑等价性（§1.2）。

    agent.py:3904 上下文：
        _decompose_threshold = int(os.getenv(
            "CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", "2"))
        if cross_action and len(cross_intents_nl) < _decompose_threshold:
    验证 env 名 / 默认值 / 解析逻辑与 Configuration 一致。
    """

    def _read_threshold_like_agent(self) -> int:
        # 与 agent.py 内联读取等价的逻辑
        return int(os.getenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", "2"))

    def test_threshold_default_2(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", raising=False)
        assert self._read_threshold_like_agent() == 2

    def test_threshold_env_99(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", "99")
        assert self._read_threshold_like_agent() == 99

    def test_threshold_consistent_with_configuration(self, monkeypatch):
        """agent 内联读与 Configuration 字段读返回一致。"""
        for v in ("2", "3", "99"):
            monkeypatch.setenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", v)
            assert self._read_threshold_like_agent() == int(v)
            assert Configuration().splitter_decompose_threshold == int(v)

    def test_threshold_invalid_fallback_raises(self, monkeypatch):
        """非数字 env 抛 ValueError（防静默用 0 导致 < 任何长度全触发）。"""
        monkeypatch.setenv("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", "abc")
        try:
            self._read_threshold_like_agent()
        except ValueError:
            return
        raise AssertionError("非数字 env 应抛 ValueError 而非静默退化")

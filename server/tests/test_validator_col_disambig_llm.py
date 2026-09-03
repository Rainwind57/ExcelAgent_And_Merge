"""validator_agent._resolve_col_with_llm 单测（LLM 列名消歧）。

opt-in：CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG=1 开启。
§Column Resolution Agent 升级后返回 (column, confidence, reason) 三元组，
底层委托 column_resolution_agent.resolve_columns，LLM 响应格式为
{"mappings":[...], "ambiguous":[...]}（非旧版 {"column":...}）。
覆盖：关/开 + 命中真实列 / 幻觉列拒绝 / 空响应 / 无表头 / 无 session / 大小写与
低置信度降级为 ambiguous。

运行: python -m pytest server/tests/test_validator_col_disambig_llm.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent


def _make_validator(raw_resp="", sid="sid-1"):
    """轻量 ValidatorAgent：mock _ensure_own_session + _call_llm_raw。"""
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v._sid = None
    v.parser = object()  # truthy guard
    v._ask_callback = None
    v._required_fields = None
    v._pk_cols_cache = None
    calls = []

    def _session():
        calls.append("session")
        return sid
    v._ensure_own_session = _session

    def _llm(prompt, timeout=90):
        calls.append("llm")
        return raw_resp
    v._call_llm_raw = _llm
    v._llm_calls = calls
    return v


_HEADERS = ["进化id", "灵兽名称", "进化后的灵兽名称", "进化消耗道具"]
_TYPE = ["int", "string", "string", "int"]


class TestResolveColWithLLM:
    def test_opt_in_off_returns_empty(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", raising=False)
        v = _make_validator('{"mappings":[{"phrase":"None","column":"灵兽名称","confidence":0.9}]}')
        out = v._resolve_col_with_llm("None", "九尾天狐·终焉", _HEADERS, _TYPE)
        assert out == ("", 0.0, "")
        assert v._llm_calls == []

    def test_hits_real_column(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", "1")
        v = _make_validator(
            '{"mappings":[{"phrase":"None","column":"进化后的灵兽名称",'
            '"confidence":0.9,"reason":"名称字段"}]}')
        out = v._resolve_col_with_llm("None", "九尾天狐·终焉", _HEADERS, _TYPE)
        assert out == ("进化后的灵兽名称", 0.9, "名称字段")
        assert v._llm_calls == ["session", "llm"]

    def test_hallucinated_column_rejected(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", "1")
        v = _make_validator(
            '{"mappings":[{"phrase":"None","column":"不存在的列","confidence":0.9}]}')
        out = v._resolve_col_with_llm("None", "九尾天狐·终焉", _HEADERS, _TYPE)
        assert out == ("", 0.0, "")

    def test_empty_json_rejected(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", "1")
        v = _make_validator('{"mappings":[{"phrase":"None","column":"","confidence":0.9}]}')
        out = v._resolve_col_with_llm("None", "九尾天狐·终焉", _HEADERS, _TYPE)
        assert out == ("", 0.0, "")

    def test_no_session_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", "1")
        v = _make_validator(
            '{"mappings":[{"phrase":"None","column":"灵兽名称","confidence":0.9}]}',
            sid="")
        out = v._resolve_col_with_llm("None", "九尾天狐·终焉", _HEADERS, _TYPE)
        assert out == ("", 0.0, "")
        assert v._llm_calls == ["session"]  # 无 sid 不调 LLM

    def test_no_headers_returns_empty(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", "1")
        v = _make_validator('{"mappings":[{"phrase":"None","column":"灵兽名称","confidence":0.9}]}')
        out = v._resolve_col_with_llm("None", "x", [], [])
        assert out == ("", 0.0, "")

    def test_case_insensitive_match(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", "1")
        v = _make_validator(
            '{"mappings":[{"phrase":"None","column":" 进化后的灵兽名称 ",'
            '"confidence":0.9}]}')
        out = v._resolve_col_with_llm("None", "九尾天狐·终焉", _HEADERS, _TYPE)
        assert out[0] == "进化后的灵兽名称"

    def test_low_confidence_downgrades_to_ambiguous(self, monkeypatch):
        """§Column Resolution Agent：置信度低于 MIN_CONFIDENCE 即使 LLM 给出
        唯一候选，也不能自动采信为高置信 mapping——confidence 应压到 0，
        交调用方走 ask 分支而非自动改名。"""
        monkeypatch.setenv("CODEMAKER_VALIDATOR_LLM_COL_DISAMBIG", "1")
        v = _make_validator(
            '{"mappings":[{"phrase":"None","column":"灵兽名称","confidence":0.3}]}')
        out = v._resolve_col_with_llm("None", "九尾天狐·终焉", _HEADERS, _TYPE)
        assert out[0] == "灵兽名称"
        assert out[1] == 0.0


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

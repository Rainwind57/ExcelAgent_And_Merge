"""A4 全链路 LLM 熔断单测:连续失败达阈值熔断,降级跳过;reset_circuit 恢复。"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from agent.excel.core.step_ai_enhancer import StepAIEnhancer


class FakeClient:
    """fake CodemakerClient:create_session ok,prompt 前 N 次失败后成功。"""
    def __init__(self, fail_times):
        self.fail_times = fail_times
        self.prompt_calls = 0

    cfg = types.SimpleNamespace(default_model="codemaker")

    def create_session(self, **kw):
        return types.SimpleNamespace(ok=True, session_id="s1", error="")

    def prompt(self, sid, prompt, timeout=60, model=None, cancel_event=None):
        self.prompt_calls += 1
        if self.prompt_calls <= self.fail_times:
            return types.SimpleNamespace(ok=False, error="timeout", response_text="")
        return types.SimpleNamespace(ok=True, error="", response_text="ok")

    def extract_json_from_response(self, raw):
        return None


def test_circuit_opens_after_threshold_failures(monkeypatch):
    """连续失败达 _circuit_threshold → 熔断,后续调用降级跳过(不调 client.prompt)。"""
    monkeypatch.setenv("CODEMAKER_AI_CIRCUIT_THRESHOLD", "3")
    client = FakeClient(fail_times=3)
    enh = StepAIEnhancer(client)
    assert enh._circuit_threshold == 3

    # 3 次失败
    for _ in range(3):
        assert enh._call_llm("p", timeout=5) is None
    assert client.prompt_calls == 3

    # 第 4 次:熔断已开 → 返回 None 且不再调 client.prompt
    assert enh._call_llm("p", timeout=5) is None
    assert client.prompt_calls == 3, "熔断后应降级跳过,不再调 prompt"


def test_circuit_reset_restores_calls(monkeypatch):
    """reset_circuit 重置失败计数 → 后续调用恢复(熔断关闭)。"""
    monkeypatch.setenv("CODEMAKER_AI_CIRCUIT_THRESHOLD", "2")
    client = FakeClient(fail_times=2)
    enh = StepAIEnhancer(client)
    for _ in range(2):
        enh._call_llm("p", timeout=5)
    # 熔断已开
    assert enh._call_llm("p", timeout=5) is None
    assert client.prompt_calls == 2
    # reset → 恢复,第 3 次 prompt 调用成功
    enh.reset_circuit()
    r = enh._call_llm("p", timeout=5)
    assert r == "ok"
    assert client.prompt_calls == 3


def test_circuit_success_resets_fail_count(monkeypatch):
    """成功调用重置 _fail_count(熔断未开时)。"""
    monkeypatch.setenv("CODEMAKER_AI_CIRCUIT_THRESHOLD", "3")
    client = FakeClient(fail_times=0)  # 全成功
    enh = StepAIEnhancer(client)
    assert enh._call_llm("p", timeout=5) == "ok"
    assert enh._fail_count == 0

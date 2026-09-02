"""_decompose_single_prompt 对"合法但空数组 []"的重采样重试确定性单测。

原逻辑只在 LLM 返回空字符串时重试；对"LLM 确信地返回 []"没有二次确认，直接
接受为最终结果。这是分段并发场景里"某段整体产空"的根因之一（同输入重跑，
产空的段位置会变——单次采样的偶然空判定被当成定局）。改为：raw 非空但解析出
的 JSON 数组为空，也在同一重试预算内重采样一次。
"""
import os
import sys
import json as _json
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.locator_agent import CandidateTable


class _MockResp:
    def __init__(self, text):
        self.response_text = text
        self.ok = True
        self.error = ""


class _EmptyThenFullClient:
    """第 1 次调用返回合法空数组 []，第 2 次调用返回真正的意图数组。"""

    def __init__(self, full_response):
        self._full = full_response
        self.calls = 0

    def create_session(self, **kw):
        @dataclass
        class S:
            ok: bool = True
            session_id: str = "mock-sid"
        return S()

    def prompt(self, sid, prompt, timeout=90, model="", cancel_event=None):
        self.calls += 1
        if self.calls == 1:
            return _MockResp("[]")
        return _MockResp(self._full)

    def extract_json_from_response(self, text):
        return None


class _AlwaysEmptyClient(_EmptyThenFullClient):
    """每次调用都返回合法空数组 []（模拟持续确信为空，重试用尽后应最终判空）。"""

    def prompt(self, sid, prompt, timeout=90, model="", cancel_event=None):
        self.calls += 1
        return _MockResp("[]")


class _MockParser:
    def __init__(self, client):
        self.client = client
        self.directory = ""
        self.model = ""


class _PetCli:
    class _P:
        def __init__(self, stem):
            self.stem = stem

    def __init__(self):
        self._paths = [self._P("pet")]

    def list_tables(self):
        return self._paths

    def get_sheets(self, path):
        return ["Pet"]

    def read_header(self, path, sheet):
        return ["宠物id", "名称"]

    def read_type_row(self, path, sheet):
        return ["宠物id:int", "名称:string"]


def test_empty_array_first_attempt_retries_and_succeeds(monkeypatch):
    monkeypatch.setenv("CODEMAKER_DECOMPOSE_SINGLE_RETRY", "1")
    full_resp = ('```json\n[{"table":"pet","sheet":"Pet","action":"add",'
                 '"fields":{"名称":"子鼠"}}]\n```')
    client = _EmptyThenFullClient(full_resp)
    da = DecomposeAgent(parser=_MockParser(client), cli=_PetCli())
    candidates = [CandidateTable("pet", "Pet", 1.0)]
    out, dropped = da._decompose_single_prompt("新增灵兽子鼠", candidates, "", 10)
    assert client.calls == 2, "第 1 次空数组应触发重采样，第 2 次才成功"
    assert len(out) == 1
    assert out[0].table_hint == "pet"


def test_empty_array_exhausts_retry_budget_returns_empty(monkeypatch):
    monkeypatch.setenv("CODEMAKER_DECOMPOSE_SINGLE_RETRY", "1")
    client = _AlwaysEmptyClient("[]")
    da = DecomposeAgent(parser=_MockParser(client), cli=_PetCli())
    candidates = [CandidateTable("pet", "Pet", 1.0)]
    out, dropped = da._decompose_single_prompt("新增灵兽子鼠", candidates, "", 10)
    # 重试预算用尽后仍为空 → 最终判定为空（不会无限重试）
    assert client.calls == 2  # 首次 + 1 次重试，预算耗尽后停止
    assert out == []


def test_empty_array_retry_can_be_disabled_via_zero_budget(monkeypatch):
    monkeypatch.setenv("CODEMAKER_DECOMPOSE_SINGLE_RETRY", "0")
    full_resp = ('```json\n[{"table":"pet","sheet":"Pet","action":"add",'
                 '"fields":{"名称":"子鼠"}}]\n```')
    client = _EmptyThenFullClient(full_resp)
    da = DecomposeAgent(parser=_MockParser(client), cli=_PetCli())
    candidates = [CandidateTable("pet", "Pet", 1.0)]
    out, dropped = da._decompose_single_prompt("新增灵兽子鼠", candidates, "", 10)
    # 重试预算为 0 → 只调 1 次，第 1 次空数组即为最终结果（不重采样）
    assert client.calls == 1
    assert out == []

# -*- coding: utf-8 -*-
"""O22 §9.1 ReplanAgent 单测（replan-on-failure）。

覆盖：
- replan 正常路径：failures + remaining → LLM 产修订 JSON → NLIntent[]
- replan LLM 空响应 → 降级返空 list
- replan LLM 非 JSON → 降级返空 list
- replan 无 failures / 无 remaining → 返空（无重规划输入）
- replan 无 parser / 无 client → 返空
- replan JSON 解析为 NLIntent 形状（action/table/sheet/fields/produces/locator_*）
- 门控 replan_enabled()=CODEMAKER_REPLAN_ON_FAILURE=0 默认关
- replan_max_rounds()=2 上限
- _to_nl_intents 字段映射正确性
- _parse_json_array fenced ```json``` 与裸数组兼容
"""
import os
import sys
import threading
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.excel.subagent.replan_agent import (
    ReplanAgent, replan_enabled, replan_max_rounds)
from agent.excel.parser.nl_parser import NLIntent


# ── Mock LLM ──────────────────────────────────────────────
class MockLLMResponse:
    def __init__(self, text):
        self.response_text = text
        self.ok = True
        self.error = ""
        self.error_type = ""


class MockClient:
    def __init__(self, response_text=""):
        self._response = response_text
        self._cancel_event = None
        self.calls = 0

    def create_session(self, **kw):
        @dataclass
        class _SR:
            ok: bool = True
            session_id: str = "mock-sess"
        return _SR()

    def prompt(self, sid, prompt, timeout=45, model="", cancel_event=None):
        self.calls += 1
        return MockLLMResponse(self._response)

    def health_check(self):
        return True


class MockParser:
    def __init__(self, response_text=""):
        self.client = MockClient(response_text)
        self.model = "mock-model"
        self.directory = ""
        self._session_id = ""
        self._cancel_event = None


class TestReplanAgentMainPath:
    """ReplanAgent 正常路径：failures + remaining → 修订 NLIntent[]。"""

    def test_replan_produces_nl_intents(self):
        """LLM 产修订 JSON → NLIntent[] 形状正确。"""
        llm_json = '''```json
[{"action":"add","table":"reward","sheet":"Reward","fields":{"奖励id":10090,"名称":"宝箱"},
  "produces":"new_reward_id","locator_field":"","locator_value":""}]
```'''
        parser = MockParser(llm_json)
        agent = ReplanAgent(parser=parser)
        failures = [{"table": "quest", "sheet": "QuestGroup", "col": "",
                      "root_cause": "reward_id 引用悬空，目标行不存在"}]
        remaining = [NLIntent(action="add", table_hint="reward",
                              sheet_hint="Reward", raw="新增奖励宝箱")]
        produced = {"new_quest_id": "10050"}
        out = agent.replan(failures, remaining, produced, "新增任务奖励", cli=None)
        assert len(out) == 1
        assert out[0].action == "add"
        assert out[0].table_hint == "reward"
        assert out[0].sheet_hint == "Reward"
        assert out[0].extras["fields"]["奖励id"] == 10090
        assert out[0].extras["produces"] == "new_reward_id"
        assert parser.client.calls == 1

    def test_replan_empty_failures_returns_empty(self):
        """无 failures → 返空（无重规划输入）。"""
        parser = MockParser("[]")
        agent = ReplanAgent(parser=parser)
        out = agent.replan([], [NLIntent(action="add")], {}, "text", cli=None)
        assert out == []
        assert parser.client.calls == 0  # 不调 LLM

    def test_re_empty(self):
        """无 remaining → 返空。"""
        parser = MockParser("[]")
        agent = ReplanAgent(parser=parser)
        out = agent.replan([{"root_cause": "fail"}], [], {}, "text", cli=None)
        assert out == []
        assert parser.client.calls == 0

    def test_replan_no_parser_returns_empty(self):
        """无 parser → 返空。"""
        agent = ReplanAgent(parser=None)
        out = agent.replan([{"root_cause": "fail"}], [NLIntent()], {}, "text")
        assert out == []

    def test_replan_no_client_returns_empty(self):
        """parser 无 client → 返空。"""
        parser = MockParser()
        parser.client = None
        agent = ReplanAgent(parser=parser)
        out = agent.replan([{"root_cause": "fail"}], [NLIntent()], {}, "text")
        assert out == []

    def test_replan_llm_empty_response_returns_empty(self):
        """LLM 空响应 → 降级返空。"""
        parser = MockParser("")
        agent = ReplanAgent(parser=parser)
        out = agent.replan([{"root_cause": "fail"}], [NLIntent()], {}, "text")
        assert out == []

    def test_replan_llm_non_json_returns_empty(self):
        """LLM 非 JSON → 降级返空。"""
        parser = MockParser("这不是 JSON")
        agent = ReplanAgent(parser=parser)
        out = agent.replan([{"root_cause": "fail"}], [NLIntent()], {}, "text")
        assert out == []

    def test_replan_llm_empty_array_returns_empty(self):
        """LLM 返空数组 [] → 返空（无修订 op，正常降级）。"""
        parser = MockParser("```json\n[]\n```")
        agent = ReplanAgent(parser=parser)
        out = agent.replan([{"root_cause": "fail"}], [NLIntent()], {}, "text")
        assert out == []

    def test_replan_llm_exception_handled(self):
        """LLM 调用异常 → 捕获返空不崩。"""
        class _ErrClient(MockClient):
            def prompt(self, *a, **kw):
                raise RuntimeError("LLM 崩")
        parser = MockParser()
        parser.client = _ErrClient()
        agent = ReplanAgent(parser=parser)
        out = agent.replan([{"root_cause": "fail"}], [NLIntent()], {}, "text")
        assert out == []


class TestReplanAgentParsing:
    """ReplanAgent JSON 解析 + 字段映射。"""

    def test_parse_json_array_fenced(self):
        """fenced ```json [...] ``` 解析。"""
        agent = ReplanAgent(parser=MockParser())
        raw = '```json\n[{"a":1}]\n```'
        arr = agent._parse_json_array(raw)
        assert arr == [{"a": 1}]

    def test_parse_json_array_bare(self):
        """裸数组 [...] 解析。"""
        agent = ReplanAgent(parser=MockParser())
        raw = '[{"a":1},{"b":2}]'
        arr = agent._parse_json_array(raw)
        assert len(arr) == 2

    def test_parse_json_array_invalid(self):
        """非 JSON → 返空。"""
        agent = ReplanAgent(parser=MockParser())
        assert agent._parse_json_array("not json") == []
        assert agent._parse_json_array("") == []

    def test_to_nl_intents_field_mapping(self):
        """_to_nl_intents JSON → NLIntent 字段映射。"""
        agent = ReplanAgent(parser=MockParser())
        arr = [{"action": "set", "table": "item", "sheet": "ItemBase",
                "fields": {"名称": "测试"}, "produces": "new_item_id",
                "locator_field": "名称", "locator_value": "测试"}]
        out = agent._to_nl_intents(arr, "text", [NLIntent()])
        assert len(out) == 1
        assert out[0].action == "set"
        assert out[0].table_hint == "item"
        assert out[0].sheet_hint == "ItemBase"
        assert out[0].locator_field == "名称"
        assert out[0].locator_value == "测试"
        assert out[0].extras["fields"]["名称"] == "测试"
        assert out[0].extras["produces"] == "new_item_id"
        assert out[0].extras["source"] == "replan"

    def test_to_nl_intents_invalid_action_defaults_add(self):
        """非法 action 默认 add。"""
        agent = ReplanAgent(parser=MockParser())
        arr = [{"action": "invalid", "table": "x"}]
        out = agent._to_nl_intents(arr, "text", [NLIntent()])
        assert out[0].action == "add"

    def test_to_nl_intents_skips_non_dict(self):
        """非 dict 元素跳过。"""
        agent = ReplanAgent(parser=MockParser())
        arr = [{"action": "add"}, "not dict", 42, None]
        out = agent._to_nl_intents(arr, "text", [NLIntent()])
        assert len(out) == 1

    def test_to_nl_intents_fields_not_dict_empties(self):
        """fields 非 dict → 空 dict。"""
        agent = ReplanAgent(parser=MockParser())
        arr = [{"action": "add", "fields": "not dict"}]
        out = agent._to_nl_intents(arr, "text", [NLIntent()])
        assert out[0].extras["fields"] == {}


class TestReplanGatingAndLimits:
    """门控 + 上限。"""

    def test_replan_enabled_default_on(self):
        """CODEMAKER_REPLAN_ON_FAILURE 默认开（准确率优先、不少指令）。"""
        # 清 env 确保默认
        old = os.environ.pop("CODEMAKER_REPLAN_ON_FAILURE", None)
        try:
            assert replan_enabled() is True
        finally:
            if old is not None:
                os.environ["CODEMAKER_REPLAN_ON_FAILURE"] = old

    def test_replan_disabled_when_env_zero(self):
        """env=0 关闭（用户显式关 replan）。"""
        old = os.environ.get("CODEMAKER_REPLAN_ON_FAILURE")
        os.environ["CODEMAKER_REPLAN_ON_FAILURE"] = "0"
        try:
            assert replan_enabled() is False
        finally:
            if old is None:
                os.environ.pop("CODEMAKER_REPLAN_ON_FAILURE", None)
            else:
                os.environ["CODEMAKER_REPLAN_ON_FAILURE"] = old

    def test_replan_enabled_on(self):
        """env=1 开启。"""
        old = os.environ.get("CODEMAKER_REPLAN_ON_FAILURE")
        os.environ["CODEMAKER_REPLAN_ON_FAILURE"] = "1"
        try:
            assert replan_enabled() is True
        finally:
            if old is None:
                os.environ.pop("CODEMAKER_REPLAN_ON_FAILURE", None)
            else:
                os.environ["CODEMAKER_REPLAN_ON_FAILURE"] = old

    def test_replan_max_rounds_is_two(self):
        """上限 2（防 LLM 死循环）。"""
        assert replan_max_rounds() == 2


class TestReplanAgentSourceField:
    """replan 产 NLIntent source 标记（区分 replan op 与原 op）。"""

    def test_replan_intent_has_source_replan(self):
        """replan 产 NLIntent extras.source='replan'。"""
        llm_json = '```json\n[{"action":"add","table":"reward","fields":{"id":1}}]\n```'
        parser = MockParser(llm_json)
        agent = ReplanAgent(parser=parser)
        out = agent.replan([{"root_cause": "fail"}], [NLIntent()], {}, "text")
        assert out[0].extras["source"] == "replan"

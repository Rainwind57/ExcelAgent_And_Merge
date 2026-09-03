"""Step1 strict parsing hardening regressions."""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.pipeline import Step1ParseSubAgent, StepContext
from agent.excel.parser.nl_parser import NLIntent
from agent.excel.subagent.decompose_agent import DecomposeAgent


class _FakeParseAgent:
    def __init__(self, intents, segments):
        self._intents = intents
        self._last_segments = segments
        self._last_locator_results = []
        self._last_locator_result = None

    def parse(self, _text):
        return self._intents


def _agent_with_sink():
    da = DecomposeAgent(parser=object())
    captured = []
    da._thinking_sink = lambda phase, detail: captured.append((phase, detail))
    return da, captured


def test_to_split_intents_coerces_fields_list_to_object():
    da, captured = _agent_with_sink()
    arr = [{
        "table": "quest", "sheet": "Quest", "action": "add",
        "fields": [
            {"field": "quest_id", "value": 250600},
            {"key": "name", "value": "封印魔龙"},
        ],
    }]

    intents, _ = da._to_split_intents(arr, "新增任务")

    assert len(intents) == 1
    assert intents[0].fields == {"quest_id": 250600, "name": "封印魔龙"}
    assert any("fields 数组已归一" in detail for _phase, detail in captured)


def test_to_split_intents_preserves_invalid_action_for_step2():
    da, captured = _agent_with_sink()
    arr = [{
        "table": "quest", "sheet": "Quest", "action": "create_row",
        "fields": {"quest_id": 250600},
    }]

    intents, _ = da._to_split_intents(arr, "创建任务")

    assert len(intents) == 1
    assert intents[0].action == "create_row"
    assert any("未知 action" in detail for _phase, detail in captured)


def test_to_split_intents_accepts_consumes_list_shape():
    da, _captured = _agent_with_sink()
    arr = [{
        "table": "combat", "sheet": "Combat", "action": "add",
        "fields": {"quest_id": "待引用", "name": "魔龙战斗"},
        "consumes": [{"field": "quest_id", "label": "new_quest_id"}],
    }]

    intents, _ = da._to_split_intents(arr, "新增战斗引用任务")

    assert len(intents) == 1
    assert intents[0].fields["quest_id"] == "<new_quest_id>"


def test_step1_short_id_field_evidence_covers_segment(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    intent = NLIntent(
        action="add", table_hint="tips", sheet_hint="tips",
        raw="批量新增 tips",
        extras={"fields": {"id": 12, "type": "ui"}},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent(
        [intent],
        ["帮我配一下提示文案", "第二条 id 12 类型 ui", "第三条 id 34 类型 ui"],
    )

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.ok is True
    assert not any(e.error_type == "segment_no_intent" for e in result.errors)


def test_step1_structured_uncovered_segment_not_suppressed(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    intent = NLIntent(
        action="add", table_hint="tips", sheet_hint="tips",
        raw="第一条 key=T1 名称=提示一",
        extras={"fields": {"key": "T1", "名称": "提示一"}},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent(
        [intent],
        ["第一条 key=T1 名称=提示一", "第二条 key=T2 名称=提示二"],
    )

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.metrics["suppressed_segment_no_intent"] == 0
    assert any(e.error_type == "segment_no_intent" for e in result.errors)

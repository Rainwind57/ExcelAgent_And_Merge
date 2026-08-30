from __future__ import annotations

from server.agent.excel.core.pipeline import (
    STEP1_PARSE,
    STEP2_VALIDATE,
    Step1ParseSubAgent,
    Step2ValidateSubAgent,
    Step3ExecuteSubAgent,
    Step4ConcludeSubAgent,
    StepContext,
    StepError,
    StepResult,
)
from server.agent.excel.parser.nl_parser import NLIntent


class _FakeParseAgent:
    _last_segments = []
    _last_locator_results = []

    def __init__(self, intents, segments=None):
        self._intents = intents
        if segments is not None:
            self._last_segments = segments

    def parse(self, _text):
        return list(self._intents)


def test_step2_structural_fallback_without_services_blocks_bad_intent():
    ctx = StepContext(session_id="s", user_text="x")
    ctx.set_result(STEP1_PARSE, StepResult(
        STEP1_PARSE,
        ok=True,
        artifacts={"intents": [NLIntent(action="add", table_hint=None, extras={})]},
    ))

    result = Step2ValidateSubAgent(services=None).execute(ctx)

    assert result.ok is False
    assert any(e.error_type == "table_missing" for e in result.errors)
    assert any(e.error_type == "add_fields_missing" for e in result.errors)
    assert all(e.step_id == STEP2_VALIDATE for e in result.errors)


def test_step1_quality_report_warns_by_default(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    consumer = NLIntent(
        action="add", table_hint="child", sheet_hint="Child", raw="x",
        extras={"fields": {"parent_id": "<missing_parent_id>", "name": "C"}},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent([consumer])

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.ok is True
    assert result.metrics["step1_quality_hard"] == 1
    assert result.artifacts["step1_quality"]["issues"][0]["type"] == "unresolved_placeholder"


def test_step1_quality_gate_blocks_unresolved_placeholder(monkeypatch):
    monkeypatch.setenv("CODEMAKER_STEP1_QUALITY_GATE", "1")
    consumer = NLIntent(
        action="add", table_hint="child", sheet_hint="Child", raw="x",
        extras={"fields": {"parent_id": "<missing_parent_id>", "name": "C"}},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent([consumer])

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.ok is False
    assert any(e.error_type == "step1_quality_gate" and e.is_hard
               for e in result.errors)


def test_step1_quality_flags_foreign_placeholder_primary_key(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    parent = NLIntent(
        action="add", table_hint="parent", sheet_hint="Parent", raw="x",
        extras={"fields": {"id": "<new_parent_id>", "name": "P"},
                "produces": "new_parent_id"},
    )
    parent.produces_label = "new_parent_id"
    child = NLIntent(
        action="add", table_hint="child", sheet_hint="Child", raw="x",
        extras={"fields": {
            "id": "<new_parent_id>",
            "parent_id": "<new_parent_id>",
            "name": "C",
        }},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent([parent, child])

    result = step.execute(StepContext(session_id="s", user_text="x"))

    issue_types = {
        x["type"] for x in result.artifacts["step1_quality"]["issues"]
    }
    assert "foreign_placeholder_primary_key" in issue_types


def test_step1_quality_flags_producer_dependency_cycle(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    parent = NLIntent(
        action="add", table_hint="parent", sheet_hint="Parent", raw="x",
        extras={"fields": {"id": "<new_parent_id>", "child_id": "<new_child_id>"},
                "produces": "new_parent_id"},
    )
    parent.produces_label = "new_parent_id"
    parent.consumes_labels = ["new_child_id"]
    child = NLIntent(
        action="add", table_hint="child", sheet_hint="Child", raw="x",
        extras={"fields": {"id": "<new_child_id>", "parent_id": "<new_parent_id>"},
                "produces": "new_child_id"},
    )
    child.produces_label = "new_child_id"
    child.consumes_labels = ["new_parent_id"]
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent([parent, child])

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.ok is True
    issue_types = {
        x["type"] for x in result.artifacts["step1_quality"]["issues"]
    }
    assert "producer_dependency_cycle" in issue_types
    assert result.metrics["step1_quality_hard"] >= 1


def test_step1_exports_plan_graph_for_references(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    parent = NLIntent(
        action="add", table_hint="parent", sheet_hint="Parent", raw="x",
        extras={"fields": {"id": "<new_parent_id>", "name": "P"},
                "produces": "new_parent_id"},
    )
    parent.produces_label = "new_parent_id"
    child = NLIntent(
        action="add", table_hint="child", sheet_hint="Child", raw="x",
        extras={"fields": {"parent_id": "<new_parent_id>",
                           "missing_id": "<missing_id>",
                           "name": "C"}},
    )
    child.consumes_labels = ["new_parent_id"]
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent([parent, child])

    result = step.execute(StepContext(session_id="s", user_text="x"))

    graph = result.artifacts["plan_graph"]
    assert graph["node_count"] == 2
    assert graph["edge_count"] == 1
    assert graph["edges"][0]["from_idx"] == 2
    assert graph["edges"][0]["to_idx"] == 1
    assert graph["edges"][0]["label"] == "new_parent_id"
    assert graph["unresolved_ref_count"] == 1
    assert graph["unresolved_refs"][0]["label"] == "missing_id"
    assert result.metrics["step1_plan_nodes"] == 2
    assert result.metrics["step1_plan_edges"] == 1


def test_step1_exports_semantic_plan_entities_and_refs(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    parent = NLIntent(
        action="add", table_hint="parent", sheet_hint="Parent", raw="make parent",
        extras={"fields": {"id": "<new_parent_id>", "name": "P"},
                "produces": "new_parent_id"},
    )
    parent.produces_label = "new_parent_id"
    child = NLIntent(
        action="add", table_hint="child", sheet_hint="Child", raw="make child",
        extras={"fields": {"parent_id": "<new_parent_id>",
                           "missing_id": "<missing_id>",
                           "name": "C"}},
    )
    child.consumes_labels = ["new_parent_id"]
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent([parent, child])

    result = step.execute(StepContext(session_id="s", user_text="x"))

    semantic = result.artifacts["semantic_plan"]
    assert semantic["version"] == 1
    assert semantic["entity_count"] == 2
    assert semantic["relation_count"] == 1
    assert semantic["entities"][1]["target"] == {"table": "child", "sheet": "Child"}
    assert any(
        r["label"] == "new_parent_id" and r["status"] == "resolved"
        for r in semantic["refs"]
    )
    assert any(
        r["label"] == "missing_id" and r["status"] == "unresolved"
        for r in semantic["refs"]
    )
    assert result.metrics["semantic_entities"] == 2
    assert result.metrics["semantic_relations"] == 1
    assert result.metrics["semantic_unresolved_refs"] == 1
    assert result.metrics["semantic_compile_issues"] == 0
    assert result.artifacts["semantic_compile_report"]["ok"] is True


def test_step1_semantic_plan_does_not_treat_own_produces_as_dependency(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    intent = NLIntent(
        action="add", table_hint="mail", sheet_hint="MailTemplate", raw="x",
        extras={"fields": {"template_id": "<new_template_id>",
                           "title": "open notice"},
                "produces": "new_template_id"},
    )
    intent.produces_label = "new_template_id"
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent([intent])

    result = step.execute(StepContext(session_id="s", user_text="x"))

    semantic = result.artifacts["semantic_plan"]
    assert semantic["relation_count"] == 0
    assert semantic["refs"] == []
    assert semantic["entities"][0]["attributes"][0]["produces_ref"] == "new_template_id"


def test_step2_blocks_step1_hard_quality_issue_even_when_step1_warned():
    intent = NLIntent(
        action="add", table_hint="child", sheet_hint="Child", raw="x",
        extras={"fields": {"parent_id": "<missing_parent_id>", "name": "C"}},
    )
    ctx = StepContext(session_id="s", user_text="x")
    ctx.set_result(STEP1_PARSE, StepResult(
        STEP1_PARSE,
        ok=True,
        artifacts={
            "intents": [intent],
            "step1_quality": {
                "hard_count": 1,
                "issues": [{
                    "type": "unresolved_placeholder",
                    "idx": 1,
                    "table": "child",
                    "sheet": "Child",
                    "label": "missing_parent_id",
                    "severity": "hard",
                }],
            },
        },
    ))

    result = Step2ValidateSubAgent(services=None).execute(ctx)

    assert result.ok is False
    assert any(e.error_type == "step1_unresolved_placeholder" and e.is_hard
               for e in result.errors)
    assert result.metrics["step1_quality_hard"] == 1


def test_step2_passes_semantic_plan_artifact_through():
    intent = NLIntent(
        action="add", table_hint="tips", sheet_hint="tips",
        extras={"fields": {"value": "x"}},
    )
    semantic_plan = {"version": 1, "entities": [{"entity_id": 1}]}
    ctx = StepContext(session_id="s", user_text="x")
    ctx.set_result(STEP1_PARSE, StepResult(
        STEP1_PARSE,
        ok=True,
        artifacts={
            "intents": [intent],
            "step1_quality": {"hard_count": 0, "issues": []},
            "semantic_plan": semantic_plan,
        },
    ))

    result = Step2ValidateSubAgent(services=None).execute(ctx)

    assert result.ok is True
    assert result.artifacts["semantic_plan"] is semantic_plan


def test_step2_blocks_semantic_compile_hard_issue():
    intent = NLIntent(
        action="add", table_hint="x", sheet_hint="X",
        extras={"fields": {"name": "A"}},
    )
    ctx = StepContext(session_id="s", user_text="x")
    ctx.set_result(STEP1_PARSE, StepResult(
        STEP1_PARSE,
        ok=True,
        artifacts={
            "intents": [intent],
            "step1_quality": {"hard_count": 0, "issues": []},
            "semantic_compile_report": {
                "hard_count": 1,
                "issues": [{
                    "type": "target_table_missing",
                    "idx": 1,
                    "severity": "hard",
                }],
            },
        },
    ))

    result = Step2ValidateSubAgent(services=None).execute(ctx)

    assert result.ok is False
    assert any(e.error_type == "semantic_target_table_missing" and e.is_hard
               for e in result.errors)


def test_step1_suppresses_low_signal_uncovered_intro_segment(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    intent = NLIntent(
        action="add", table_hint="tips", sheet_hint="tips",
        raw="第一条：'当前网络波动，请稍后重试'，key 用 TID_TIPS_NETWORK_RETRY，类型 tips",
        extras={"fields": {
            "value": "当前网络波动，请稍后重试",
            "key": "TID_TIPS_NETWORK_RETRY",
            "type": "tips",
        }},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent(
        [intent],
        segments=[
            "帮我把几条游戏内提示文案配一下。",
            "第一条：'当前网络波动，请稍后重试'，key 用 TID_TIPS_NETWORK_RETRY，类型 tips",
        ],
    )

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.ok is True
    assert result.metrics["suppressed_segment_no_intent"] == 1
    assert not any(e.error_type == "segment_no_intent" for e in result.errors)


def test_step1_segment_coverage_uses_extracted_field_evidence(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    intent = NLIntent(
        action="add", table_hint="tips", sheet_hint="tips",
        raw="批量新增 tips",
        extras={"fields": {
            "value": "灵兽升级已达到当前等级上限",
            "key": "TID_TIPS_PET_EXP_LIMIT_TIPS_2",
            "type": "tips",
        }},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent(
        [intent],
        segments=[
            "帮我把几条游戏内提示文案配一下。",
            "第二条：'灵兽升级已达到当前等级上限'，key 用 TID_TIPS_PET_EXP_LIMIT_TIPS_2，类型 tips",
        ],
    )

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.ok is True
    assert result.metrics["suppressed_segment_no_intent"] == 1
    assert not any(e.error_type == "segment_no_intent" for e in result.errors)


def test_step1_keeps_uncovered_action_segment_as_warning(monkeypatch):
    monkeypatch.delenv("CODEMAKER_STEP1_QUALITY_GATE", raising=False)
    intent = NLIntent(
        action="add", table_hint="tips", sheet_hint="tips",
        raw="第一条：'当前网络波动，请稍后重试'，key 用 TID_TIPS_NETWORK_RETRY，类型 tips",
        extras={"fields": {
            "value": "当前网络波动，请稍后重试",
            "key": "TID_TIPS_NETWORK_RETRY",
            "type": "tips",
        }},
    )
    step = Step1ParseSubAgent()
    step._parse_agent = _FakeParseAgent(
        [intent],
        segments=[
            "新增一个活动叫'九霄论剑'",
            "第一条：'当前网络波动，请稍后重试'，key 用 TID_TIPS_NETWORK_RETRY，类型 tips",
        ],
    )

    result = step.execute(StepContext(session_id="s", user_text="x"))

    assert result.ok is True
    assert result.metrics["suppressed_segment_no_intent"] == 0
    assert any(e.error_type == "segment_no_intent" for e in result.errors)


def test_step3_missing_services_is_hard_not_silent_success():
    ctx = StepContext(session_id="s", user_text="x")
    ctx.set_result(STEP2_VALIDATE, StepResult(
        STEP2_VALIDATE,
        ok=True,
        artifacts={"validated": [NLIntent(action="get", table_hint="pet")]},
    ))

    result = Step3ExecuteSubAgent(services=None).execute(ctx)

    assert result.ok is False
    assert any(e.is_hard and e.error_type == "execute_service_missing"
               for e in result.errors)
    assert result.artifacts["failures"][0]["type"] == "execute_service_missing"


class _NoneRunServices:
    def peek_llm_total(self):
        return 0

    def run_single(self, *args, **kwargs):
        return None


def test_step3_empty_dispatch_result_does_not_report_ok():
    ctx = StepContext(session_id="s", user_text="x")
    ctx.set_result(STEP2_VALIDATE, StepResult(
        STEP2_VALIDATE,
        ok=True,
        artifacts={"validated": [NLIntent(action="get", table_hint="pet")]},
    ))

    result = Step3ExecuteSubAgent(services=_NoneRunServices()).execute(ctx)

    assert result.ok is False
    assert any(e.error_type == "execute_empty_result" for e in result.errors)
    assert any(e.error_type == "all_subtasks_failed" and e.is_hard
               for e in result.errors)


def test_step4_summarizes_step1_or_step2_failures_when_step3_never_ran():
    ctx = StepContext(session_id="s", user_text="x")
    ctx.set_result(STEP1_PARSE, StepResult(
        STEP1_PARSE,
        ok=False,
        errors=[StepError(
            step_id=STEP1_PARSE,
            error_type="parse_empty",
            message="no intent",
            root_cause="no candidate",
            is_hard=True,
        )],
    ))

    result = Step4ConcludeSubAgent().execute(ctx)

    assert result.ok is False
    assert "no candidate" in result.artifacts["summary"]

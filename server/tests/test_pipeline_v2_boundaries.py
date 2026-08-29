from __future__ import annotations

from server.agent.excel.core.pipeline import (
    STEP1_PARSE,
    STEP2_VALIDATE,
    Step2ValidateSubAgent,
    Step3ExecuteSubAgent,
    Step4ConcludeSubAgent,
    StepContext,
    StepError,
    StepResult,
)
from server.agent.excel.parser.nl_parser import NLIntent


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

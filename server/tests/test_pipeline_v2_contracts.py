"""4-Step V2 流水线契约与 orchestrator 骨架单测（§设计 S1）。

验证不变量：
  - 每步恰好一次 stage_start/stage_end
  - hard error 硬停 + 仍走 Step4 汇总
  - 错误归属固定到抛错步 step_id
  - StepContext 步间只追加
"""
from __future__ import annotations

from agent.excel.core.pipeline import (
    SSE, STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE,
    STEP_ORDER, StepContext, StepError, StepHardError, StepResult,
    ExcelAgentPipeline, Step1ParseSubAgent,
)


def _collect(gen):
    return list(gen)


class _FakeStep:
    def __init__(self, step_id, result, raise_hard=None):
        self.id = step_id
        self._result = result
        self._raise_hard = raise_hard

    def execute(self, ctx):
        if self._raise_hard:
            raise self._raise_hard
        return self._result


def test_four_steps_each_stage_start_end_once():
    ctx = StepContext(session_id="s", user_text="hi")
    steps = {
        STEP1_PARSE: _FakeStep(STEP1_PARSE, StepResult(STEP1_PARSE, ok=True)),
        STEP2_VALIDATE: _FakeStep(STEP2_VALIDATE, StepResult(STEP2_VALIDATE, ok=True)),
        STEP3_EXECUTE: _FakeStep(STEP3_EXECUTE, StepResult(STEP3_EXECUTE, ok=True)),
        STEP4_CONCLUDE: _FakeStep(STEP4_CONCLUDE, StepResult(STEP4_CONCLUDE, ok=True)),
    }
    pipe = ExcelAgentPipeline(
        step1=steps[STEP1_PARSE], step2=steps[STEP2_VALIDATE],
        step3=steps[STEP3_EXECUTE], step4=steps[STEP4_CONCLUDE])
    evs = _collect(pipe.run(ctx))
    starts = [e for e in evs if e["type"] == "stage_start"]
    ends = [e for e in evs if e["type"] == "stage_end"]
    dones = [e for e in evs if e["type"] == "done"]
    assert len(starts) == 4
    assert len(ends) == 4
    assert len(dones) == 1
    assert [s["step_id"] for s in starts] == STEP_ORDER
    assert ctx.all_ok() is True


def test_hard_error_stops_and_still_runs_step4():
    ctx = StepContext(session_id="s", user_text="hi")
    hard = StepHardError(STEP2_VALIDATE, "validate_fail", "校验硬失败",
                         root_cause="字段不存在")
    s1 = _FakeStep(STEP1_PARSE, StepResult(STEP1_PARSE, ok=True))
    s2 = _FakeStep(STEP2_VALIDATE, None, raise_hard=hard)
    s4 = _FakeStep(STEP4_CONCLUDE, StepResult(STEP4_CONCLUDE, ok=True))
    pipe = ExcelAgentPipeline(step1=s1, step2=s2, step4=s4)
    evs = _collect(pipe.run(ctx))
    ends = [e for e in evs if e["type"] == "stage_end"]
    # Step1, Step2, Step4（Step3 不跑）
    assert [e["step_id"] for e in ends] == [STEP1_PARSE, STEP2_VALIDATE, STEP4_CONCLUDE]
    s2_end = [e for e in ends if e["step_id"] == STEP2_VALIDATE][0]
    assert s2_end["ok"] is False
    assert any(err["is_hard"] for err in s2_end["errors"])
    assert ctx.all_ok() is False


def test_step_context_results_append_only():
    ctx = StepContext(session_id="s", user_text="hi")
    r1 = StepResult(STEP1_PARSE, ok=True)
    ctx.set_result(STEP1_PARSE, r1)
    assert ctx.get_result(STEP1_PARSE) is r1
    r2 = StepResult(STEP2_VALIDATE, ok=False,
                    errors=[StepError(step_id=STEP2_VALIDATE,
                                      error_type="x", message="m")])
    ctx.set_result(STEP2_VALIDATE, r2)
    # 前一步结果不被后步改写
    assert ctx.get_result(STEP1_PARSE) is r1
    assert ctx.get_result(STEP2_VALIDATE) is r2


def test_step_error_to_event_has_step_id():
    err = StepError(step_id=STEP3_EXECUTE, error_type="write_fail",
                    message="写入失败", table="pet", sheet="Sheet1",
                    column="name", is_hard=True)
    ev = err.to_event()
    assert ev["step_id"] == STEP3_EXECUTE
    assert ev["is_hard"] is True
    assert ev["table"] == "pet"
    assert ev["column"] == "name"


def test_sse_all_events_carry_step_id():
    assert SSE.stage_start(STEP1_PARSE)["step_id"] == STEP1_PARSE
    assert SSE.progress(STEP1_PARSE, "thinking", detail="x")["step_id"] == STEP1_PARSE
    assert SSE.subtask(STEP3_EXECUTE, 1, 3)["step_id"] == STEP3_EXECUTE
    assert SSE.ask(STEP2_VALIDATE, "缺字段")["step_id"] == STEP2_VALIDATE
    ctx = StepContext(session_id="s", user_text="hi")
    ctx.set_result(STEP1_PARSE, StepResult(STEP1_PARSE, ok=True))
    done_ev = SSE.done(ctx)
    assert done_ev["ok"] is True
    assert done_ev["steps"][0]["step_id"] == STEP1_PARSE


def test_step1_parse_subagent_empty_text_returns_hard_error():
    agent = Step1ParseSubAgent()
    ctx = StepContext(session_id="s", user_text="")
    r = agent.execute(ctx)
    assert r.step_id == STEP1_PARSE
    assert r.ok is False
    assert any(e.is_hard for e in r.errors)
    assert r.errors[0].step_id == STEP1_PARSE  # 归属固定

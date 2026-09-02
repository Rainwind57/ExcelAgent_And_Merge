"""StepTrace 统一账本 + 慢因归因单测（纯函数，0 LLM，确定性）。"""
from agent.excel.core.pipeline.step_trace import build_step_trace, DEFAULT_THRESHOLDS


def test_aggregates_step_and_llm_metrics():
    sm = {
        "step1_parse": {"dur_ms": 5000, "candidate_count": 3, "schema_chars": 1200},
        "step2_validate": {"dur_ms": 300, "intents": 2},
        "step3_execute": {"dur_ms": 800, "llm_calls": 0},
    }
    ls = {"total_calls": 4, "total_dur_ms": 4200, "total_prompt_chars": 1500,
          "total_resp_chars": 900, "total_timeouts": 0, "total_errors": 0}
    tr = build_step_trace(sm, ls)
    assert tr["totals"]["dur_ms"] == 6100
    assert tr["totals"]["llm_calls"] == 4
    assert tr["totals"]["candidate_count"] == 3
    assert tr["slow_cause"] == "none"
    assert tr["attribution"] == []


def test_slow_cause_llm_timeout_has_priority():
    sm = {"step1_parse": {"dur_ms": 100000, "candidate_count": 20}}
    ls = {"total_calls": 3, "total_timeouts": 2, "total_prompt_chars": 999999}
    tr = build_step_trace(sm, ls)
    # 超时优先于 schema 过长/候选过多/慢步
    assert tr["slow_cause"] == "llm_timeout"
    causes = {a["cause"] for a in tr["attribution"]}
    assert {"llm_timeout", "schema_too_large", "candidate_overflow", "slow_step"} <= causes


def test_slow_cause_schema_too_large():
    sm = {"step1_parse": {"dur_ms": 1000, "schema_chars": 25000}}
    tr = build_step_trace(sm, {"total_calls": 1})
    assert tr["slow_cause"] == "schema_too_large"


def test_slow_cause_candidate_overflow():
    sm = {"step1_parse": {"dur_ms": 1000, "candidate_count": 12}}
    tr = build_step_trace(sm, None)
    assert tr["slow_cause"] == "candidate_overflow"


def test_slow_cause_slow_step():
    sm = {"step3_execute": {"dur_ms": 9000}}
    tr = build_step_trace(sm, None)
    assert tr["slow_cause"] == "slow_step"
    assert tr["attribution"][0]["evidence"].startswith("step3_execute")


def test_thresholds_override():
    sm = {"step1_parse": {"dur_ms": 1000, "candidate_count": 5}}
    tr = build_step_trace(sm, None, thresholds={"candidate_large": 4})
    assert tr["slow_cause"] == "candidate_overflow"


def test_empty_inputs_safe():
    tr = build_step_trace({}, None)
    assert tr["slow_cause"] == "none"
    assert tr["totals"]["dur_ms"] == 0
    assert tr["steps"] == {}

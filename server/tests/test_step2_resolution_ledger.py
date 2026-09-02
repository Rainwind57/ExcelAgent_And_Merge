"""主线1：Step2 把已解决决策登记进 ctx 级 resolution 台账（additive，0 LLM）。"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.pipeline.contracts import (
    StepContext, StepResult, STEP1_PARSE,
)
from agent.excel.core.pipeline.step2_validate_subagent import Step2ValidateSubAgent


def _intent(pk_resolved=None, skipped=False):
    it = types.SimpleNamespace(
        action="add", table_hint="reward", sheet_hint="Reward",
        extras={"fields": {"名称": "包A"}}, failures=[],
        validation=types.SimpleNamespace(skipped=skipped),
        target_field=None,
    )
    if pk_resolved is not None:
        it.extras["_pk_resolved"] = pk_resolved
    return it


def _ctx_with(intents):
    ctx = StepContext(session_id="s1", user_text="新增奖励包")
    s1 = StepResult(step_id=STEP1_PARSE, ok=True,
                    artifacts={"intents": intents})
    ctx.set_result(STEP1_PARSE, s1)
    return ctx


def test_step2_records_pk_resolution_into_ledger():
    # services=None → 跳过 legacy 校验，仅走结构检查 + 台账登记
    agent = Step2ValidateSubAgent(services=None)
    ctx = _ctx_with([_intent(pk_resolved={"col": "reward_id", "value": 100604})])
    agent.execute(ctx)

    led = ctx.get_ledger()
    assert len(led) == 1
    # 内容派生的 stable id 可复现
    from agent.excel.core.pipeline.resolution_ledger import make_issue_id
    iid = make_issue_id(kind="pk_resolved", table="reward", sheet="Reward",
                        col="reward_id", value=100604)
    assert led.has(iid)
    assert led.get(iid).resolved == 100604
    assert led.get(iid).source == "validator"


def test_step2_records_skipped_validation_into_ledger():
    agent = Step2ValidateSubAgent(services=None)
    ctx = _ctx_with([_intent(skipped=True)])
    agent.execute(ctx)
    led = ctx.get_ledger()
    assert any(r.kind == "validation_skipped" and r.status == "skipped"
               for r in led._items.values())


def test_step2_no_resolution_leaves_ledger_empty():
    agent = Step2ValidateSubAgent(services=None)
    ctx = _ctx_with([_intent()])
    agent.execute(ctx)
    assert len(ctx.get_ledger()) == 0


def test_ledger_persists_as_plain_dict_on_ctx():
    # 台账以纯 dict 持久在 ctx 上（deepcopy 安全、跨 Step 携带）
    agent = Step2ValidateSubAgent(services=None)
    ctx = _ctx_with([_intent(pk_resolved={"col": "reward_id", "value": 7})])
    agent.execute(ctx)
    import copy
    ctx2 = copy.deepcopy(ctx)
    assert isinstance(ctx2.resolution_ledger, dict)
    assert len(ctx2.get_ledger()) == 1

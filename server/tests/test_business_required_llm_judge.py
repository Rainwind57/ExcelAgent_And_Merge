# -*- coding: utf-8 -*-
"""验证 Step2 "业务必填豁免" 新增的第 5 条 opt-in LLM 二次判断：

`_check_business_required_pre_add` 原有 4 条豁免全是硬编码关键词/正则
（全空列/否定式/同族已填/反规范化镜像列），覆盖不到的场景会被误判"漏填"
直接硬阻断 + 整条 intent 标 skipped。新增 §豁免5：开关关闭时行为完全不变
（默认关，零风险）；开关打开后，遇到前 4 条都没豁免掉的列，先让模型结合
原始指令二次判断，判"可选"才豁免，判不出来/异常/关闭时维持原硬阻断。
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import IssueType


class _FakeIntent:
    def __init__(self, table_hint="activity", sheet_hint="Activity"):
        self.action = "add"
        self.table_hint = table_hint
        self.sheet_hint = sheet_hint
        self.raw = ""
        self.validation = None


def _run_check(headers, fields, raw, existing, env=None, llm_raw_resp=None):
    v = ValidatorAgent()
    v._cli = None
    it = _FakeIntent()
    it.raw = raw
    env = env or {}
    with mock.patch.dict(os.environ, env, clear=False):
        if llm_raw_resp is not None:
            with mock.patch.object(ValidatorAgent, "_ensure_own_session", return_value="sid-1"), \
                 mock.patch.object(ValidatorAgent, "_call_llm_raw", return_value=llm_raw_resp) as mocked:
                issues = v._check_business_required_pre_add(
                    it, headers, fields, raw, existing_values=existing)
                return issues, mocked
        issues = v._check_business_required_pre_add(
            it, headers, fields, raw, existing_values=existing)
        return issues, None


def test_default_off_keeps_original_hard_block():
    """开关默认关闭时，行为与改动前完全一致：命中启发式漏填仍硬报，不调 LLM。"""
    headers = ["活动id", "活动名称", "活动描述"]
    fields = {"活动id": 3060}
    existing = {"活动名称": {"旧活动"}, "活动描述": {"旧描述"}, "活动id": {"3000"}}
    raw = "开一个活动叫'九霄论剑'"
    issues, mocked = _run_check(headers, fields, raw, existing, env={})
    flagged = {getattr(i, "col", "") for i in issues}
    assert "活动名称" in flagged, f"默认关闭时应维持原硬阻断，实际 {flagged}"
    assert mocked is None  # 没有触发 LLM mock 分支（本用例本就没传 llm_raw_resp）


def test_llm_optional_verdict_exempts_column():
    """开启开关 + LLM 判"可选" -> 该列不再报 MISSING_REQUIRED。"""
    headers = ["活动id", "活动名称", "发送人"]
    fields = {"活动id": 3060, "活动名称": "九霄论剑"}
    existing = {"活动名称": {"旧活动"}, "活动id": {"3000"}, "发送人": {"系统"}}
    # raw 里出现"发送人"关键词触发 explicit_kws 分支，但 LLM 判定用户并未
    # 真的要求填这一列 -> 应被 §豁免5 摘除。
    raw = "开一个活动叫'九霄论剑'，发送人相关的以后再补"
    issues, mocked = _run_check(
        headers, fields, raw, existing,
        env={"CODEMAKER_VALIDATOR_LLM_BUSINESS_REQUIRED": "1"},
        llm_raw_resp='{"verdict": "optional", "reason": "指令未给出具体发送人"}',
    )
    flagged = {getattr(i, "col", "") for i in issues}
    assert "发送人" not in flagged, f"LLM 判可选应豁免，实际 {flagged}"
    assert mocked.called, "应触发 LLM 调用"


def test_llm_required_verdict_keeps_hard_block():
    """开启开关 + LLM 判"必须填" -> 仍然硬报，不被豁免。"""
    headers = ["活动id", "活动名称", "发送人"]
    fields = {"活动id": 3060, "活动名称": "九霄论剑"}
    existing = {"活动名称": {"旧活动"}, "活动id": {"3000"}, "发送人": {"系统"}}
    raw = "开一个活动叫'九霄论剑'，发送人写'系统'"
    issues, mocked = _run_check(
        headers, fields, raw, existing,
        env={"CODEMAKER_VALIDATOR_LLM_BUSINESS_REQUIRED": "1"},
        llm_raw_resp='{"verdict": "required", "reason": "指令明确给了发送人"}',
    )
    flagged = {getattr(i, "col", "") for i in issues}
    assert "发送人" in flagged, f"LLM 判必须填应维持硬阻断，实际 {flagged}"


def test_llm_unreachable_keeps_hard_block():
    """LLM 不可达（无 session）-> 维持原硬阻断，不静默放行。"""
    v = ValidatorAgent()
    v._cli = None
    headers = ["活动id", "活动名称", "发送人"]
    fields = {"活动id": 3060, "活动名称": "九霄论剑"}
    existing = {"活动名称": {"旧活动"}, "活动id": {"3000"}, "发送人": {"系统"}}
    it = _FakeIntent()
    it.raw = "开一个活动叫'九霄论剑'，发送人写'系统'"
    with mock.patch.dict(os.environ, {"CODEMAKER_VALIDATOR_LLM_BUSINESS_REQUIRED": "1"}), \
         mock.patch.object(ValidatorAgent, "_ensure_own_session", return_value=""):
        issues = v._check_business_required_pre_add(
            it, headers, fields, it.raw, existing_values=existing)
    flagged = {getattr(i, "col", "") for i in issues}
    assert "发送人" in flagged, f"LLM 不可达应维持硬阻断，实际 {flagged}"


def test_llm_judge_business_required_direct_verdicts():
    """直接测 _llm_judge_business_required 的三种响应路径。"""
    v = ValidatorAgent()
    with mock.patch.object(ValidatorAgent, "_ensure_own_session", return_value="sid-1"):
        with mock.patch.object(ValidatorAgent, "_call_llm_raw",
                               return_value='{"verdict":"optional","reason":"x"}'):
            assert v._llm_judge_business_required("发送人", "raw", [], {}) == "optional"
        with mock.patch.object(ValidatorAgent, "_call_llm_raw",
                               return_value='{"verdict":"required","reason":"x"}'):
            assert v._llm_judge_business_required("发送人", "raw", [], {}) == "required"
        with mock.patch.object(ValidatorAgent, "_call_llm_raw", return_value=""):
            assert v._llm_judge_business_required("发送人", "raw", [], {}) == ""
        with mock.patch.object(ValidatorAgent, "_call_llm_raw", return_value="不是JSON"):
            assert v._llm_judge_business_required("发送人", "raw", [], {}) == ""


if __name__ == "__main__":
    test_default_off_keeps_original_hard_block()
    test_llm_optional_verdict_exempts_column()
    test_llm_required_verdict_keeps_hard_block()
    test_llm_unreachable_keeps_hard_block()
    test_llm_judge_business_required_direct_verdicts()
    print("[PASS] all business-required llm-judge tests")

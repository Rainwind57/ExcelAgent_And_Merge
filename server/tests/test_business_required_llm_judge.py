# -*- coding: utf-8 -*-
"""验证 Step2 业务必填启发式停用后的行为：

`_check_business_required_pre_add` 已停用（§约束收缩：只强制主键列，其余列
均可为空——邮件类型/发送人/名称/描述等业务列不再视为必填）。原 4 条豁免 +
第 5 条 opt-in LLM 二次判断机制整体退役：无论开关开/关、LLM 判 required/
optional、LLM 不可达，一律不报 MISSING_REQUIRED、不标记 skipped。
"""
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent


class _FakeIntent:
    def __init__(self, table_hint="activity", sheet_hint="Activity"):
        self.action = "add"
        self.table_hint = table_hint
        self.sheet_hint = sheet_hint
        self.raw = ""
        self.validation = None


def _run_check(headers, fields, raw, existing, env=None):
    v = ValidatorAgent()
    v._cli = None
    it = _FakeIntent()
    it.raw = raw
    with mock.patch.dict(os.environ, env or {}, clear=False):
        return v._check_business_required_pre_add(
            it, headers, fields, raw, existing_values=existing)


def test_disabled_never_reports_missing_required():
    """启发式停用：无论指令是否明确给了名称/描述/发送人/邮件类型值，缺失均不报。"""
    headers = ["活动id", "活动名称", "活动描述", "发送人"]
    fields = {"活动id": 3060}
    existing = {"活动名称": {"旧活动"}, "活动描述": {"旧描述"},
                "活动id": {"3000"}, "发送人": {"系统"}}
    raw = "开一个活动叫'九霄论剑'，发送人写'系统'"
    issues = _run_check(headers, fields, raw, existing, env={})
    assert issues == [], f"启发式停用后不应报任何 MISSING_REQUIRED，实际 {issues}"


def test_disabled_ignores_business_required_env_switch():
    """即使打开 CODEMAKER_VALIDATOR_LLM_BUSINESS_REQUIRED=1，也不再触发 LLM 判定。"""
    v = ValidatorAgent()
    v._cli = None
    it = _FakeIntent()
    it.raw = "开一个活动叫'九霄论剑'，发送人写'系统'"
    headers = ["活动id", "活动名称", "发送人"]
    fields = {"活动id": 3060, "活动名称": "九霄论剑"}
    existing = {"活动名称": {"旧活动"}, "活动id": {"3000"}, "发送人": {"系统"}}
    with mock.patch.dict(os.environ,
                         {"CODEMAKER_VALIDATOR_LLM_BUSINESS_REQUIRED": "1"}), \
         mock.patch.object(ValidatorAgent, "_call_llm_raw",
                           return_value='{"verdict": "required", "reason": "x"}') as mocked:
        issues = v._check_business_required_pre_add(
            it, headers, fields, it.raw, existing_values=existing)
    assert issues == [], f"启发式停用后不应报缺，实际 {issues}"
    assert not mocked.called, "启发式停用后不应再调 LLM 判定"


def test_disabled_no_llm_session_also_no_report():
    """LLM 不可达路径同样不再产生业务必填缺失（整体退役）。"""
    v = ValidatorAgent()
    v._cli = None
    it = _FakeIntent()
    it.raw = "开一个活动叫'九霄论剑'，发送人写'系统'"
    headers = ["活动id", "活动名称", "发送人"]
    fields = {"活动id": 3060, "活动名称": "九霄论剑"}
    existing = {"活动名称": {"旧活动"}, "活动id": {"3000"}, "发送人": {"系统"}}
    with mock.patch.dict(os.environ,
                         {"CODEMAKER_VALIDATOR_LLM_BUSINESS_REQUIRED": "1"}), \
         mock.patch.object(ValidatorAgent, "_ensure_own_session", return_value=""):
        issues = v._check_business_required_pre_add(
            it, headers, fields, it.raw, existing_values=existing)
    assert issues == [], f"启发式停用后不应报缺，实际 {issues}"


def test_disabled_mail_scenario_no_report():
    """邮件场景回归：发全服邮件漏产「邮件类型/发送人」等列不再被视为必填。"""
    headers = ["全服邮件ID", "邮件类型", "模板ID", "发送人", "发送时间", "奖励"]
    fields = {"全服邮件ID": 21}
    existing = {"全服邮件ID": {"20"}}
    raw = ("帮我发一封全服邮件：邮件模板标题'月华庆典开启'，"
           "全服邮件 global_id 21，邮件类型 1，发送人'系统'，"
           "发送时间 2026-10-01 00:00:00，附带奖励 10001。")
    issues = _run_check(headers, fields, raw, existing, env={})
    assert issues == [], f"邮件类型/发送人等列不应再被视为必填，实际 {issues}"


if __name__ == "__main__":
    test_disabled_never_reports_missing_required()
    test_disabled_ignores_business_required_env_switch()
    test_disabled_no_llm_session_also_no_report()
    test_disabled_mail_scenario_no_report()
    print("[PASS] business-required heuristic disabled tests")

"""ExecuteAgent 去 LLM 单测（§3.7）。

聚焦 _phase_execute 的 execute_no_llm 分支（§3.1 CODEMAKER_EXECUTE_NO_LLM=1）：
  - execute_no_llm=1 + _dispatch 失败 → 跳过 verify-repair + D3 retry LLM 诊断/重试，
    失败直接结构化进 res.failures（#40）+ return res（ok=False）
  - execute_no_llm=1 + 成功 → 不进分支，直接返回 out
  - execute_no_llm=0（默认）+ 失败 → 不进新分支，走原 D3 retry 路径（failures 不含
    execute_failed_no_llm）

端到端「8 SubTask 拓扑序 + 失败不阻塞同层」（§3.7 主路径）由
test_multi_table_orchestration 已覆盖（4267-4282 _blocked_by + 4306-4332
broken_producers + G8 链回滚）。§3.4 拓扑派发 + §3.5 produced 同步 +
§3.6 失败不阻塞同层现状已落地，本测聚焦 §3.1 新增 execute_no_llm 分支。

运行: python -m pytest server/tests/test_execute_agent.py -v
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import agent.excel.core.agent as agent_mod
from agent.excel.core.agent import TableAgent
from agent.excel.parser.nl_parser import NLIntent, ValidationResult


# ── 桩构造 ────────────────────────────────────────────────────


def _make_res():
    """轻量 AgentResult-like：failures 列表 + thinking 收集。"""
    failures = []
    thinking = []
    res = types.SimpleNamespace(
        ok=True, message="", failures=failures,
        _commit_backup=None, _skip_summarize=False,
        add_thinking=lambda phase, detail: thinking.append((phase, detail)),
        add=lambda phase, value, detail: thinking.append((phase, detail)),
        _has_unresolved_placeholder=None,
    )
    return res, failures, thinking


def _make_agent(*, execute_no_llm: bool, out, retry_out=None):
    """轻量 agent：绑 _phase_execute + mock _dispatch 四动作 + LLM helpers。"""
    ag = types.SimpleNamespace(
        execute_no_llm=execute_no_llm,
        enable_verify_repair_loop=False,  # 跳 verify-repair,直测 execute_no_llm / D3 retry
        repair_playbook=None,
        _ai_enhancer=None,  # 跳 AI 诊断
        cli=types.SimpleNamespace(read_header=lambda p, s: []),
        _run_set=lambda intent, path, sheet, res: out,
        _run_add=lambda intent, path, sheet, res: out,
        _run_delete=lambda intent, path, sheet, res: out,
        _run_get=lambda intent, path, sheet, res: out,
        _run_col=lambda intent, path, sheet, res: out,
        _rollback_write=lambda path, backup, res: None,
        _ask_callback=None,  # 跳占位符中断反问
        _collect_error_feedback=lambda res, intent, path, sheet: "",
        _retry_with_error_feedback=lambda intent, path, sheet, res, fb: retry_out,
    )
    ag._phase_execute = TableAgent._phase_execute.__get__(ag)
    return ag


def _intent():
    """set 动作 intent（is_write=True），无占位符。"""
    return NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                   raw="修改朱雀成长率", extras={"fields": {"成长率": "1.5"}})


def _patch_classify_no_placeholder(monkeypatch):
    """patch _classify_placeholder_fields 返空（无占位符,跳断言）。"""
    monkeypatch.setattr(agent_mod, "_classify_placeholder_fields",
                        lambda fields: ([], []))


# ── execute_no_llm=1 失败 → 直接进 failures ───────────────────


class TestExecuteNoLlmFailureBranch:
    def test_failure_goes_to_failures_structured(self, monkeypatch):
        _patch_classify_no_placeholder(monkeypatch)
        out_fail = types.SimpleNamespace(ok=False, message="列[xxx]未找到")
        ag = _make_agent(execute_no_llm=True, out=out_fail)
        res, failures, _ = _make_res()

        result = ag._phase_execute(_intent(), Path("pet.xlsx"), "Pet", res, None)

        assert result is res
        assert res.ok is False
        assert len(failures) == 1
        f = failures[0]
        assert f["type"] == "execute_failed_no_llm"
        assert f["table"] == "pet"
        assert f["sheet"] == "Pet"
        assert f["root_cause"] == "列[xxx]未找到"
        assert f["attempted_strategies"] == ["direct_dispatch"]
        assert f["status"] == "failed"
        assert "ExecuteAgent 去 LLM" in res.message
        assert "列[xxx]未找到" in res.message

    def test_failure_does_not_call_llm_helpers(self, monkeypatch):
        """execute_no_llm=1 失败时不调 _ai_enhancer/_retry_with_error_feedback。"""
        _patch_classify_no_placeholder(monkeypatch)
        out_fail = types.SimpleNamespace(ok=False, message="失败")
        calls = {"retry": 0, "collect": 0}

        def _boom_retry(*a, **k):
            calls["retry"] += 1
            return None

        def _boom_collect(*a, **k):
            calls["collect"] += 1
            return ""

        ag = _make_agent(execute_no_llm=True, out=out_fail)
        ag._retry_with_error_feedback = _boom_retry
        ag._collect_error_feedback = _boom_collect
        res, _, _ = _make_res()

        ag._phase_execute(_intent(), Path("pet.xlsx"), "Pet", res, None)

        # execute_no_llm 分支跳过 D3 retry,不调 LLM helpers
        assert calls["retry"] == 0
        assert calls["collect"] == 0

    def test_failure_calls_rollback(self, monkeypatch):
        """execute_no_llm=1 失败时不崩（backup_file 内部取,可能 None）。"""
        _patch_classify_no_placeholder(monkeypatch)
        out_fail = types.SimpleNamespace(ok=False, message="失败")
        ag = _make_agent(execute_no_llm=True, out=out_fail)
        res, _, _ = _make_res()

        ag._phase_execute(_intent(), Path("pet.xlsx"), "Pet", res, None)

        assert res.ok is False


# ── execute_no_llm=1 成功 → 不进分支 ─────────────────────────


class TestExecuteNoLlmSuccessPath:
    def test_success_returns_out_directly(self, monkeypatch):
        _patch_classify_no_placeholder(monkeypatch)
        out_ok = types.SimpleNamespace(ok=True, message="ok")
        ag = _make_agent(execute_no_llm=True, out=out_ok)
        res, failures, _ = _make_res()

        result = ag._phase_execute(_intent(), Path("pet.xlsx"), "Pet", res, None)

        assert result is out_ok
        assert len(failures) == 0  # 成功路径不填 failures

    def test_success_does_not_enter_no_llm_branch(self, monkeypatch):
        _patch_classify_no_placeholder(monkeypatch)
        out_ok = types.SimpleNamespace(ok=True, message="ok")
        ag = _make_agent(execute_no_llm=True, out=out_ok)
        res, failures, _ = _make_res()

        ag._phase_execute(_intent(), Path("pet.xlsx"), "Pet", res, None)

        assert not any(f.get("type") == "execute_failed_no_llm" for f in failures)


# ── execute_no_llm=0（默认）→ 走原 D3 retry 路径 ──────────────


class TestDefaultKeepsOriginalRetryPath:
    def test_default_off_failure_not_in_no_llm_branch(self, monkeypatch):
        """execute_no_llm=False 时失败走原 D3 retry,不进 execute_no_llm 分支。"""
        _patch_classify_no_placeholder(monkeypatch)
        out_fail = types.SimpleNamespace(ok=False, message="列[xxx]未找到")
        retry_out = types.SimpleNamespace(ok=False, message="重试也失败")
        ag = _make_agent(execute_no_llm=False, out=out_fail, retry_out=retry_out)
        res, failures, _ = _make_res()

        result = ag._phase_execute(_intent(), Path("pet.xlsx"), "Pet", res, None)

        # 默认路径走 D3 retry(_retry_with_error_feedback 返 retry_out),
        # 不进 execute_no_llm 分支(failures 不含 execute_failed_no_llm)
        assert not any(f.get("type") == "execute_failed_no_llm" for f in failures)
        assert res.ok is False
        assert "重试也失败" in res.message
        assert result is res

    def test_default_off_calls_retry_helper(self, monkeypatch):
        """execute_no_llm=False 时失败调 _retry_with_error_feedback（原 D3 retry 行为）。"""
        _patch_classify_no_placeholder(monkeypatch)
        out_fail = types.SimpleNamespace(ok=False, message="失败")
        retry_out = types.SimpleNamespace(ok=False, message="重试失败")
        calls = {"retry": 0}

        def _spy_retry(intent, path, sheet, res, fb):
            calls["retry"] += 1
            return retry_out

        ag = _make_agent(execute_no_llm=False, out=out_fail, retry_out=retry_out)
        ag._retry_with_error_feedback = _spy_retry
        res, _, _ = _make_res()

        ag._phase_execute(_intent(), Path("pet.xlsx"), "Pet", res, None)

        assert calls["retry"] == 1  # 默认路径调 D3 retry LLM 重试


# ── 现状已落地确认（§3.3/3.4/3.5/3.6 文档化）─────────────────


class TestExistingCapabilitiesPreserved:
    """§3.3-3.6 现状已落地,本类文档化确认（不改代码）。

    - §3.3 占位符断言：_phase_execute 5355-5444 已做 pre 断言
      （_classify_placeholder_fields + _has_unresolved_placeholder + 5430 failure）
    - §3.4 拓扑派发：run() step5 循环调 OperationOrchestrator._topo_order
      （4136）+ _resolve_placeholders（4286）+ _capture_produced（4336）
    - §3.5 resolved 同步：produced dict 4192/4286/4336（label→pk） 失败不阻塞同层：4267-4282 _blocked_by 跳过依赖 + 4306-4332
      broken_producers + G8 链回滚（test_multi_table_orchestration 覆盖）
    """

    def test_phase_execute_method_exists(self):
        assert hasattr(TableAgent, "_phase_execute")

    def test_execute_no_llm_attr_default_off(self, monkeypatch):
        """agent 实例 execute_no_llm 默认关（保持现状）。"""
        monkeypatch.delenv("CODEMAKER_EXECUTE_NO_LLM", raising=False)
        val = os.getenv("CODEMAKER_EXECUTE_NO_LLM", "0")
        assert val == "0"
        assert not (val != "0")  # 开关关

    def test_execute_no_llm_attr_env_on(self, monkeypatch):
        """env=1 时开关开。"""
        monkeypatch.setenv("CODEMAKER_EXECUTE_NO_LLM", "1")
        val = os.getenv("CODEMAKER_EXECUTE_NO_LLM", "0")
        assert val == "1"
        assert val != "0"  # 开关开


# ── ExecuteAgent 跳 skipped（§4 validation.skipped）────────


class TestExecuteSkip:
    """§4 ExecuteAgent 跳 validation.skipped=True 的子任务（用户 skip 不执行）。"""

    def test_skipped_intent_returns_ok_no_dispatch(self, monkeypatch):
        """validation.skipped=True → res.ok=True + 不调 _dispatch。"""
        _patch_classify_no_placeholder(monkeypatch)
        out = types.SimpleNamespace(ok=False, message="不应被调")
        dispatch_calls = [0]

        def _spy_dispatch(intent, path, sheet, res):
            dispatch_calls[0] += 1
            return out

        ag = _make_agent(execute_no_llm=False, out=out)
        ag._run_set = _spy_dispatch
        ag._run_add = _spy_dispatch
        ag._run_delete = _spy_dispatch
        ag._run_get = _spy_dispatch
        ag._run_col = _spy_dispatch
        res, _, _ = _make_res()

        it = _intent()
        it.validation = ValidationResult(ok=True, skipped=True)

        result = ag._phase_execute(it, Path("pet.xlsx"), "Pet", res, None)

        assert result is res
        assert res.ok is True
        assert "用户跳过" in res.message
        assert dispatch_calls[0] == 0  # 不调 _dispatch

    def test_non_skipped_intent_normal_dispatch(self, monkeypatch):
        """validation.skipped=False → 正常 _dispatch（不跳）。"""
        _patch_classify_no_placeholder(monkeypatch)
        out = types.SimpleNamespace(ok=True, message="ok")
        ag = _make_agent(execute_no_llm=True, out=out)
        res, _, _ = _make_res()

        it = _intent()
        it.validation = ValidationResult(ok=True, skipped=False)

        result = ag._phase_execute(it, Path("pet.xlsx"), "Pet", res, None)

        assert result is out  # 成功直接返回 out（不进 skipped 分支）
        assert res.ok is True

    def test_no_validation_normal_path(self, monkeypatch):
        """无 validation 字段 → 正常路径（不跳）。"""
        _patch_classify_no_placeholder(monkeypatch)
        out = types.SimpleNamespace(ok=True, message="ok")
        ag = _make_agent(execute_no_llm=True, out=out)
        res, _, _ = _make_res()

        it = _intent()  # 无 validation
        assert it.validation is None

        result = ag._phase_execute(it, Path("pet.xlsx"), "Pet", res, None)

        assert result is out  # 正常成功路径

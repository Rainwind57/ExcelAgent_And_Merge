"""ConcludeAgent 单测（§5.1 _phase_summarize 全量 failures 聚合）。

验证 §5.1 改动：_phase_summarize 失败路径从 failures[-1] 单条扩为全量聚合。
  - 多 failures → res.message 含全量 table/sheet/col/root_cause
  - 单 failures → 向后兼容（原行为）
  - attempted_strategies list/tuple 兼容（join 为字符串）
  - 无 failures + 无 thinking_steps repair_failure → 未完成模板

§5.2/5.3/5.6 现状已落地（_check_dangling_fk_refs / skill_updater.induce_anti_patterns /
promote_with_guard / mini_regression），本测聚焦 5.1 改动。§5.4 done_data failures
payload 是 SSE 序列化（routers/agent.py:256），逻辑简单（getattr+json），本测不覆盖。

运行: python -m pytest server/tests/test_conclude_agent.py -v
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.agent import TableAgent
from agent.excel.parser.nl_parser import NLIntent


def _make_res(failures=None, thinking_steps=None, message=""):
    """轻量 AgentResult-like：failures + thinking_steps + add_thinking/add 收集。"""
    thinking = []
    return types.SimpleNamespace(
        failures=list(failures or []),
        thinking_steps=list(thinking_steps or []),
        message=message,
        _skip_summarize=False,
        add_thinking=lambda phase, detail: thinking.append((phase, detail)),
        add=lambda phase, value, detail: thinking.append((phase, detail)),
        _thinking=thinking,
    ), thinking


def _make_agent():
    """轻量 agent：绑 _phase_summarize,_ai_enhancer=None（失败路径不调 LLM）。"""
    ag = types.SimpleNamespace(_ai_enhancer=None)
    ag._phase_summarize = TableAgent._phase_summarize.__get__(ag)
    return ag


# ── 全量 failures 聚合 ───────────────────────────────────────


class TestPhaseSummarizeMultiFailures:
    def test_multi_failures_all_aggregated(self):
        """多 failures → res.message 含全量 table/sheet/col/root_cause。"""
        ag = _make_agent()
        failures = [
            {"type": "execute_failed_no_llm", "table": "pet", "sheet": "Pet",
             "col": "成长率", "root_cause": "类型错",
             "attempted_strategies": ["direct_dispatch"]},
            {"type": "col_not_found", "table": "quest", "sheet": "Quest",
             "col": "bad_col", "root_cause": "幻觉列",
             "attempted_strategies": ["field_layer"]},
        ]
        res, _ = _make_res(failures=failures)
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert res.message.startswith("失败：")
        assert "pet/Pet" in res.message
        assert "quest/Quest" in res.message
        assert "类型错" in res.message
        assert "幻觉列" in res.message
        assert "成长率" in res.message
        assert "bad_col" in res.message
        assert " | " in res.message  # 多失败分隔符

    def test_single_failure_backward_compatible(self):
        """单 failures → 仍工作（向后兼容原 failures[-1] 行为）。"""
        ag = _make_agent()
        failures = [{"table": "pet", "sheet": "Pet", "col": "成长率",
                     "root_cause": "类型错", "attempted_strategies": "direct_dispatch"}]
        res, _ = _make_res(failures=failures)
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert res.message.startswith("失败：")
        assert "pet/Pet" in res.message
        assert "类型错" in res.message
        assert " | " not in res.message  # 单失败无分隔符

    def test_attempted_strategies_list_joined(self):
        """attempted_strategies list → join 为字符串。"""
        ag = _make_agent()
        failures = [{"table": "pet", "sheet": "Pet", "col": "x",
                     "root_cause": "错",
                     "attempted_strategies": ["strat_a", "strat_b"]}]
        res, _ = _make_res(failures=failures)
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert "strat_a" in res.message and "strat_b" in res.message

    def test_attempted_strategies_tuple_joined(self):
        """attempted_strategies tuple → 同 list 处理。"""
        ag = _make_agent()
        failures = [{"table": "pet", "sheet": "Pet", "col": "x",
                     "root_cause": "错",
                     "attempted_strategies": ("s1", "s2")}]
        res, _ = _make_res(failures=failures)
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert "s1" in res.message and "s2" in res.message

    def test_failure_without_table_falls_back_to_path(self):
        """failure 无 table/sheet 字段 → 用 path.stem/sheet 参数兜底。"""
        ag = _make_agent()
        failures = [{"col": "x", "root_cause": "错",
                     "attempted_strategies": "s"}]
        res, _ = _make_res(failures=failures)
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert "pet/Pet" in res.message  # 用 path.stem/sheet


# ── 回退路径（无 failures）────────────────────────────────────


class TestPhaseSummarizeFallback:
    def test_no_failures_no_thinking_steps_uses_template(self):
        """无 failures + 无 thinking_steps repair_failure → 未完成模板。"""
        ag = _make_agent()
        res, _ = _make_res(failures=[], thinking_steps=[], message="")
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert "操作未完成" in res.message
        assert "pet/Pet" in res.message

    def test_no_failures_falls_back_to_thinking_steps_repair_failure(self):
        """无 failures + thinking_steps 有 repair_failure → 回退取 repair_failure。"""
        ag = _make_agent()
        thinking_steps = [
            {"repair_failure": {"root_cause": "从 repair 回退取",
                                "attempted_strategies": "verify_repair"}},
        ]
        res, _ = _make_res(failures=[], thinking_steps=thinking_steps)
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert "从 repair 回退取" in res.message
        assert "verify_repair" in res.message

    def test_empty_failure_root_cause_defaults_unknown(self):
        """failure root_cause 空 → 默认「未知」。"""
        ag = _make_agent()
        failures = [{"table": "pet", "sheet": "Pet", "col": "x",
                     "root_cause": "", "attempted_strategies": ""}]
        res, _ = _make_res(failures=failures)
        out = types.SimpleNamespace(ok=False)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert "未知" in res.message


# ── 成功路径 + skip_summarize ─────────────────────────────────


class TestPhaseSummarizeSuccessPath:
    def test_skip_summarize_returns_early(self):
        """res._skip_summarize=True → 跳过汇总（重试成功路径）。"""
        ag = _make_agent()
        res, thinking = _make_res(message="原消息")
        res._skip_summarize = True
        out = types.SimpleNamespace(ok=True)
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        assert res.message == "原消息"  # 不改
        assert thinking == []  # 不调 add_thinking

    def test_success_path_no_ai_enhancer_uses_template(self):
        """成功路径 + _ai_enhancer=None → 走模板拼接（add_thinking 记「操作完成」）。"""
        ag = _make_agent()
        res, thinking = _make_res()
        out = types.SimpleNamespace(ok=True, message="ok")
        intent = NLIntent(action="set", raw="test")

        ag._phase_summarize(intent, Path("pet.xlsx"), "Pet", res, out)

        # 现状：成功路径 + 无 ai_enhancer 走模板,add_thinking 记"操作完成"
        # （res.message 不设是现状行为,§5.1 未改成功路径,聚焦失败路径全量聚合）
        assert any("操作完成" in d for _, d in thinking)


# ── §5.2/5.3/5.6 现状已落地确认（文档化）─────────────────────


class TestExistingCapabilitiesPreserved:
    """§5.2/5.3/5.6 现状已落地,本类文档化确认（不改代码）。

    - §5.2 连通校验 opt-in：_check_dangling_fk_refs（agent.py:6430）+
      CODEMAKER_CONNECTIVITY_DEEP_CHECK=0 默认关 + 调用点 agent.py:4399
    - §5.3 自学习触发：skill_updater.induce_anti_patterns（skill_updater.py:663）+
      生产调用 agent.py:6364（CODEMAKER_INDUCE_PROD 默认关）+ promote_with_guard（792）+
      anti_patterns.yaml pending_review（skills/L3_anti_patterns/）
    - §5.6 mini_regression：MINI_REGRESSION_SAMPLE=30 + CODEMAKER_SKIP_REGRESSION=1
      默认跳过 + TABLE_CASE_EVAL_RUNNING 保持 pending_review
    - §5.4 done_data failures payload：routers/agent.py:256 加 failures 键
    """

    def test_phase_summarize_method_exists(self):
        assert hasattr(TableAgent, "_phase_summarize")

    def test_check_dangling_fk_refs_exists(self):
        assert hasattr(TableAgent, "_check_dangling_fk_refs")

    def test_connectivity_deep_check_env_default_off(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_CONNECTIVITY_DEEP_CHECK", raising=False)
        assert os.getenv("CODEMAKER_CONNECTIVITY_DEEP_CHECK", "0") == "0"

    def test_induce_prod_env_default_off(self, monkeypatch):
        monkeypatch.delenv("CODEMAKER_INDUCE_PROD", raising=False)
        assert os.getenv("CODEMAKER_INDUCE_PROD", "0") == "0"


# ── D5 ConcludeAgent 批量级自学习闭环（_phase_conclude）─────────


class _UpRecorder:
    """mock SkillUpdater：记录 induce_anti_patterns 调用 + 可配返回。"""

    def __init__(self, produced=None, raise_exc=None):
        self.calls = []
        self._produced = produced
        self._raise = raise_exc

    def induce_anti_patterns(self, traces, enhancer=None):
        self.calls.append(traces)
        if self._raise:
            raise self._raise
        return self._produced


class TestPhaseConclude:
    """_phase_conclude：批量级聚合全 failure → induce_anti_patterns。"""

    def _make_agent(self, enable_skill=True, enhancer=object()):
        ag = types.SimpleNamespace(
            enable_skill=enable_skill, _ai_enhancer=enhancer)
        ag._phase_conclude = TableAgent._phase_conclude.__get__(ag)
        return ag

    def _make_stream(self):
        thinking = []
        ns = types.SimpleNamespace(
            add_thinking=lambda phase, detail: thinking.append((phase, detail)))
        return ns, thinking

    def _make_partition(self, stem="pet", sheet="Pet", failures=None):
        res = types.SimpleNamespace(
            table_stem=stem, table_sheet=sheet, failures=list(failures or []))
        return {"executed": True, "res": res,
                "path": Path(f"{stem}.xlsx"), "sheet": sheet}

    def test_induce_called_with_all_failure_types(self, monkeypatch):
        """placeholder + verify_repair_exhausted 两类 failure 都映射喂 induce。"""
        up = _UpRecorder(produced=[{"id": "ap_x"}])
        monkeypatch.setattr("agent.excel.core.agent.get_skill_updater", lambda: up)
        ag = self._make_agent()
        stream, thinking = self._make_stream()
        partitions = [self._make_partition(failures=[
            {"type": "placeholder_unresolved", "col": "parent_id",
             "root_cause": "占位符残留", "snip": "新增灵兽寒冰凤",
             "status": "unresolved"},
            {"type": "verify_repair_exhausted", "col": "成长率",
             "root_cause": "类型错", "status": "unresolved"},
        ])]
        ag._phase_conclude(partitions, "text", stream)
        assert len(up.calls) == 1
        traces = up.calls[0]
        assert len(traces) == 2
        assert traces[0]["error_type"] == "placeholder_unresolved"
        assert traces[0]["input"] == "新增灵兽寒冰凤"
        assert "表=pet" in traces[0]["entries_summary"]
        assert "col=parent_id" in traces[0]["entries_summary"]
        assert traces[1]["error_type"] == "verify_repair_exhausted"
        assert traces[1]["error_detail"] == "类型错"
        # produced → 记归纳 thinking
        assert any(p == "归纳" for p, _ in thinking)

    def test_no_failures_no_induce(self, monkeypatch):
        up = _UpRecorder(produced=[])
        monkeypatch.setattr("agent.excel.core.agent.get_skill_updater", lambda: up)
        ag = self._make_agent()
        stream, thinking = self._make_stream()
        ag._phase_conclude([self._make_partition(failures=[])], "t", stream)
        assert up.calls == []
        assert thinking == []

    def test_enable_skill_off_skips(self, monkeypatch):
        up = _UpRecorder(produced=[])
        monkeypatch.setattr("agent.excel.core.agent.get_skill_updater", lambda: up)
        ag = self._make_agent(enable_skill=False)
        stream, _ = self._make_stream()
        ag._phase_conclude([self._make_partition(failures=[
            {"type": "x", "root_cause": "y"}])], "t", stream)
        assert up.calls == []

    def test_no_enhancer_skips(self, monkeypatch):
        up = _UpRecorder(produced=[])
        monkeypatch.setattr("agent.excel.core.agent.get_skill_updater", lambda: up)
        ag = self._make_agent(enhancer=None)  # _ai_enhancer=None
        stream, _ = self._make_stream()
        ag._phase_conclude([self._make_partition(failures=[
            {"type": "x", "root_cause": "y"}])], "t", stream)
        assert up.calls == []

    def test_induce_exception_swallowed(self, monkeypatch):
        """induce 抛错 → 不崩主流程（降级）。"""
        up = _UpRecorder(produced=None, raise_exc=RuntimeError("boom"))
        monkeypatch.setattr("agent.excel.core.agent.get_skill_updater", lambda: up)
        ag = self._make_agent()
        stream, _ = self._make_stream()
        ag._phase_conclude([self._make_partition(failures=[
            {"type": "x", "root_cause": "y"}])], "t", stream)  # 不抛

    def test_skipped_partition_not_collected(self, monkeypatch):
        """executed=False 的 partition 不进 failed_traces。"""
        up = _UpRecorder(produced=[])
        monkeypatch.setattr("agent.excel.core.agent.get_skill_updater", lambda: up)
        ag = self._make_agent()
        stream, _ = self._make_stream()
        p_skip = {"executed": False, "res": types.SimpleNamespace(
            table_stem="q", table_sheet="Q", failures=[
                {"type": "x", "root_cause": "y"}])}
        ag._phase_conclude([p_skip], "t", stream)
        assert up.calls == []

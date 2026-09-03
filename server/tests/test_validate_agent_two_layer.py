"""ValidateAgent 两段式单测（§4.8）。

本批覆盖 §4.1 validate_field_layer + §4.4 assemble_tips/IssueType（字段层 2 项 +
tips 序列化）。FK 拓扑层（§4.2）+ 交互反问（§4.5）+ e2e（§4.9）留后续批次。

覆盖：
  - 字段层列存在性：LLM 幻觉列 → COL_NOT_FOUND issue
  - 字段层类型 coerce：int 列传非数字 → TYPE_MISMATCH issue
  - 字段层全过：列存在 + 类型 ok → 无 issue
  - 占位符值软跳过（<auto>/<label> 不报错）
  - schema 拉不到 → SCHEMA_MISSING issue
  - assemble_tips 序列化 {subtask_id: [Issue]} → 前端 tips 列表
  - IssueType 枚举值

FK 层（validate_fk_layer）/ LLM 前向引用裁决（_validate_forward_refs_llm #19）
现状已落地（validator_agent.py:248），§4.2 拓扑序对齐留后续批次。

运行: python -m pytest server/tests/test_validate_agent_two_layer.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parser.nl_parser import (
    Issue, IssueType, NLIntent, ValidationResult, assemble_tips,
)
from agent.excel.subagent.validator_agent import ValidatorAgent


# ── 桩 ────────────────────────────────────────────────────────


def _intent(table="pet", sheet="Pet", fields=None):
    it = NLIntent(action="add", table_hint=table, sheet_hint=sheet,
                  raw="test", extras={"fields": fields or {}})
    return it


def _schema_getter(headers, type_row=None):
    """返回 schema_getter callable,固定返 (headers, type_row)。"""
    return lambda intent: (list(headers), list(type_row or []))


def _make_validator():
    """轻量 ValidatorAgent（不实例化 LLMSubAgent 全依赖,用 __new__ 绕过 __init__）。"""
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v._parser = None
    v._ask_callback = None  # __init__ 设的属性,绕过 __init__ 需手动补
    v._required_fields = {}  # §P26 设空 dict 避免懒加载 required_fields.yaml（测试不校验必填）
    v._pk_cols_cache = {}  # §P26 设空 dict 避免 table_relations 懒加载
    return v


# ── 字段层：列存在性 ──────────────────────────────────────────


class TestFieldLayerColExistence:
    def test_col_not_found_for_hallucinated_column(self):
        """LLM 幻觉列（不在表头）→ COL_NOT_FOUND issue。"""
        v = _make_validator()
        # LLM 产了"魔法值"列,但表头只有 pet_id/名称/成长率
        it = _intent(fields={"pet_id": 1, "魔法值": 999})
        sg = _schema_getter(["pet_id", "名称", "成长率"],
                            ["int", "string", "float"])
        out = v.validate_field_layer([it], schema_getter=sg)
        sid = id(it)
        assert sid in out
        issues = out[sid]
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.COL_NOT_FOUND.value
        assert issues[0].col == "魔法值"
        assert "魔法值" in issues[0].suggestion

    def test_col_exists_no_issue(self):
        """列存在 + 类型 ok → 无 issue。"""
        v = _make_validator()
        it = _intent(fields={"pet_id": 1, "成长率": 1.5})
        sg = _schema_getter(["pet_id", "成长率"], ["int", "float"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert out[id(it)] == []


# ── 字段层：类型 coerce ───────────────────────────────────────


class TestFieldLayerTypeCoerce:
    def test_int_type_mismatch(self):
        """int 列传非数字 → TYPE_MISMATCH。"""
        v = _make_validator()
        it = _intent(fields={"pet_id": "不是数字"})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_field_layer([it], schema_getter=sg)
        issues = out[id(it)]
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.TYPE_MISMATCH.value
        assert "int" in issues[0].expected

    def test_float_coerce_ok(self):
        """float 列传可解析数字 → 无 issue。"""
        v = _make_validator()
        it = _intent(fields={"成长率": "1.5"})
        sg = _schema_getter(["成长率"], ["float"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert out[id(it)] == []

    def test_bool_type_mismatch(self):
        """bool 列传非 bool → TYPE_MISMATCH。"""
        v = _make_validator()
        it = _intent(fields={"是否可见": "可能"})
        sg = _schema_getter(["是否可见"], ["bool"])
        out = v.validate_field_layer([it], schema_getter=sg)
        issues = out[id(it)]
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.TYPE_MISMATCH.value

    def test_bool_valid_values(self):
        """bool 列接受 true/false/1/0/是/否。"""
        v = _make_validator()
        for val in ("true", "false", "1", "0", "是", "否"):
            it = _intent(fields={"flag": val})
            sg = _schema_getter(["flag"], ["bool"])
            out = v.validate_field_layer([it], schema_getter=sg)
            assert out[id(it)] == [], f"bool 值 {val} 应通过"


# ── 占位符软跳过 ──────────────────────────────────────────────


class TestPlaceholderSoftSkip:
    def test_placeholder_label_skipped(self):
        """<new_pet_id> 占位符值不报类型错（待拓扑序前序产出替换）。"""
        v = _make_validator()
        it = _intent(fields={"parent_id": "<new_pet_id>"})
        sg = _schema_getter(["parent_id"], ["int"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert out[id(it)] == []  # 占位符软跳过

    def test_auto_placeholder_skipped(self):
        """<auto> 占位符（用户未提的可选列）软跳过。"""
        v = _make_validator()
        it = _intent(fields={"备注": "<auto>"})
        sg = _schema_getter(["备注"], ["string"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert out[id(it)] == []

    def test_empty_value_skipped(self):
        """空字符串软跳过。"""
        v = _make_validator()
        it = _intent(fields={"名称": ""})
        sg = _schema_getter(["名称"], ["string"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert out[id(it)] == []


# ── schema 缺失 ───────────────────────────────────────────────


class TestSchemaMissing:
    def test_schema_getter_returns_empty(self):
        """schema_getter 返空表头 → SCHEMA_MISSING issue。"""
        v = _make_validator()
        it = _intent(fields={"pet_id": 1})
        sg = _schema_getter([], [])
        out = v.validate_field_layer([it], schema_getter=sg)
        issues = out[id(it)]
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.SCHEMA_MISSING.value

    def test_no_schema_getter_no_cli(self):
        """无 schema_getter 且 _cli=None → SCHEMA_MISSING（validator 无 path 解析）。"""
        v = _make_validator()  # _cli=None
        it = _intent(fields={"pet_id": 1})
        out = v.validate_field_layer([it], schema_getter=None)
        issues = out[id(it)]
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.SCHEMA_MISSING.value

    def test_schema_getter_exception_returns_empty(self):
        """schema_getter 抛错 → 降级返空 schema → SCHEMA_MISSING。"""
        v = _make_validator()
        it = _intent(fields={"pet_id": 1})

        def boom(intent):
            raise RuntimeError("schema boom")

        out = v.validate_field_layer([it], schema_getter=boom)
        issues = out[id(it)]
        assert len(issues) == 1
        assert issues[0].issue_type == IssueType.SCHEMA_MISSING.value


# ── 空 fields / 空 intents ────────────────────────────────────


class TestEmptyPaths:
    def test_empty_intents_returns_empty_map(self):
        v = _make_validator()
        assert v.validate_field_layer([], schema_getter=_schema_getter([])) == {}

    def test_intent_no_fields_returns_empty_issues(self):
        """intent extras 无 fields → 该 subtask issues=[]。"""
        v = _make_validator()
        it = NLIntent(action="add", table_hint="pet", raw="test", extras={})
        out = v.validate_field_layer([it], schema_getter=_schema_getter(["x"]))
        assert out[id(it)] == []


# ── assemble_tips 序列化（§4.4）──────────────────────────────


class TestAssembleTips:
    def test_issue_objects_serialized_to_tips(self):
        """{sid: [Issue]} → [{subtask_id, col, issue_type, expected, suggestion}]。"""
        v = _make_validator()
        it = _intent(fields={"魔法值": 999})
        sg = _schema_getter(["pet_id"], ["int"])
        issues_map = v.validate_field_layer([it], schema_getter=sg)
        tips = assemble_tips(issues_map)
        assert len(tips) == 1
        t = tips[0]
        assert t["subtask_id"] == id(it)
        assert t["col"] == "魔法值"
        assert t["issue_type"] == IssueType.COL_NOT_FOUND.value
        assert "expected" in t and "suggestion" in t

    def test_dict_issues_serialized(self):
        """dict 形式 issue 也兼容序列化。"""
        sid = "subtask_1"
        issues_map = {sid: [{"col": "x", "issue_type": "type_mismatch",
                            "expected": "int", "suggestion": "改值"}]}
        tips = assemble_tips(issues_map)
        assert len(tips) == 1
        assert tips[0]["subtask_id"] == sid
        assert tips[0]["col"] == "x"

    def test_empty_issues_map_returns_empty_tips(self):
        assert assemble_tips({}) == []
        assert assemble_tips(None) == []

    def test_subtask_with_no_issues_skipped(self):
        """无 issue 的 subtask 不产 tip。"""
        issues_map = {"a": [], "b": [Issue(col="x", issue_type="type_mismatch")]}
        tips = assemble_tips(issues_map)
        assert len(tips) == 1
        assert tips[0]["subtask_id"] == "b"

    def test_multiple_intents_multiple_tips(self):
        """多 intent 多 issue → tips 按序聚合。"""
        v = _make_validator()
        it1 = _intent(table="pet", fields={"魔法值": 999})
        it2 = _intent(table="quest", fields={"bad_col": "x", "quest_id": "非数"})
        sg1 = _schema_getter(["pet_id"], ["int"])
        # 同一 schema_getter 对两 intent 返不同 schema
        schemas = {id(it1): (["pet_id"], ["int"]),
                   id(it2): (["quest_id"], ["int"])}
        sg = lambda intent: schemas[id(intent)]
        issues_map = v.validate_field_layer([it1, it2], schema_getter=sg)
        tips = assemble_tips(issues_map)
        # it1: 魔法值 COL_NOT_FOUND; it2: bad_col COL_NOT_FOUND + quest_id TYPE_MISMATCH
        assert len(tips) == 3
        issue_types = [t["issue_type"] for t in tips]
        assert IssueType.COL_NOT_FOUND.value in issue_types
        assert IssueType.TYPE_MISMATCH.value in issue_types


# ── IssueType 枚举 ────────────────────────────────────────────


class TestIssueTypeEnum:
    def test_enum_values(self):
        assert IssueType.MISSING_REQUIRED.value == "missing_required"
        assert IssueType.TYPE_MISMATCH.value == "type_mismatch"
        assert IssueType.UNIQUE_VIOLATION.value == "unique_violation"
        assert IssueType.ENUM_INVALID.value == "enum_invalid"
        assert IssueType.FORWARD_REF_BROKEN.value == "forward_ref_broken"
        assert IssueType.RANGE_OUTLIER.value == "range_outlier"
        assert IssueType.COL_NOT_FOUND.value == "col_not_found"
        assert IssueType.SCHEMA_MISSING.value == "schema_missing"

    def test_issue_to_dict(self):
        iss = Issue(col="成长率", issue_type=IssueType.TYPE_MISMATCH.value,
                   expected="float", suggestion="改值", value="abc")
        d = iss.to_dict()
        assert d == {"col": "成长率", "issue_type": "type_mismatch",
                     "expected": "float", "suggestion": "改值", "value": "abc",
                     "suggested_combo": ""}

    def test_validation_result_holds_issues(self):
        """ValidationResult.issues 字段承载 Issue 列表。"""
        vr = ValidationResult()
        assert vr.issues == []
        vr.issues.append(Issue(col="x", issue_type="type_mismatch"))
        assert len(vr.issues) == 1
        assert vr.ok is False


# ── FK 拓扑层（§4.2）─────────────────────────────────────────


def _producer(label="new_pet_id", fields=None):
    """producer intent：produces_label + extras[fields]。"""
    it = NLIntent(action="add", table_hint="pet", sheet_hint="Pet", raw="p",
                extras={"fields": fields or {"pet_id": 1}})
    it.produces_label = label
    it.extras["produces"] = label
    return it


def _consumer(consumes_label="new_pet_id", col="parent_id"):
    """consumer intent：fields 含 <consumes_label> 占位符。"""
    it = NLIntent(action="add", table_hint="pet2", sheet_hint="Pet", raw="c",
                extras={"fields": {col: f"<{consumes_label}>"}})
    return it


class TestFkLayerTopology:
    """validate_fk_layer 拓扑序推进 produced 集合。"""

    def test_forward_ref_resolved(self, monkeypatch):
        """producer 在 consumer 前 → consumes 占位符在 produced → 无 issue。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: [0, 1]))
        v = _make_validator()
        p = _producer()
        c = _consumer()
        out = v.validate_fk_layer([p, c])
        assert out[id(c)] == []
        assert out[id(p)] == []

    def test_forward_ref_broken(self, monkeypatch):
        """consumer 在 producer 前 → consumes 未在 produced → FORWARD_REF_BROKEN。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: [1, 0]))  # consumer(1) 先,producer(0) 后
        v = _make_validator()
        p = _producer()
        c = _consumer()
        out = v.validate_fk_layer([p, c])
        # consumer 先：parent_id=<new_pet_id> 未在 produced → FORWARD_REF_BROKEN
        c_issues = out[id(c)]
        assert len(c_issues) == 1
        assert c_issues[0].issue_type == IssueType.FORWARD_REF_BROKEN.value
        assert c_issues[0].col == "parent_id"

    def test_produces_label_populates_produced(self, monkeypatch):
        """producer 产出后 produced 集合填充,下游 consumer 可读。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: [0, 1]))
        v = _make_validator()
        p = _producer(label="new_quest_id")
        c = _consumer(consumes_label="new_quest_id")
        out = v.validate_fk_layer([p, c])
        # consumer 的 consumes 在 produced（producer 先产出）→ 无 issue
        assert out[id(c)] == []

    def test_empty_intents_returns_empty(self):
        v = _make_validator()
        assert v.validate_fk_layer([]) == {}

    def test_intent_no_fields_no_issues(self, monkeypatch):
        """intent 无 fields → 无 consumes 校验 → 无 issue。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: [0]))
        v = _make_validator()
        it = NLIntent(action="add", table_hint="pet", raw="x", extras={})
        out = v.validate_fk_layer([it])
        assert out[id(it)] == []

    def test_topo_order_failure_fallbacks_to_original_order(self, monkeypatch):
        """_topo_order 抛错 → 降级原序,不崩。"""
        def _boom(intents):
            raise RuntimeError("topo boom")
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(_boom))
        v = _make_validator()
        p = _producer()
        c = _consumer()
        out = v.validate_fk_layer([p, c])  # 降级原序 [0,1]:producer 先 → 无 issue
        assert out[id(c)] == []


# ── 交互反问接入（§4.5）──────────────────────────────────────


class TestAskUser:
    def test_no_callback_returns_continue(self):
        """无 _ask_callback（非交互场景/CI）→ {mode:continue}（不阻塞,不标 skipped）。"""
        v = _make_validator()
        assert v._ask_callback is None
        result = v.ask_user([{"col": "x", "issue_type": "type_mismatch"}])
        assert result == {"mode": "continue"}

    def test_calls_callback_with_tips(self):
        """mock _ask_callback 验证调用 + tips 传入。"""
        v = _make_validator()
        received = []

        def _cb(question):
            received.append(question)
            return {"mode": "nl", "text": "修正描述"}

        v.set_ask_callback(_cb)
        tips = [{"subtask_id": 1, "col": "成长率",
                 "issue_type": "type_mismatch", "expected": "float"}]
        result = v.ask_user(tips)
        assert result == {"mode": "nl", "text": "修正描述"}
        assert len(received) == 1
        assert received[0]["tips"] is tips
        assert "校验" in received[0]["reason"]

    def test_callback_returns_none_defaults_skip(self):
        """_ask_callback 返 None → 默认 skip。"""
        v = _make_validator()
        v.set_ask_callback(lambda q: None)
        result = v.ask_user([{"col": "x"}])
        assert result == {"mode": "skip"}

    def test_callback_exception_returns_skip(self):
        """_ask_callback 抛错 → 降级 skip（不崩）。"""
        v = _make_validator()

        def _boom(q):
            raise RuntimeError("ask boom")

        v.set_ask_callback(_boom)
        result = v.ask_user([{"col": "x"}])
        assert result == {"mode": "skip"}

    def test_set_ask_callback_injects(self):
        """set_ask_callback 注入 _ask_callback 属性。"""
        v = _make_validator()
        cb = lambda q: {"mode": "skip"}
        v.set_ask_callback(cb)
        assert v._ask_callback is cb


# ── 两段式整合（字段层 + FK 层 + ask_user）──────────────────


class TestTwoLayerIntegration:
    """字段层 + FK 层两段式整合,ask_user 反问（§4.1+4.2+4.5 最小闭环）。"""

    def test_field_and_fk_issues_merged_to_tips(self, monkeypatch):
        """字段层 issue + FK 层 issue 合并 → assemble_tips → ask_user。"""
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: [0, 1]))  # producer 先
        v = _make_validator()
        # producer: 字段层有 type_mismatch（pet_id 传字符串）
        p = _producer(fields={"pet_id": "非数字"})
        # consumer: FK 层 ok（producer 先产出）
        c = _consumer()
        sg = _schema_getter(["pet_id", "parent_id"], ["int", "int"])
        schemas = {id(p): (["pet_id"], ["int"]),
                   id(c): (["parent_id"], ["int"])}
        sg = lambda intent: schemas[id(intent)]
        field_map = v.validate_field_layer([p, c], schema_getter=sg)
        fk_map = v.validate_fk_layer([p, c])
        # 合并 issues（field + fk）
        merged = {}
        for sid in set(field_map) | set(fk_map):
            merged[sid] = field_map.get(sid, []) + fk_map.get(sid, [])
        tips = assemble_tips(merged)
        # producer: pet_id type_mismatch; consumer: 无 issue
        assert any(t["issue_type"] == IssueType.TYPE_MISMATCH.value for t in tips)
        assert len([t for t in tips if t["subtask_id"] == id(p)]) == 1
        assert len([t for t in tips if t["subtask_id"] == id(c)]) == 0

        # mock _ask_callback 验证 ask_user 发 tips
        received = []
        v.set_ask_callback(lambda q: received.append(q) or {"mode": "skip"})
        v.ask_user(tips)
        assert len(received) == 1
        assert received[0]["tips"] is tips


# ── 两段式整合 validate_two_layer（§4.1+4.2+4.5+4.6）────────


class TestValidateTwoLayer:
    """validate_two_layer 整合字段层+FK层+ask_user+重校。"""

    def _patch_topo(self, monkeypatch, order):
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(order)))

    def test_no_issues_ok(self, monkeypatch):
        """字段层 + FK 层全过 → ok=True + validation.ok=True 标记。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        it = _intent(fields={"pet_id": 1})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True
        assert out["tips"] == []
        assert it.validation is not None
        assert it.validation.ok is True
        assert it.validation.skipped is False

    def test_issues_collected_non_blocking_display(self, monkeypatch):
        """COL_NOT_FOUND 走批量 ask 卡；用户 skip → intent 标 skipped（放弃写盘），
        但 Step2 整体 ok=True（用户已决策，不硬阻断整批）。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v.set_ask_callback(lambda q: {"mode": "skip"})
        it = _intent(fields={"bad_col": 1})  # COL_NOT_FOUND
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True  # 用户 skip 后不硬阻断整批
        assert it.validation is not None
        assert it.validation.skipped is True  # 用户 skip → mark skipped 跳写盘

    def test_multi_intent_issues_displayed_all_proceed(self, monkeypatch):
        """无 cb：it1 COL_NOT_FOUND 自动解决（幻觉列删），不 skip；it2 clean。
        整体 ok=True（无失败收尾）。"""
        self._patch_topo(monkeypatch, [0, 1])
        v = _make_validator()
        it1 = _intent(fields={"bad_col": 1})  # COL_NOT_FOUND
        it2 = _intent(table="quest", fields={"quest_id": 2})  # clean
        schemas = {id(it1): (["pet_id"], ["int"]),
                   id(it2): (["quest_id"], ["int"])}
        sg = lambda intent: schemas[id(intent)]
        out = v.validate_two_layer([it1, it2], schema_getter=sg)
        assert out["ok"] is True
        assert "bad_col" not in it1.extras["fields"]  # 幻觉列自动删
        assert it1.validation.skipped is False  # 自动解决不 skip
        assert it2.validation.skipped is False  # clean 不 skip
        assert it2.validation.ok is True

    def test_type_mismatch_hard_issue_fix_applied(self, monkeypatch):
        """TYPE_MISMATCH 硬 issue → ask field fix（value）→ 应用 fix → ok=True。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v.set_ask_callback(lambda q: {"mode": "field", "value": 1})
        it = _intent(fields={"pet_id": "非数"})  # TYPE_MISMATCH
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True  # fix 应用后硬 issue 消除
        assert it.extras["fields"]["pet_id"] == 1  # fix 已应用
        assert len(out["tips"]) == 0  # 已解决，从 tips 移除

    def test_no_callback_non_blocking(self, monkeypatch):
        """无 cb：COL_NOT_FOUND 自动解决（幻觉列删），不 skip，ok=True。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()  # _ask_callback=None
        it = _intent(fields={"bad_col": 1})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True  # 自动解决，无失败收尾
        assert "bad_col" not in it.extras["fields"]  # 幻觉列自动删
        assert it.validation is not None
        assert it.validation.skipped is False  # 不 skip

    def test_validation_marked_on_all_intents(self, monkeypatch):
        """无 issue 时所有 intent 标 validation.ok=True（下游 ExecuteAgent 据此写盘）。"""
        self._patch_topo(monkeypatch, [0, 1])
        v = _make_validator()
        it1 = _intent(fields={"pet_id": 1})
        it2 = _intent(table="quest", fields={"quest_id": 2})
        schemas = {id(it1): (["pet_id"], ["int"]),
                   id(it2): (["quest_id"], ["int"])}
        sg = lambda intent: schemas[id(intent)]
        out = v.validate_two_layer([it1, it2], schema_getter=sg)
        assert out["ok"] is True
        assert it1.validation.ok is True
        assert it2.validation.ok is True


# ── ID 段校验（§4.1 ⑦ O4）──────────────────────────────────


class TestIdScopeFieldLayer:
    """O4：validate_field_layer 对 ID/编号 列调 id_scope 段校验。

    id_mgr 未加载（默认）→ validate_value 返 ok=True，不报 ID_OUT_OF_SCOPE。
    加载且越界 → 报 ID_OUT_OF_SCOPE issue。
    """

    def test_id_out_of_scope_emitted(self, monkeypatch):
        """ID 列值越界 id_mgr 预留段 → ID_OUT_OF_SCOPE issue。"""

        class _FakeV:
            _id_mgr_loaded = True

            def validate_value(self, module, value):
                if module == "pet.Pet" and int(value) == 999999999:
                    return False, "值 999999999 越界：模块 pet.Pet 预留段[1,100]"
                return True, ""

        monkeypatch.setattr("engine.id_scope.get_id_scope_validator",
                            lambda: _FakeV())
        v = _make_validator()
        it = _intent(fields={"pet_id": 999999999})  # ID 列越界值
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_field_layer([it], schema_getter=sg)
        issues = out[id(it)]
        assert any(i.issue_type == IssueType.ID_OUT_OF_SCOPE.value for i in issues)
        assert any(i.col == "pet_id" for i in issues)

    def test_id_in_scope_no_issue(self, monkeypatch):
        """ID 列值在段内 → 无 ID_OUT_OF_SCOPE issue。"""

        class _FakeV:
            _id_mgr_loaded = True

            def validate_value(self, module, value):
                return True, ""  # 在段内

        monkeypatch.setattr("engine.id_scope.get_id_scope_validator",
                            lambda: _FakeV())
        v = _make_validator()
        it = _intent(fields={"pet_id": 1})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert not any(i.issue_type == IssueType.ID_OUT_OF_SCOPE.value
                       for i in out[id(it)])

    def test_id_scope_skipped_when_id_mgr_unloaded(self, monkeypatch):
        """id_mgr 未加载 → 不报 ID_OUT_OF_SCOPE（validate_value 返 ok=True）。"""
        # 不 monkeypatch：真实单例 _id_mgr_loaded=False（测试环境未 load）
        v = _make_validator()
        it = _intent(fields={"pet_id": 999999999})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert not any(i.issue_type == IssueType.ID_OUT_OF_SCOPE.value
                       for i in out[id(it)])

    def test_non_id_col_not_checked(self, monkeypatch):
        """非 ID 列（如 名称）→ 不触发 id_scope 校验。"""
        calls = []

        class _FakeV:
            _id_mgr_loaded = True

            def validate_value(self, module, value):
                calls.append((module, value))
                return True, ""

        monkeypatch.setattr("engine.id_scope.get_id_scope_validator",
                            lambda: _FakeV())
        v = _make_validator()
        it = _intent(fields={"pet_id": 1, "名称": "朱雀"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        v.validate_field_layer([it], schema_getter=sg)
        assert len(calls) == 1  # 仅 pet_id 触发（名称 非 ID 列）
        assert calls[0][0] == "pet.Pet"


# ── 必填性校验（§4.1 ③）─────────────────────────────────────


class TestRequiredFields:
    """§4.1 ③ 必填性：required_fields.yaml 配置,缺失必填列 → MISSING_REQUIRED。"""

    def _patch_topo(self, monkeypatch, order):
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(order)))

    def test_required_field_missing(self, monkeypatch):
        """配置必填"名称" + fields 缺"名称" → MISSING_REQUIRED。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v._required_fields = {"pet": {"Pet": ["名称"]}}
        it = _intent(fields={"pet_id": 1})  # 缺"名称"
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        out = v.validate_field_layer([it], schema_getter=sg)
        issues = out[id(it)]
        assert any(i.issue_type == IssueType.MISSING_REQUIRED.value for i in issues)
        assert any(i.col == "名称" for i in issues)

    def test_required_field_present(self, monkeypatch):
        """配置必填"名称" + fields 含"名称" → 无 MISSING_REQUIRED。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v._required_fields = {"pet": {"Pet": ["名称"]}}
        it = _intent(fields={"pet_id": 1, "名称": "朱雀"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert not any(i.issue_type == IssueType.MISSING_REQUIRED.value
                       for i in out[id(it)])

    def test_required_fields_empty_config_skips(self, monkeypatch):
        """required_fields 空 dict → 跳过必填性校验（现状,配置未填充）。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v._required_fields = {}
        it = _intent(fields={"pet_id": 1})  # 即使缺必填,配置空也跳过
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert out[id(it)] == []

    def test_required_field_case_insensitive(self, monkeypatch):
        """必填列名匹配大小写不敏感（PET_ID vs pet_id）。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v._required_fields = {"pet": {"Pet": ["名称"]}}
        it = _intent(fields={"PET_ID": 1, "名称": "x"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        out = v.validate_field_layer([it], schema_getter=sg)
        assert not any(i.issue_type == IssueType.MISSING_REQUIRED.value
                       for i in out[id(it)])

    def test_required_field_multiple(self, monkeypatch):
        """多必填列 + 部分缺失 → 多 MISSING_REQUIRED issue。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v._required_fields = {"pet": {"Pet": ["名称", "pet_id", "成长率"]}}
        it = _intent(fields={"pet_id": 1})  # 缺"名称"+"成长率"
        sg = _schema_getter(["pet_id", "名称", "成长率"], ["int", "string", "float"])
        out = v.validate_field_layer([it], schema_getter=sg)
        issues = out[id(it)]
        missing = [i.col for i in issues
                   if i.issue_type == IssueType.MISSING_REQUIRED.value]
        assert "名称" in missing
        assert "成长率" in missing
        assert "pet_id" not in missing  # 已存在


# ── ⑤枚举白名单 / ④唯一性 / ⑥范围分布（§4.1 ④⑤⑥）────────


class TestFieldLayerEnumUniqueRange:
    """§4.1 ④⑤⑥：data_getter 注入 enum_set/existing_values/result_rows + 纯函数复用。"""

    def _patch_topo(self, monkeypatch, order):
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(order)))

    def test_enum_whitelist_violation(self, monkeypatch):
        """⑤ data_getter.enum_set + 值不在白名单 → ENUM_INVALID。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        it = _intent(fields={"pet_id": 1, "type": "未知类型"})
        sg = _schema_getter(["pet_id", "type"], ["int", "int"])
        data = {"enum_set": {"type": {"火", "水", "风"}}}
        out = v.validate_field_layer([it], schema_getter=sg,
                                     data_getter=lambda i: data)
        issues = out[id(it)]
        assert any(i.issue_type == IssueType.ENUM_INVALID.value for i in issues)
        assert any(i.col == "type" for i in issues)

    def test_enum_whitelist_value_in_set_no_issue(self, monkeypatch):
        """⑤ 值在 enum_set → 无 ENUM_INVALID。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        it = _intent(fields={"pet_id": 1, "type": "火"})
        sg = _schema_getter(["pet_id", "type"], ["int", "int"])
        data = {"enum_set": {"type": {"火", "水", "风"}}}
        out = v.validate_field_layer([it], schema_getter=sg,
                                     data_getter=lambda i: data)
        assert not any(i.issue_type == IssueType.ENUM_INVALID.value
                       for i in out[id(it)])

    def test_enum_placeholder_skipped(self, monkeypatch):
        """⑤ 占位符值软跳过（不报枚举错）。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        it = _intent(fields={"type": "<auto>"})
        sg = _schema_getter(["type"], ["int"])
        data = {"enum_set": {"type": {"火", "水"}}}
        out = v.validate_field_layer([it], schema_getter=sg,
                                     data_getter=lambda i: data)
        assert out[id(it)] == []

    def test_unique_violation(self, monkeypatch):
        """④ data_getter.existing_values + PK 值重复 → UNIQUE_VIOLATION。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        it = _intent(fields={"pet_id": 1, "名称": "朱雀"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        data = {"existing_values": {"pet_id": {1, 2}, "名称": {"朱雀", "白虎"}}}
        out = v.validate_field_layer([it], schema_getter=sg,
                                     data_getter=lambda i: data)
        issues = out[id(it)]
        assert any(i.issue_type == IssueType.UNIQUE_VIOLATION.value for i in issues)
        assert any(i.col == "pet_id" for i in issues)

    def test_unique_value_not_in_existing_no_issue(self, monkeypatch):
        """④ 值不在 existing_values → 无 UNIQUE_VIOLATION。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        it = _intent(fields={"pet_id": 1, "名称": "新宠物"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        data = {"existing_values": {"名称": {"朱雀", "白虎"}}}
        out = v.validate_field_layer([it], schema_getter=sg,
                                     data_getter=lambda i: data)
        assert not any(i.issue_type == IssueType.UNIQUE_VIOLATION.value
                       for i in out[id(it)])

    def test_range_outlier_modify_only(self, monkeypatch):
        """⑥ modify + run_semantic_gate 返 issue → RANGE_OUTLIER。"""
        self._patch_topo(monkeypatch, [0])
        monkeypatch.setattr(
            "agent.excel.semantic_gate.run_semantic_gate",
            lambda *a, **k: [{"column": "成长率", "reason": "超出范围",
                              "value": 999, "suggested_fix": "改值"}])
        # _check_enum_whitelist fallback 不触发（enum_set 覆盖成长率且值在白名单）
        monkeypatch.setattr(
            "agent.excel.semantic_gate._check_enum_whitelist",
            lambda *a, **k: None)
        v = _make_validator()
        it = NLIntent(action="modify", table_hint="pet", sheet_hint="Pet",
                    raw="m", extras={"fields": {"成长率": 999}})
        sg = _schema_getter(["成长率"], ["float"])
        data = {"path": "pet.xlsx", "stem": "pet", "sheet": "Pet",
                "vc": {"成长率": {"min": 0, "max": 10}},
                "result_rows": [{"成长率": 1.0}],
                "cli": object(), "enum_set": {"成长率": {999}}}
        out = v.validate_field_layer([it], schema_getter=sg,
                                     data_getter=lambda i: data)
        issues = out[id(it)]
        assert any(i.issue_type == IssueType.RANGE_OUTLIER.value for i in issues)

    def test_range_outlier_skipped_for_add(self, monkeypatch):
        """⑥ add 动作不跑范围分布（§4.1 ⑥ only for modify）。"""
        self._patch_topo(monkeypatch, [0])
        calls = [0]

        def _spy_sg(*a, **k):
            calls[0] += 1
            return []

        monkeypatch.setattr("agent.excel.semantic_gate.run_semantic_gate", _spy_sg)
        monkeypatch.setattr("agent.excel.semantic_gate._check_enum_whitelist",
                            lambda *a, **k: None)
        v = _make_validator()
        it = _intent(fields={"成长率": 999})  # action=add
        sg = _schema_getter(["成长率"], ["float"])
        data = {"path": "pet.xlsx", "vc": {"成长率": {"min": 0, "max": 10}},
                "result_rows": [{"成长率": 1.0}], "cli": object()}
        v.validate_field_layer([it], schema_getter=sg,
                               data_getter=lambda i: data)
        assert calls[0] == 0  # add 不调 run_semantic_gate

    def test_data_getter_none_skips_456(self, monkeypatch):
        """无 data_getter → ④⑤⑥ 跳过（仅 ①②③）。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        it = _intent(fields={"pet_id": 1})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_field_layer([it], schema_getter=sg,
                                     data_getter=None)
        assert out[id(it)] == []  # 无 ④⑤⑥ 数据,仅 ①② 过


# ── P9 回归：_suppress_over_produce 不误杀同表多行 op ──────────


class TestSuppressOverProduceMultiRow:
    """P9：原实现按 (stem,sheet) 一刀切去重，会误杀同表多行 op（如
    BuildingInteract 的 idle/collect 两条不同 state 行——它们是引用行，无
    produces）。改为：仅"同 (stem,sheet) 且都声明 produces"才判过产抑制
    （producer 一表一 op 契约）；无 produces 的引用/明细行允许多行。
    """

    def test_same_sheet_reference_rows_kept(self):
        """同表同 sheet、无 produces 的引用行（idle/collect）→ 两条都保留."""
        v = _make_validator()
        a = _intent(table="residence_building", sheet="BuildingInteract",
                    fields={"building_type": 12, "state_id": "idle",
                            "character_montage": "DongfuIdle.graph",
                            "building_state": 0, "soft_stop": True})
        b = _intent(table="residence_building", sheet="BuildingInteract",
                    fields={"building_type": 12, "state_id": "collect",
                            "character_montage": "DongfuCollect.graph",
                            "building_state": 1, "soft_stop": False})
        # 无 produces → 不参与去重
        a.extras["produces"] = ""
        b.extras["produces"] = ""
        dropped = v._suppress_over_produce([a, b])
        assert dropped == 0
        assert len([a, b]) == 2

    def test_same_sheet_producer_ops_suppressed(self):
        """同表同 sheet 且都声明 produces → 抑制第二条（LLM 过产）."""
        v = _make_validator()
        a = _intent(table="mail", sheet="MailTemplate",
                    fields={"标题": "礼包A"})
        b = _intent(table="mail", sheet="MailTemplate",
                    fields={"标题": "礼包B"})
        a.extras["produces"] = "new_mail_template_id"
        b.extras["produces"] = "new_mail_template_id2"
        intents = [a, b]
        dropped = v._suppress_over_produce(intents)
        assert dropped == 1
        assert len(intents) == 1
        assert intents[0].extras["fields"]["标题"] == "礼包A"  # 保留首个

    def test_different_sheet_both_kept(self):
        """不同 sheet（BuildingType vs BuildingInteract）→ 两条都保留."""
        v = _make_validator()
        a = _intent(table="residence_building", sheet="BuildingType",
                    fields={"building_type": 12, "name": "聚灵塔"})
        b = _intent(table="residence_building", sheet="BuildingInteract",
                    fields={"building_type": 12, "state_id": "idle"})
        a.extras["produces"] = "new_building_type_id"
        b.extras["produces"] = ""
        dropped = v._suppress_over_produce([a, b])
        assert dropped == 0


# ── O3 回归：validate_two_layer 非阻断纯展示 ──────────────────


class TestValidateTwoLayerNonBlocking:
    """O3：validate_two_layer 降级为纯展示——不 ask、不 skip、不阻断，
    全 intent 标 validation.ok=True 继续写盘。修复交写后 verify_repair_loop。
    """

    def test_issues_collected_but_ok_true(self):
        """无 cb：COL_NOT_FOUND 自动解决（幻觉列删），不 skip，ok=True。"""
        v = _make_validator()
        v._ask_callback = None  # 无 callback，确保不进 ask 路径
        it = _intent(table="pet", sheet="Pet",
                     fields={"pet_id": 1, "魔法值": 999})
        sg = _schema_getter(["pet_id", "成长率"], ["int", "float"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True  # 自动解决，无失败收尾
        assert "魔法值" not in it.extras["fields"]  # 幻觉列自动删
        assert getattr(it, "validation", None) is not None
        assert getattr(it.validation, "skipped", False) is False  # 不 skip


class TestBusinessRequiredPack3:
    """Pack 3：业务必填列 heuristic 写前移到 Step2。

    bench 实证 #4 写 ItemBase 行872 内容={1: 29012, 2: '冰魄碎片'}（仅 2 列），
    agent.py:4561 _check_missing_required_after_add 写后才发现缺"道具描述"列 →
    step4 retro 之后 标失败 + 行已落盘半成品。改前移 Step2 validate_field_layer
    检测 + 直接标 intent.validation.skipped=True 让 Step3 跳写盘。
    """

    def test_business_required_missing_marks_skipped(self):
        """业务必填启发式已停用：即使指令含引号、漏产 名称/描述 类列，也不报缺、不 skip。

        §约束收缩（用户原则）：Step2 只强制主键列，其余列均可为空。
        """
        v = _make_validator()
        v._pk_cols_cache = {}
        it = _intent(table="item", sheet="ItemBase",
                     fields={"物品编号": 29012, "名称": "冰魄之戒"})
        it.raw = "「这个戒指在 item.xlsx」"
        sg = _schema_getter(
            ["物品编号", "名称", "道具描述", "道具备注"],
            ["int", "string", "string", "string"])
        out = v.validate_field_layer([it], schema_getter=sg, data_getter=lambda x: {})
        issues = out.get(id(it)) or []
        assert not any(getattr(i, "issue_type", "") == IssueType.MISSING_REQUIRED.value
                       for i in issues), "业务必填启发式已停用，不应再报缺"
        assert getattr(getattr(it, "validation", None), "skipped", False) is False

    def test_business_required_not_quoted_no_check(self):
        """指令无引号（用户未显式给名称/描述值）→ 不触发 heuristic check（豁免）。"""
        v = _make_validator()
        v._pk_cols_cache = {}
        it = _intent(table="item", sheet="ItemBase",
                     fields={"物品编号": 29012, "名称": "冰魄之戒"})
        it.raw = "冰封王座首通挑战"  # 无引号/中文写法
        sg = _schema_getter(
            ["物品编号", "名称", "道具描述", "道具备注"],
            ["int", "string", "string", "string"])
        out = v.validate_field_layer([it], schema_getter=sg, data_getter=lambda x: {})
        issues = out.get(id(it)) or []
        assert not any(
            getattr(i, "issue_type", "") == IssueType.MISSING_REQUIRED.value
            and "描述" in getattr(i, "col", "")
            for i in issues)
        # skipped 不变（None / False）
        _sk = getattr(getattr(it, "validation", None), "skipped", False)
        assert _sk is False

    def test_business_required_all_provided_no_issue(self):
        """指令含引号 + LLM 给了所有 名称/描述 类列 → 无 issue。"""
        v = _make_validator()
        v._pk_cols_cache = {}
        it = _intent(table="item", sheet="ItemBase",
                     fields={"物品编号": 29012, "名称": "冰魄之戒", "道具描述": "冰封王座掉落"})
        it.raw = "「这个戒指在 item.xlsx」"
        sg = _schema_getter(
            ["物品编号", "名称", "道具描述", "道具备注"],
            ["int", "string", "string", "string"])
        out = v.validate_field_layer([it], schema_getter=sg, data_getter=lambda x: {})
        issues = out.get(id(it)) or []
        assert not any(
            getattr(i, "issue_type", "") == IssueType.MISSING_REQUIRED.value
            for i in issues)
        assert getattr(it.validation, "skipped", False) is False

    def test_modify_action_not_checked(self):
        """action=modify 不触发 heuristic check（只有 add 才补必填列场景）。"""
        v = _make_validator()
        v._pk_cols_cache = {}
        it = NLIntent(action="modify", table_hint="item", sheet_hint="ItemBase",
                      raw="「改戒指描述」", extras={"fields": {"物品编号": 29012}})
        sg = _schema_getter(
            ["物品编号", "名称", "道具描述"],
            ["int", "string", "string"])
        out = v.validate_field_layer([it], schema_getter=sg, data_getter=lambda x: {})
        issues = out.get(id(it)) or []
        assert not any(
            getattr(i, "issue_type", "") == IssueType.MISSING_REQUIRED.value
            for i in issues)

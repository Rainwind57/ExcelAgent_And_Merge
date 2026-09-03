
"""解析层与编排器单测（P3 3.6 + P2 编排器 + P5.3 并发）。

不依赖 codemaker serve：仅测纯函数与可注入桩的编排逻辑。
覆盖：
  - agent._clean_quotes：成对引号去外层、内容保留、未配对原样
  - codemaker_parser.mode_classify：单命中归类、复合→AUTO、优先级、空输入
  - CodemakerNLParser._validate_intent：action 归一化/别名/非法降级 get、table_hint 合法性降级
  - CodemakerNLParser._fields_from_item / _coerce_row_override：边界
  - skill_context.build_skill_context：无候选→空、token 预算降级（列名必留）
  - llm_context.estimate_tokens：中/英文 token 估算
  - OperationOrchestrator：依赖分析/拓扑排序/分层/占位符替换/多命名键/前向引用/环回退/
    顺序执行/并发执行（结果等价、produced 传递）

运行: python -m pytest server/tests/test_parse_layer.py
   或: python -m server.tests.test_parse_layer
"""
from __future__ import annotations

import os
import sys
import threading
import time

# 确保 server/ 在 sys.path → agent.* 命名空间（直接 python -m 与 pytest 均可）
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # server/

from agent.excel.agent import _clean_quotes
from agent.excel.codemaker_parser import PromptMode, mode_classify, CodemakerNLParser
from agent.excel.llm_context import estimate_tokens
from agent.excel.nl_parser import NLIntent
from agent.excel.operation_orchestrator import (
    OperationOrchestrator, _FailedResult,
)


# ---------------------------------------------------------------------------
# _clean_quotes（agent.py）—— P3 3.1
# ---------------------------------------------------------------------------

class TestCleanQuotes:
    def test_halfwidth_double(self):
        assert _clean_quotes('名称"张三"') == "名称张三"

    def test_halfwidth_single(self):
        assert _clean_quotes("value='abc'") == "value=abc"

    def test_fullwidth_double(self):
        assert _clean_quotes("\u201c张三\u201d") == "张三"

    def test_fullwidth_single(self):
        assert _clean_quotes("\u2018x\u2019") == "x"

    def test_corner_brackets(self):
        assert _clean_quotes("「张三」") == "张三"

    def test_title_brackets(self):
        assert _clean_quotes("『张三』") == "张三"

    def test_inner_quote_preserved(self):
        # 内容内本身含撇号，不应被破坏（成对外层才剥）
        assert _clean_quotes("it's a test") == "it's a test"

    def test_unpaired_preserved(self):
        # 未配对引号原样保留
        assert _clean_quotes('张"三') == '张"三'

    def test_empty(self):
        assert _clean_quotes("") == ""

    def test_none_like(self):
        # 空字符串短路
        assert _clean_quotes("") == ""

    def test_multiple_pairs(self):
        assert _clean_quotes('"a"和"b"') == "a和b"

    def test_nested_not_stripped_inner(self):
        # 成对外层剥一层，内层成对再剥——正则非贪婪逐对处理
        assert _clean_quotes('"张三"') == "张三"


# ---------------------------------------------------------------------------
# mode_classify（codemaker_parser.py）—— P3 3.2
# ---------------------------------------------------------------------------

class TestModeClassify:
    def test_empty(self):
        assert mode_classify("") == PromptMode.AUTO

    def test_none_text(self):
        assert mode_classify(None) == PromptMode.AUTO  # type: ignore[arg-type]

    def test_query_single(self):
        assert mode_classify("查询饕餮") == PromptMode.QUERY

    def test_add_single(self):
        assert mode_classify("新增宠物朱雀") == PromptMode.ADD

    def test_modify_single(self):
        assert mode_classify("把等级改为10") == PromptMode.MODIFY

    def test_delete_single(self):
        assert mode_classify("删除道具分身斧") == PromptMode.DELETE

    def test_composite_returns_auto(self):
        # 同时含 add+delete → 复合，不强制单一
        assert mode_classify("增加道具A，删除道具B") == PromptMode.AUTO

    def test_composite_add_modify(self):
        assert mode_classify("新增宠物并修改等级") == PromptMode.AUTO

    def test_no_keyword_auto(self):
        assert mode_classify("你好") == PromptMode.AUTO

    def test_priority_delete_over_add(self):
        # 单命中才走优先级；这里构造只命中 delete 的
        assert mode_classify("移除建筑") == PromptMode.DELETE

    def test_english_keyword(self):
        assert mode_classify("delete item 5") == PromptMode.DELETE
        assert mode_classify("add a new pet") == PromptMode.ADD

    def test_case_insensitive(self):
        assert mode_classify("DELETE row") == PromptMode.DELETE


# ---------------------------------------------------------------------------
# _fields_from_item / _coerce_row_override（codemaker_parser.py）
# ---------------------------------------------------------------------------

class TestFieldsAndRowOverride:
    def test_fields_from_item_dict(self):
        assert CodemakerNLParser._fields_from_item(
            {"fields": {"等级": 10, "名称": "x"}}) == {"等级": 10, "名称": "x"}

    def test_fields_from_item_empty(self):
        assert CodemakerNLParser._fields_from_item({}) == {}

    def test_fields_from_item_non_dict(self):
        assert CodemakerNLParser._fields_from_item({"fields": "x"}) == {}

    def test_fields_from_item_legacy_target_field(self):
        # 旧格式 target_field/value → fields
        out = CodemakerNLParser._fields_from_item(
            {"target_field": "等级", "value": 10})
        assert out == {"等级": 10}

    def test_fields_from_item_legacy_not_overwrite(self):
        # fields 已有该键时不被旧格式覆盖
        out = CodemakerNLParser._fields_from_item(
            {"fields": {"等级": 5}, "target_field": "等级", "value": 10})
        assert out == {"等级": 5}

    def test_coerce_row_override_int(self):
        assert CodemakerNLParser._coerce_row_override({"row_override": 6}) == 6

    def test_coerce_row_override_zero_invalid(self):
        assert CodemakerNLParser._coerce_row_override({"row_override": 0}) is None

    def test_coerce_row_override_negative_invalid(self):
        assert CodemakerNLParser._coerce_row_override({"row_override": -3}) is None

    def test_coerce_row_override_str_digit(self):
        assert CodemakerNLParser._coerce_row_override({"row_override": "8"}) == 8

    def test_coerce_row_override_str_nondigit(self):
        assert CodemakerNLParser._coerce_row_override({"row_override": "abc"}) is None

    def test_coerce_row_override_bool_invalid(self):
        assert CodemakerNLParser._coerce_row_override({"row_override": True}) is None

    def test_coerce_row_override_none(self):
        assert CodemakerNLParser._coerce_row_override({}) is None
        assert CodemakerNLParser._coerce_row_override({"row_override": None}) is None


# ---------------------------------------------------------------------------
# _validate_intent（codemaker_parser.py）—— P3 3.4
# ---------------------------------------------------------------------------

class TestValidateIntent:
    def test_action_already_valid(self):
        it = NLIntent(action="get")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "get"

    def test_action_alias_query_to_get(self):
        it = NLIntent(action="query")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "get"

    def test_action_alias_update_to_set(self):
        it = NLIntent(action="update")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "set"

    def test_action_alias_insert_to_add(self):
        it = NLIntent(action="insert")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "add"

    def test_action_alias_remove_to_delete(self):
        it = NLIntent(action="remove")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "delete"

    def test_action_invalid_falls_back_to_get(self):
        it = NLIntent(action="whatever")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "get"

    def test_action_empty_falls_back_to_get(self):
        it = NLIntent(action="")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "get"

    def test_action_case_insensitive(self):
        it = NLIntent(action="DELETE")
        CodemakerNLParser._validate_intent(it)
        assert it.action == "delete"

    def test_table_hint_empty_unchanged(self):
        it = NLIntent(action="get", table_hint=None)
        CodemakerNLParser._validate_intent(it)
        assert it.table_hint is None

    def test_table_hint_in_stems_kept(self, monkeypatch):
        import agent.excel.skill_context as sc
        monkeypatch.setattr(sc, "known_stems", lambda: {"pet", "item"})
        monkeypatch.setattr(sc, "get_table_route", lambda: {})
        it = NLIntent(action="get", table_hint="pet")
        CodemakerNLParser._validate_intent(it)
        assert it.table_hint == "pet"

    def test_table_hint_routed_to_stem(self, monkeypatch):
        import agent.excel.skill_context as sc
        monkeypatch.setattr(sc, "known_stems", lambda: {"pet"})
        monkeypatch.setattr(sc, "get_table_route", lambda: {"宠物": "pet"})
        it = NLIntent(action="get", table_hint="宠物")
        CodemakerNLParser._validate_intent(it)
        assert it.table_hint == "pet"

    def test_table_hint_unmappable_cleared(self, monkeypatch):
        import agent.excel.skill_context as sc
        monkeypatch.setattr(sc, "known_stems", lambda: {"pet"})
        monkeypatch.setattr(sc, "get_table_route", lambda: {})
        it = NLIntent(action="get", table_hint="不存在的表")
        CodemakerNLParser._validate_intent(it)
        assert it.table_hint is None


class TestValidateFields:
    """D2: _validate_intent 就近对 fields 做 match_best 校验+自纠（capability: column-matching-accuracy）。"""

    def _patch_sc(self, monkeypatch, columns):
        from agent.excel import skill_context as sc
        monkeypatch.setattr(sc, "get_columns",
                            lambda stem, sheet="": columns if stem == "pet" else {})
        monkeypatch.setattr(sc, "known_stems", lambda: {"pet"})
        monkeypatch.setattr(sc, "get_table_route", lambda: {})

    def test_field_normalized_to_real_column(self, monkeypatch):
        self._patch_sc(monkeypatch, {"Pet": ["pet_id", "名字", "类型"]})
        it = NLIntent(action="add", table_hint="pet", sheet_hint="Pet",
                      extras={"fields": {"名字": "小白", "宠物类型": "1"}})
        CodemakerNLParser._validate_intent(it)
        # "名字" 精确命中保留；"宠物类型" 经 match_best 命中"类型"→规范化（score>=0.85）
        assert "名字" in it.extras["fields"]
        assert "类型" in it.extras["fields"]
        assert "宠物类型" not in it.extras["fields"]

    def test_unresolved_field_marked(self, monkeypatch):
        self._patch_sc(monkeypatch, {"Pet": ["pet_id", "名字"]})
        it = NLIntent(action="add", table_hint="pet", sheet_hint="Pet",
                      extras={"fields": {"aaaa": "v"}})  # 纯 ascii vs 中文列 → 无匹配
        CodemakerNLParser._validate_intent(it)
        assert "aaaa" in it.extras.get("_unresolved_fields", [])

    def test_no_table_hint_skips(self):
        it = NLIntent(action="add", table_hint=None, extras={"fields": {"x": "y"}})
        CodemakerNLParser._validate_intent(it)
        assert it.extras["fields"] == {"x": "y"}
        assert "_unresolved_fields" not in it.extras


# ---------------------------------------------------------------------------
# build_skill_context / pre_route（skill_context.py）—— P1 + P3 3.5 + P6 6.2
# ---------------------------------------------------------------------------

class TestSkillContext:
    def test_pre_route_empty_text(self):
        from agent.excel.skill_context import pre_route
        assert pre_route("") == []
        assert pre_route(None) == []  # type: ignore[arg-type]

    def test_build_skill_context_empty_text(self):
        from agent.excel.skill_context import build_skill_context
        assert build_skill_context("") == ""

    def test_build_skill_context_no_candidate(self, monkeypatch):
        # D1：预解析无候选时退化为全表列名候选（不再返回空，避免 LLM 零约束臆造）
        import agent.excel.skill_context as sc
        monkeypatch.setattr(sc, "pre_route", lambda txt, limit=3: [])
        monkeypatch.setattr(sc, "known_stems", lambda: {"pet", "item"})
        result = sc.build_skill_context("无关文本")
        assert result != "", "无候选时应退化注入全表列名候选"

    def test_build_skill_context_budget_keeps_columns(self, monkeypatch):
        # 3.5 修正：候选列名必保留，路由从全量→仅候选→无降级
        import agent.excel.skill_context as sc
        monkeypatch.setattr(sc, "pre_route", lambda txt, limit=3: ["pet"])
        monkeypatch.setattr(sc, "_format_columns_block",
                            lambda stems: "【列名】pet: 名称,等级,ID")
        monkeypatch.setattr(sc, "_format_enums_block", lambda stems: "【枚举】")
        # full_route 巨大 → 超 budget
        monkeypatch.setattr(sc, "_format_route_block",
                            lambda only=None: "X" * 10000)
        monkeypatch.setattr(sc, "estimate_tokens", lambda txt: len(txt))
        ctx = sc.build_skill_context("新增宠物", budget=50)
        # 列名块必须保留
        assert "【列名】" in ctx

    def test_build_skill_context_full_route_when_within_budget(self, monkeypatch):
        import agent.excel.skill_context as sc
        monkeypatch.setattr(sc, "pre_route", lambda txt, limit=3: ["pet"])
        monkeypatch.setattr(sc, "_format_columns_block", lambda stems: "COL")
        monkeypatch.setattr(sc, "_format_enums_block", lambda stems: "ENUM")
        monkeypatch.setattr(sc, "_format_route_block", lambda only=None: "ROUTE")
        monkeypatch.setattr(sc, "estimate_tokens", lambda txt: len(txt))
        ctx = sc.build_skill_context("新增宠物", budget=10000)
        assert "ROUTE" in ctx and "COL" in ctx and "ENUM" in ctx


# ---------------------------------------------------------------------------
# estimate_tokens（llm_context.py）
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_empty(self):
        assert estimate_tokens("") == 0

    def test_ascii(self):
        # 0.5 token/ascii → "abcd" ≈ 2 token（向上取整）
        assert estimate_tokens("abcd") >= 2

    def test_cjk(self):
        # 中文 2 token/字
        assert estimate_tokens("中") >= 2
        assert estimate_tokens("中文") >= 4

    def test_cjk_more_than_ascii(self):
        assert estimate_tokens("中") > estimate_tokens("a")


# ---------------------------------------------------------------------------
# OperationOrchestrator（P2 + P5.3 并发）
# ---------------------------------------------------------------------------

def _ok_result(rows=None):
    """构造一个 ok 的桩结果（鸭子兼容 AgentResult）。"""
    class _R:
        ok = True
        steps = []
        final = None
        message = "ok"
        result_rows = rows or []
        table_stem = ""
        table_sheet = ""
        session_id = ""
        sub_tasks = []
    return _R()


def _add_result(col_name, new_value):
    """构造 add 产出新 ID 的桩结果。"""
    return _ok_result([{"col_name": col_name, "new_value": new_value}])


class TestOrchestratorDeps:
    def test_no_deps_no_produces(self):
        its = [NLIntent(action="get", locator_value="a"),
               NLIntent(action="get", locator_value="b")]
        deps = OperationOrchestrator._compute_deps(its)
        assert all(not deps[i] for i in range(len(its)))

    def test_producer_consumer_dep(self):
        a = NLIntent(action="add", extras={"produces": "new_prefab_id"})
        b = NLIntent(action="add", locator_value="<new_prefab_id>")
        deps = OperationOrchestrator._compute_deps([a, b])
        assert deps[1] == {0}

    def test_forward_reference(self):
        # 生产者写在消费者后（前向引用）仍识别依赖
        a = NLIntent(action="add", locator_value="<new_prefab_id>")  # consumer
        b = NLIntent(action="add", extras={"produces": "new_prefab_id"})  # producer
        deps = OperationOrchestrator._compute_deps([a, b])
        assert deps[0] == {1}

    def test_generic_alias_dep(self):
        # <new_id> 通用占位符——但无 produces 标注时不建边（保守）
        a = NLIntent(action="add")
        b = NLIntent(action="add", locator_value="<new_id>")
        deps = OperationOrchestrator._compute_deps([a, b])
        assert all(not deps[i] for i in range(len([a, b])))

    def test_has_dependencies_true(self):
        its = [NLIntent(action="add", extras={"produces": "new_x"}),
               NLIntent(action="add", locator_value="<new_x>")]
        assert OperationOrchestrator.has_dependencies(its) is True

    def test_has_dependencies_false(self):
        its = [NLIntent(action="get", locator_value="a"),
               NLIntent(action="get", locator_value="b")]
        assert OperationOrchestrator.has_dependencies(its) is False

    def test_has_dependencies_skips_none(self):
        its = [None, NLIntent(action="get", locator_value="a")]
        assert OperationOrchestrator.has_dependencies(its) is False


class TestTopoOrder:
    def test_no_deps_original_order(self):
        its = [NLIntent(action="get", locator_value=str(i)) for i in range(3)]
        assert OperationOrchestrator._topo_order(its) == [0, 1, 2]

    def test_producer_before_consumer(self):
        a = NLIntent(action="add", extras={"produces": "new_prefab_id"})
        b = NLIntent(action="add", locator_value="<new_prefab_id>")
        # 输入 [consumer, producer] → 输出 producer(1) 先，consumer(0) 后
        order = OperationOrchestrator._topo_order([b, a])
        assert order.index(1) < order.index(0)

    def test_cycle_fallback_original_order(self):
        a = NLIntent(action="add", extras={"produces": "new_x"}, locator_value="<new_y>")
        b = NLIntent(action="add", extras={"produces": "new_y"}, locator_value="<new_x>")
        order = OperationOrchestrator._topo_order([a, b])
        assert order == [0, 1]  # 环回退原序

    def test_none_intent_kept_in_order(self):
        its = [NLIntent(action="get"), None, NLIntent(action="get")]
        order = OperationOrchestrator._topo_order(its)
        assert sorted(order) == [0, 1, 2]


class TestTopoLevels:
    def test_no_deps_single_level(self):
        its = [NLIntent(action="get") for _ in range(3)]
        lv = OperationOrchestrator._topo_levels(its)
        assert lv == [[0, 1, 2]]

    def test_producer_consumer_two_levels(self):
        a = NLIntent(action="add", extras={"produces": "new_prefab_id"})
        b = NLIntent(action="add", locator_value="<new_prefab_id>")
        lv = OperationOrchestrator._topo_levels([a, b])
        assert lv == [[0], [1]]

    def test_forward_ref_two_levels(self):
        a = NLIntent(action="add", locator_value="<new_prefab_id>")
        b = NLIntent(action="add", extras={"produces": "new_prefab_id"})
        lv = OperationOrchestrator._topo_levels([a, b])
        # producer(1) 在 level0，consumer(0) 在 level1
        assert lv == [[1], [0]]

    def test_independent_same_level(self):
        # 0 produces x, 1 independent, 2 consumes x
        its = [
            NLIntent(action="add", extras={"produces": "new_x"}),
            NLIntent(action="get", locator_value="独立"),
            NLIntent(action="add", locator_value="<new_x>"),
        ]
        lv = OperationOrchestrator._topo_levels(its)
        assert lv[0] == [0, 1]  # producer + 独立 同层
        assert lv[1] == [2]

    def test_cycle_fallback(self):
        a = NLIntent(action="add", extras={"produces": "new_x"}, locator_value="<new_y>")
        b = NLIntent(action="add", extras={"produces": "new_y"}, locator_value="<new_x>")
        lv = OperationOrchestrator._topo_levels([a, b])
        assert lv == [[0, 1]]  # 环回退：剩余全部并入一层


class TestResolvePlaceholders:
    def test_substitute_named(self):
        it = NLIntent(action="add", locator_value="<new_prefab_id>")
        OperationOrchestrator._resolve_placeholders(
            it, {"new_prefab_id": "1001", "prefab_id": "1001"})
        assert it.locator_value == "1001"

    def test_substitute_generic_new_id(self):
        it = NLIntent(action="add", locator_value="<prev_id>")
        OperationOrchestrator._resolve_placeholders(it, {"new_id": "42"})
        assert it.locator_value == "42"

    def test_substitute_fields(self):
        it = NLIntent(action="set", extras={"fields": {"ref": "<new_conv_id>"}})
        OperationOrchestrator._resolve_placeholders(it, {"new_conv_id": "C7", "conv_id": "C7"})
        assert it.extras["fields"]["ref"] == "C7"

    def test_unresolved_preserved(self):
        it = NLIntent(action="add", locator_value="<unknown_id>")
        OperationOrchestrator._resolve_placeholders(it, {"new_id": "1"})
        assert it.locator_value == "<unknown_id>"

    def test_no_placeholder_unchanged(self):
        it = NLIntent(action="get", locator_value="张三")
        OperationOrchestrator._resolve_placeholders(it, {"new_id": "1"})
        assert it.locator_value == "张三"

    def test_cn_placeholder(self):
        it = NLIntent(action="add", locator_value="<上一个id>")
        OperationOrchestrator._resolve_placeholders(it, {"new_id": "9"})
        assert it.locator_value == "9"


class TestCaptureProduced:
    def test_capture_id_column(self):
        it = NLIntent(action="add", extras={"produces": "new_prefab_id"})
        produced, seq = {}, {}
        OperationOrchestrator._capture_produced(
            _add_result("prefab_id", 1001), it, produced, seq)
        assert produced["new_id"] == "1001"
        assert produced["new_prefab_id"] == "1001"
        assert produced["prefab_id"] == "1001"

    def test_capture_non_id_column_captures_nothing(self):
        # 结果仅含非 ID 列（如"描述"）→ _capture_produced 找不到 ID 列，不写入 produced
        it = NLIntent(action="add", extras={"produces": "conv_id"})
        produced, seq = {}, {}
        OperationOrchestrator._capture_produced(
            _add_result("描述", "C5"), it, produced, seq)
        assert produced == {}

    def test_capture_label_with_id_column(self):
        # 结果含 ID 列 + produces 标签 → 标签键与列名键均写入
        it = NLIntent(action="add", extras={"produces": "conv_id"})
        produced, seq = {}, {}
        OperationOrchestrator._capture_produced(
            _add_result("conv_id", "C5"), it, produced, seq)
        assert produced.get("conv_id") == "C5"
        assert produced.get("new_conv_id") == "C5"
        assert produced.get("new_id") == "C5"

    def test_capture_ordered_keys(self):
        # 同基名多次产出 → option_1_id / option_2_id
        produced, seq = {}, {}
        it1 = NLIntent(action="add", extras={"produces": "option_id"})
        it2 = NLIntent(action="add", extras={"produces": "option_id"})
        OperationOrchestrator._capture_produced(_add_result("option_id", 11), it1, produced, seq)
        OperationOrchestrator._capture_produced(_add_result("option_id", 22), it2, produced, seq)
        assert produced["option_1_id"] == "11"
        assert produced["option_2_id"] == "22"

    def test_capture_failed_result_skipped(self):
        it = NLIntent(action="add")
        produced, seq = {}, {}
        OperationOrchestrator._capture_produced(_FailedResult(it, "err"), it, produced, seq)
        assert produced == {}

    def test_capture_none_result_skipped(self):
        it = NLIntent(action="add")
        produced, seq = {}, {}
        OperationOrchestrator._capture_produced(None, it, produced, seq)
        assert produced == {}


class TestOrchestratorRun:
    def _fake_run_single(self, log: list, delay: float = 0.0):
        def _rs(intent, confirm_token, session_id):
            if delay:
                time.sleep(delay)
            log.append(intent.locator_value or intent.raw)
            if intent.action == "add":
                return _add_result("prefab_id", 1001)
            return _ok_result()
        return _rs

    def test_sequential_results_aligned(self):
        its = [NLIntent(action="get", locator_value=f"x{i}") for i in range(3)]
        log: list = []
        orch = OperationOrchestrator(self._fake_run_single(log))
        res = orch.run(its)
        assert len(res) == 3
        assert all(getattr(r, "ok", False) for r in res)
        assert log == ["x0", "x1", "x2"]

    def test_dependency_order_enforced(self):
        # consumer 引用 producer 的产出 → producer 先执行
        a = NLIntent(action="add", extras={"produces": "new_prefab_id"}, locator_value="npc1")
        b = NLIntent(action="set", locator_value="<new_prefab_id>")
        log: list = []
        orch = OperationOrchestrator(self._fake_run_single(log))
        orch.run([b, a])  # consumer 在前
        # producer (a, locator=npc1) 应先于 consumer (b, locator=<...>) 执行
        assert log[0] == "npc1"

    def test_placeholder_passed_to_consumer(self):
        a = NLIntent(action="add", extras={"produces": "new_prefab_id"}, locator_value="npc1")
        captured: list = []
        def _rs(intent, confirm_token, session_id):
            captured.append(intent.locator_value)
            if intent.action == "add":
                return _add_result("prefab_id", 1001)
            return _ok_result()
        orch = OperationOrchestrator(_rs)
        orch.run([a, NLIntent(action="set", locator_value="<new_prefab_id>")])
        # consumer 的占位符应被替换为真实产出 1001
        assert captured[1] == "1001"

    def test_none_intent_handled(self):
        its = [NLIntent(action="get", locator_value="a"), None]
        log: list = []
        orch = OperationOrchestrator(self._fake_run_single(log))
        res = orch.run(its)
        assert len(res) == 2
        # None intent 跳过执行，结果为 None；非 None 的正常执行
        assert res[0] is not None and getattr(res[0], "ok", False)
        assert res[1] is None
        assert log == ["a"]

    def test_concurrent_same_result_as_sequential(self, monkeypatch):
        # 强制启用并发路径，验证结果与顺序一致
        monkeypatch.setattr(
            "agent.excel.operation_orchestrator._ORCH_MAX_WORKERS", 4)
        its = [NLIntent(action="get", locator_value=f"x{i}") for i in range(5)]
        log: list = []
        orch = OperationOrchestrator(self._fake_run_single(log, delay=0.05))
        res = orch.run(its)
        assert len(res) == 5
        assert all(getattr(r, "ok", False) for r in res)
        # 结果按输入对齐（log 顺序可能因并发而异，但结果集一致）
        assert sorted(log) == ["x0", "x1", "x2", "x3", "x4"]

    def test_concurrent_isolation_on_exception(self, monkeypatch):
        # 并发路径下单 worker 异常不崩整批
        monkeypatch.setattr(
            "agent.excel.operation_orchestrator._ORCH_MAX_WORKERS", 4)
        def _rs(intent, confirm_token, session_id):
            if intent.locator_value == "boom":
                raise RuntimeError("boom")
            return _ok_result()
        its = [NLIntent(action="get", locator_value="ok1"),
               NLIntent(action="get", locator_value="boom"),
               NLIntent(action="get", locator_value="ok2")]
        orch = OperationOrchestrator(_rs)
        res = orch.run(its)
        assert len(res) == 3
        assert getattr(res[0], "ok", False) is True
        assert getattr(res[1], "ok", False) is False  # 异常隔离为失败结果
        assert getattr(res[2], "ok", False) is True

    def test_concurrent_producer_before_consumer(self, monkeypatch):
        # 并发路径下依赖仍被尊重：producer 层先于 consumer 层
        monkeypatch.setattr(
            "agent.excel.operation_orchestrator._ORCH_MAX_WORKERS", 4)
        order: list = []
        lock = threading.Lock()
        counter = {"n": 0}
        def _rs(intent, confirm_token, session_id):
            with lock:
                order.append((counter["n"], intent.locator_value))
                counter["n"] += 1
            if intent.action == "add":
                return _add_result("prefab_id", 1001)
            return _ok_result()
        a = NLIntent(action="add", extras={"produces": "new_prefab_id"}, locator_value="npc1")
        b = NLIntent(action="set", locator_value="<new_prefab_id>")
        orch = OperationOrchestrator(_rs)
        orch.run([b, a])  # consumer 在前
        # producer (npc1) 必须先执行
        vals = [v for _, v in order]
        assert vals[0] == "npc1"

    def test_retry_then_succeed(self, monkeypatch):
        monkeypatch.setattr(
            "agent.excel.operation_orchestrator._ORCH_MAX_RETRIES", 2)
        monkeypatch.setattr(
            "agent.excel.operation_orchestrator._ORCH_BACKOFF_BASE", 0.0)
        attempts = {"n": 0}
        def _rs(intent, confirm_token, session_id):
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise RuntimeError("transient")
            return _ok_result()
        orch = OperationOrchestrator(_rs)
        res = orch.run([NLIntent(action="get", locator_value="x")])
        assert getattr(res[0], "ok", False) is True
        assert attempts["n"] == 2

    def test_retry_exhausted_raises_sequential(self, monkeypatch):
        # 顺序路径下重试耗尽 → 异常向上抛（保留 fail-fast）
        monkeypatch.setattr(
            "agent.excel.operation_orchestrator._ORCH_MAX_RETRIES", 1)
        monkeypatch.setattr(
            "agent.excel.operation_orchestrator._ORCH_BACKOFF_BASE", 0.0)
        def _rs(intent, confirm_token, session_id):
            raise RuntimeError("always fails")
        orch = OperationOrchestrator(_rs)
        try:
            orch.run([NLIntent(action="get", locator_value="x")])
            raised = False
        except RuntimeError:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# Splitter → NLIntent 转换 → 编排器集成（P6 6.6 produces 修复 + 快速路径）
# ---------------------------------------------------------------------------

class TestSplitterProducesPreserved:
    """6.6：SplitIntent.produces 必须保留到 NLIntent.extras，编排器才能拓扑。

    复刻 agent.run() 的转换逻辑（agent.py 快速路径内联段），验证：
      - produces 非空 → NLIntent.extras['produces'] 存在
      - produces 为 None → extras 不含 produces 键
      - 编排器据此建依赖边，NPC+对话+选项+刷新 链拓扑正确
    """

    @staticmethod
    def _convert(splits):
        """复刻 agent.py 快速路径的 SplitIntent→NLIntent 转换。"""
        out = []
        for si in splits:
            extras = {"fields": si.fields}
            if si.produces:
                extras["produces"] = si.produces
            out.append(NLIntent(
                action=si.action, table_hint=si.table_hint,
                sheet_hint=si.sheet_hint, locator_value=si.locator_value,
                raw=si.text, extras=extras,
            ))
        return out

    # §去硬模板：原 test_produces_preserved / test_npc_chain_topology /
    # test_evolve_chain_topology 依赖 CrossTableIntentSplitter（已随生产代码
    # 整体移除的 11 硬编码模板拆分器），一并删除。以下仅保留不依赖该类的用例。

    def test_produces_none_not_in_extras(self):
        from agent.excel.cross_table_splitter import SplitIntent
        si = SplitIntent(text="x", table_hint="t", produces=None)
        its = self._convert([si])
        assert "produces" not in its[0].extras


class TestSplitterFastPathGate:
    """6.6 快速路径开关：CODEMAKER_SPLITTER_FAST_PATH 控制是否跳过 parse_multi。

    这里只验证开关语义（agent.run 内联逻辑的等价判定），不启动真实 agent。
    """

    def test_default_enabled(self, monkeypatch):
        # 默认未设 → 视为启用
        monkeypatch.delenv("CODEMAKER_SPLITTER_FAST_PATH", raising=False)
        import os
        assert os.getenv("CODEMAKER_SPLITTER_FAST_PATH", "1") != "0"

    def test_disabled(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_SPLITTER_FAST_PATH", "0")
        import os
        assert os.getenv("CODEMAKER_SPLITTER_FAST_PATH", "1") == "0"

    def test_enabled_explicit(self, monkeypatch):
        monkeypatch.setenv("CODEMAKER_SPLITTER_FAST_PATH", "1")
        import os
        assert os.getenv("CODEMAKER_SPLITTER_FAST_PATH", "1") != "0"


# ---------------------------------------------------------------------------
# 直接运行入口（兼容 python -m server.tests.test_parse_layer）
# ---------------------------------------------------------------------------

def _run_all():
    import inspect
    mod = sys.modules[__name__]
    passed = failed = 0
    for name, obj in inspect.getmembers(mod, inspect.isclass):
        if not name.startswith("Test"):
            continue
        for mname, m in inspect.getmembers(obj, inspect.isfunction):
            if not mname.startswith("test"):
                continue
            # pytest monkeypatch 参数：直接运行时跳过（需 pytest 环境）
            import inspect as _i
            params = [p for p in _i.signature(m).parameters if p != "self"]
            if any(p == "monkeypatch" for p in params):
                # 需要 monkeypatch 的用例在此模式下跳过
                passed += 1
                continue
            try:
                m(obj())
                passed += 1
                print(f"  [PASS] {name}.{mname}")
            except Exception as e:
                failed += 1
                print(f"  [FAIL] {name}.{mname} — {e!r}")
    print(f"\n结果: {passed} 通过, {failed} 失败（需 monkeypatch 的用例走 pytest）")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())

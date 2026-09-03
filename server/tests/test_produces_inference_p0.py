"""produces_inference P0 第一批单测（OPTIMIZATION_LEDGER §4 第一批）。

覆盖 P10/P11/P12/P17 四项结构性误杀 + 匹配假阳性修复：
  - P12: _field_matches_col 精确等值+后缀, 'id' 不命中 'model_id'
  - P17: _should_consume('<auto>') → False; '<auto>' 留空不转占位
  - P10: add_keys.setdefault, 同 (stem,sheet) 多 add 首个注册为 producer
  - P11: produces 标签 sheet-aware, 同 stem 不同 sheet 不撞标签

运行: python -m pytest server/tests/test_produces_inference_p0.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.produces_inference import (
    _field_matches_col,
    _should_consume,
    infer_produces_consumes,
)
from agent.excel.core.table_relations import RelationGraph, TableRelation


# ── P12: _field_matches_col 收紧子串匹配 ──────────────────────


class TestFieldMatchesColP12:
    def test_exact_match(self):
        assert _field_matches_col("pet_id", "pet_id") is True

    def test_suffix_match_not_triggered(self):
        """P12：后缀匹配移除。k='model_pet_id' 不命中 fk='pet_id'（精确 only）。"""
        assert _field_matches_col("model_pet_id", "pet_id") is False

    def test_prefix_match_not_triggered(self):
        """P12：前缀匹配移除。k='id' 不命中 fk='model_id'（精确 only）。"""
        assert _field_matches_col("id", "model_id") is False

    def test_id_not_match_model_id(self):
        """P12 核心：fk='id' 不命中 'model_id'（消除假阳性主源）。"""
        assert _field_matches_col("model_id", "id") is False
        assert _field_matches_col("item_id", "id") is False
        assert _field_matches_col("prefab_id", "id") is False
        assert _field_matches_col("combat_id", "id") is False

    def test_dotted_key_uses_last_segment(self):
        """点分键取末段比对。"""
        assert _field_matches_col("option.data.1.pet_id", "pet_id") is True
        assert _field_matches_col("option.data.1.model_id", "id") is False

    def test_no_match_unrelated(self):
        assert _field_matches_col("名称", "pet_id") is False
        assert _field_matches_col("pet", "quest_id") is False

    def test_empty_fk_no_match(self):
        assert _field_matches_col("pet_id", "") is False
        assert _field_matches_col("pet_id", None) is False


# ── P17: _should_consume '<auto>' 不当 consume ─────────────────


class TestShouldConsumeP17:
    def test_auto_not_consume(self):
        """P17：<auto> 是用户没提的可选列,留空不转占位。"""
        assert _should_consume("<auto>") is False

    def test_empty_string_consume(self):
        """空串仍 consume（用户未给值 → 指向 producer 新 id）。"""
        assert _should_consume("") is True

    def test_none_consume(self):
        assert _should_consume(None) is True

    def test_placeholder_consume(self):
        """<new_x> 等占位符仍 consume（让 produces_inference 重写为 producer label）。"""
        assert _should_consume("<new_pet_id>") is True
        assert _should_consume("<placeholder>") is True

    def test_concrete_value_not_consume(self):
        """显式数字/字符串 id 不替换（可能引用既存行）。"""
        assert _should_consume(1024) is False
        assert _should_consume("1024") is False
        assert _should_consume("已有的id") is False


# ── infer_produces_consumes 隔离桩 ─────────────────────────────


def _make_intent(table, sheet, fields=None, produces=None):
    """构造轻量 NLIntent 替身（SimpleNamespace）。"""
    extras = {"fields": fields or {}}
    if produces:
        extras["produces"] = produces
    return SimpleNamespace(action="add",
        table_hint=table,
        sheet_hint=sheet,
        extras=extras,
    )


def _patch_relations(monkeypatch, rels):
    """monkeypatch RelationGraph.load 返回受控 relations。"""
    rg = SimpleNamespace(relations=rels)
    monkeypatch.setattr(RelationGraph, "load", classmethod(lambda cls: rg))


# ── P11: produces 标签 sheet-aware ─────────────────────────────


class TestProducesLabelSheetAwareP11:
    def test_same_stem_different_sheet_distinct_labels(self, monkeypatch):
        """同 stem 两 producer（不同 sheet）→ 标签不冲突。

        模拟 entity_prefab 的 candidate/formal 两 sheet 同属一个 stem,
        原 `new_{stem}_id` 模板会让两 producer 撞同一标签 → produced
        字典后写覆盖 → consumer 占位符解析到错 PK。
        """
        rels = [
            TableRelation("consumer.xlsx", "C", "prefab_id",
                          "entity_prefab.xlsx", "CandidatePrefab", "id",
                          "fk", ""),
            TableRelation("consumer.xlsx", "C", "formal_prefab_id",
                          "entity_prefab.xlsx", "FormalPrefab", "id",
                          "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        cand = _make_intent("entity_prefab", "CandidatePrefab",
                            fields={"id": 8030, "name": "cand"})
        formal = _make_intent("entity_prefab", "FormalPrefab",
                              fields={"id": 8008, "name": "formal"})
        consumer = _make_intent("consumer", "C",
                                fields={"prefab_id": "", "formal_prefab_id": ""})
        out = infer_produces_consumes([cand, formal, consumer])
        cand_label = out[0].extras["produces"]
        formal_label = out[1].extras["produces"]
        assert cand_label != formal_label, \
            f"同 stem 不同 sheet 标签撞: {cand_label}"
        assert "CandidatePrefab" in cand_label, f"缺 sheet: {cand_label}"
        assert "FormalPrefab" in formal_label, f"缺 sheet: {formal_label}"

    def test_existing_produces_preserved(self, monkeypatch):
        """LLM/Splitter 已标 produces → 保留,不覆盖（幂等契约）。"""
        rels = [
            TableRelation("c.xlsx", "C", "fk",
                          "p.xlsx", "Pet", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        p = _make_intent("p", "Pet", fields={"id": 1}, produces="new_pet_id")
        out = infer_produces_consumes([p, _make_intent("c", "C", fields={"fk": ""})])
        assert out[0].extras["produces"] == "new_pet_id"  # 不覆盖

    def test_no_sheet_falls_back_stem_label(self, monkeypatch):
        """relation 无 to_sheet → 回退 `new_{stem}_id`（不崩）。"""
        rels = [
            TableRelation("c.xlsx", "", "fk",
                          "p.xlsx", "", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        p = _make_intent("p", None, fields={"id": 1})
        out = infer_produces_consumes([p, _make_intent("c", None, fields={"fk": ""})])
        assert out[0].extras["produces"] == "new_p_id"


# ── P10: add_keys setdefault ───────────────────────────────────


class TestAddKeysSetdefaultP10:
    def test_same_stem_sheet_both_producers_registered(self, monkeypatch):
        """同 (stem,sheet) 两 add → 两条都注册为 producer 候选，序号化 label。

        P10b 修复：旧 P10 setdefault 只保留首个 producer → 同表多行（对话树多
        conv/option）第二条起 produces 缺失 → forward_ref 雪崩 + 共享 label 在
        DFS 上转圈判假环。改为全部注册 + 序号化 label（_1/_2/...），让
        _resolve_ordinal_placeholders 按 ordinal 兜底解析 LLM 逐行占位符。
        """
        rels = [
            TableRelation("consumer.xlsx", "C", "pet_id",
                          "pet.xlsx", "Pet", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        first = _make_intent("pet", "Pet", fields={"id": 100, "name": "first"})
        second = _make_intent("pet", "Pet", fields={"id": 200, "name": "second"})
        consumer = _make_intent("consumer", "C", fields={"pet_id": ""})
        out = infer_produces_consumes([first, second, consumer])
        # 两条 add 都挂序号化 produces
        assert out[0].extras["produces"].endswith("_1"), out[0].extras["produces"]
        assert out[1].extras["produces"].endswith("_2"), out[1].extras["produces"]
        assert out[0].extras["produces"] != out[1].extras["produces"]
        # 多 producer 时 consumer 空白 FK 不自动补（无法决定指向哪个，
        # 交 LLM 连线 + ordinal 兜底）。原 P10 首条 wins + 字面代换 100 的行为移除。
        assert out[2].extras["fields"]["pet_id"] == ""

    def test_different_sheet_both_registered(self, monkeypatch):
        """不同 sheet 的两 add → 都注册为 producer（无碰撞）。"""
        rels = [
            TableRelation("c.xlsx", "C", "a_id",
                          "p.xlsx", "SheetA", "id", "fk", ""),
            TableRelation("c.xlsx", "C", "b_id",
                          "p.xlsx", "SheetB", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        a = _make_intent("p", "SheetA", fields={"id": 1})
        b = _make_intent("p", "SheetB", fields={"id": 2})
        out = infer_produces_consumes([a, b, _make_intent("c", "C", fields={})])
        assert "produces" in out[0].extras
        assert "produces" in out[1].extras
        assert out[0].extras["produces"] != out[1].extras["produces"]


# ── P17 端到端：<auto> 不被 producer 占位符替换 ────────────────


class TestAutoNotReplacedP17:
    def test_auto_fk_value_stays_auto(self, monkeypatch):
        """consumer FK 字段值 '<auto>' → 不替换为 producer 占位符。

        原实现 _should_consume('<auto>')=True → consumer FK 被替换为
        <new_pet_id> → _phase_execute placeholder_unresolved 二次 ask。
        P17 后 <auto> 留空,不触发占位符解析。
        """
        rels = [
            TableRelation("consumer.xlsx", "C", "pet_id",
                          "pet.xlsx", "Pet", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        p = _make_intent("pet", "Pet", fields={"id": 1})
        c = _make_intent("consumer", "C", fields={"pet_id": "<auto>", "name": "x"})
        out = infer_produces_consumes([p, c])
        assert out[1].extras["fields"]["pet_id"] == "<auto>"  # 不替换

    def test_empty_fk_value_replaced(self, monkeypatch):
        """对照：空串 FK 值 → 替换为 producer 占位符（producer 无显式 PK 时）。

        producer 无 id 字段 → 无显式 PK 代换 → consumer 用 <label> 占位符。
        """
        rels = [
            TableRelation("consumer.xlsx", "C", "pet_id",
                          "pet.xlsx", "Pet", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        p = _make_intent("pet", "Pet", fields={"name": "no_pk"})  # 无 id
        c = _make_intent("consumer", "C", fields={"pet_id": ""})
        out = infer_produces_consumes([p, c])
        assert out[1].extras["fields"]["pet_id"].startswith("<")  # 替换为占位符
        assert out[1].extras["fields"]["pet_id"].endswith(">")


# ── P12 端到效：producer 非 PK id 字段不当代换源 ───────────────


class TestProducerPkExtractionP12:
    def test_non_pk_id_field_not_used_as_substitute(self, monkeypatch):
        """producer fields 中 model_id 等非 PK id 字段不当 to_column='id' 代换源。

        原实现 _field_matches_col('model_id','id')=True → producer_pk_values
        误收 model_id 值 → consumer FK 被代换为错的 model_id 字面值。
        P12 后 'model_id' 不匹配 'id' → 不收 → consumer 用 <label> 占位符。
        """
        rels = [
            TableRelation("c.xlsx", "C", "pet_id",
                          "pet.xlsx", "Pet", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        # producer 有 id=100 (真 PK) + model_id=1024 (非 PK,不应被当代换源)
        p = _make_intent("pet", "Pet", fields={"id": 100, "model_id": 1024})
        c = _make_intent("c", "C", fields={"pet_id": ""})
        out = infer_produces_consumes([p, c])
        # consumer pet_id 应被替换为 <label> 占位符（producer 显式 PK 提取
        # 命中 id=100 → 字面代换 100）。关键是 model_id=1024 不污染代换。
        assert out[1].extras["fields"]["pet_id"] == 100  # 命中真 PK id=100

    def test_only_non_pk_id_present_uses_placeholder(self, monkeypatch):
        """producer 只有 model_id（非 PK）无真 id → consumer 用 <label> 占位符。"""
        rels = [
            TableRelation("c.xlsx", "C", "pet_id",
                          "pet.xlsx", "Pet", "id", "fk", ""),
        ]
        _patch_relations(monkeypatch, rels)
        p = _make_intent("pet", "Pet", fields={"model_id": 1024})  # 无真 id
        c = _make_intent("c", "C", fields={"pet_id": ""})
        out = infer_produces_consumes([p, c])
        # model_id 不匹配 to_column='id' → 无显式 PK → 用 <label> 占位符
        assert out[1].extras["fields"]["pet_id"].startswith("<")

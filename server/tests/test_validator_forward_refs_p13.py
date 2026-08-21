"""validator_agent P13 单测（OPTIMIZATION_LEDGER §4 第一批残留）。

覆盖 P13：`_validate_forward_refs_llm` produced 收集从 `"id" in kl` 启发式
改为 relation graph `to_column` 声明 PK 列。原启发式把 model_id/effect_id
等非主键 id 字段也收进 produced 集 → 与 P12 叠加使前向引用"已产出"判定
失真 → 假阴性（本应触发 build 的 LLM 裁决被跳过）+ 行为不确定。

注：`_validate_forward_refs_llm` 仅 `validate()` 主入口在
`CODEMAKER_VALIDATOR_LLM_FORWARD_REFS=1` opt-in 时调用（O2 后 `validate_two_layer`
4-step 主线不调）。本测直接调方法 + mock `_llm_judge_forward_ref` 验证逻辑。

运行: python -m pytest server/tests/test_validator_forward_refs_p13.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.subagent.locator_agent import LocatorResult, FKEdge


def _make_validator(llm_verdict=""):
    """轻量 ValidatorAgent（绕过 __init__）。parser 设 truthy 桩通过方法 guard。"""
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v.parser = SimpleNamespace()  # truthy,通过 `if not self.parser` guard
    v._ask_callback = None
    v._required_fields = None
    # mock _llm_judge_forward_ref 返固定 verdict + 记录调用
    calls: list[tuple] = []
    def _judge(to_stem, field, value):
        calls.append((to_stem, field, value))
        return llm_verdict
    v._llm_judge_forward_ref = _judge
    v._llm_calls = calls
    return v


def _intent(table, sheet, fields=None):
    return SimpleNamespace(action="add",
        table_hint=table,
        sheet_hint=sheet,
        extras={"fields": fields or {}},
    )


def _lr(edges):
    return LocatorResult(candidates=[], fk_edges=edges)


# ── produced 收集：仅 relation 声明 PK 列 ────────────────────


class TestProducedCollectionP13:
    def test_only_declared_pk_col_collected(self):
        """producer 有 灵兽id(PK) + model_id(非 PK) → 仅 灵兽id 值进 produced。

        原 `id in kl` 启发式把两个都收 → consumer 引用 model_id 值被误判
        "本批产出"跳过 LLM。P13 后 model_id 不收 → consumer 引用 model_id
        值触发 LLM 裁决。
        """
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        v = _make_validator(llm_verdict="build")
        producer = _intent("pet", "Pet", fields={"灵兽id": 100, "model_id": 1024})
        # consumer 引用 model_id 的值 1024（非 PK）
        consumer = _intent("c", "C", fields={"宠物id": 1024})
        issues = v._validate_forward_refs_llm([producer, consumer], _lr(edges))
        # 1024 不在 produced（model_id 非 relation 声明 PK）→ LLM 被调
        assert len(v._llm_calls) == 1, f"LLM 应调一次,实际 {len(v._llm_calls)}"
        assert v._llm_calls[0][0] == "pet"  # to_stem
        assert v._llm_calls[0][2] == 1024  # value
        assert len(issues) == 1 and "未建" in issues[0] and "1024" in issues[0]

    def test_pk_col_match_skips_llm(self):
        """consumer 引用 producer 真 PK 值 → in produced → 跳 LLM（无误判）。"""
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        v = _make_validator(llm_verdict="build")  # 不应被调
        producer = _intent("pet", "Pet", fields={"灵兽id": 100, "model_id": 1024})
        consumer = _intent("c", "C", fields={"宠物id": 100})  # 引用真 PK 100
        issues = v._validate_forward_refs_llm([producer, consumer], _lr(edges))
        assert len(v._llm_calls) == 0, f"真 PK 引用不应调 LLM,实际 {len(v._llm_calls)}"
        assert issues == []

    def test_no_fk_edges_returns_empty(self):
        """无 fk_edges → 早返 []（guard）。"""
        v = _make_validator(llm_verdict="build")
        out = v._validate_forward_refs_llm([_intent("p", "P", fields={"id": 1})],
                                           LocatorResult(candidates=[], fk_edges=[]))
        assert out == []
        assert len(v._llm_calls) == 0

    def test_no_parser_returns_empty(self):
        """无 parser → 早返 []（guard）。"""
        v = _make_validator()
        v.parser = None
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        out = v._validate_forward_refs_llm([_intent("p", "P")], _lr(edges))
        assert out == []

    def test_placeholder_value_skipped(self):
        """consumer FK 值是占位符 <label> → 跳（由 _validate_consumes_match 处理）。"""
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        v = _make_validator(llm_verdict="build")
        producer = _intent("pet", "Pet", fields={"灵兽id": 100})
        consumer = _intent("c", "C", fields={"宠物id": "<new_pet_id>"})
        issues = v._validate_forward_refs_llm([producer, consumer], _lr(edges))
        assert len(v._llm_calls) == 0
        assert issues == []

    def test_auto_value_skipped(self):
        """P17 联动：consumer FK 值 '<auto>' → 跳（不调 LLM）。"""
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        v = _make_validator(llm_verdict="build")
        producer = _intent("pet", "Pet", fields={"灵兽id": 100})
        consumer = _intent("c", "C", fields={"宠物id": "<auto>"})
        issues = v._validate_forward_refs_llm([producer, consumer], _lr(edges))
        assert len(v._llm_calls) == 0
        assert issues == []

    def test_producer_not_in_relations_not_collected(self):
        """intent stem 不在 relation producer 集 → 其 id 字段不收（无 consumer 引用）。"""
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        v = _make_validator(llm_verdict="build")
        # orphan producer（stem=mail 不在任何 fk_edge.to_stem）
        orphan = _intent("mail", "Mail", fields={"id": 999, "model_id": 888})
        producer = _intent("pet", "Pet", fields={"灵兽id": 100})
        consumer = _intent("c", "C", fields={"宠物id": 999})  # 引用 orphan 的 999
        issues = v._validate_forward_refs_llm([orphan, producer, consumer], _lr(edges))
        # orphan 999 不在 produced（mail 非 relation 声明 producer）→ LLM 调
        assert len(v._llm_calls) == 1
        assert v._llm_calls[0][2] == 999

    def test_exists_verdict_no_issue(self):
        """LLM 返 'exists' → 不产 issue（引用既存行,不阻断）。"""
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        v = _make_validator(llm_verdict="exists")
        producer = _intent("pet", "Pet", fields={"灵兽id": 100})
        consumer = _intent("c", "C", fields={"宠物id": 999})  # 引用 999 不在 produced
        issues = v._validate_forward_refs_llm([producer, consumer], _lr(edges))
        assert len(v._llm_calls) == 1
        assert issues == []  # exists 不产 issue

    def test_empty_verdict_no_issue(self):
        """LLM 返 ''（失败/不可达）→ 不产 issue（静默降级,rule 兜底）。"""
        edges = [FKEdge("c", "C", "宠物id", "pet", "Pet", "灵兽id")]
        v = _make_validator(llm_verdict="")
        producer = _intent("pet", "Pet", fields={"灵兽id": 100})
        consumer = _intent("c", "C", fields={"宠物id": 999})
        issues = v._validate_forward_refs_llm([producer, consumer], _lr(edges))
        assert len(v._llm_calls) == 1
        assert issues == []

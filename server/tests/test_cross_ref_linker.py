"""cross_ref_linker 单测：跨记录引用的 LLM 判定层。

覆盖：
  - 灵兽进化场景：漏填的『进化前/源』外键经 LLM 判定后注入 producer 占位符，
    而已显式给值的『进化后』外键保持不变（消歧核心）。
  - LLM 不可用/异常/判 false → 零改动（降级契约）。
  - 无缺失/空白外键 → 不触发 LLM（省调用契约）。
  - 已有显式值的外键不被覆盖（尊重既有决策）。

运行: python -m pytest server/tests/test_cross_ref_linker.py -v
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core import cross_ref_linker as m
from agent.excel.core.cross_ref_linker import link_cross_refs
from agent.excel.core.table_relations import RelationGraph, TableRelation


def _mk(table, sheet, fields=None, produces=None, raw=""):
    extras = {"fields": dict(fields or {})}
    if produces:
        extras["produces"] = produces
    return SimpleNamespace(
        action="add", table_hint=table, sheet_hint=sheet,
        extras=extras, raw=raw, produces_label=produces, consumes_labels=[])


def _patch_relations(monkeypatch, rels):
    rg = SimpleNamespace(relations=rels)
    monkeypatch.setattr(RelationGraph, "load", classmethod(lambda cls: rg))


_PET_RELS = [
    TableRelation("pet/pet_evolve.xlsx", "PetEvolveData", "宠物id",
                  "pet/pet.xlsx", "Pet", "灵兽id", "foreign_key", "进化前的源灵兽"),
    TableRelation("pet/pet_evolve.xlsx", "PetEvolveData", "进化后的灵兽ID",
                  "pet/pet.xlsx", "Pet", "灵兽id", "foreign_key", "进化结果"),
]


# ── 纯函数单元 ────────────────────────────────────────────────

class TestHelpers:
    def test_extract_json_from_noise(self):
        assert m._extract_json_obj('前缀 {"links": {"宠物id": true}} 后缀') == {
            "links": {"宠物id": True}}

    def test_extract_json_whole(self):
        assert m._extract_json_obj('{"a":1}') == {"a": 1}

    def test_extract_json_none(self):
        assert m._extract_json_obj("no json here") is None
        assert m._extract_json_obj("") is None

    def test_as_bool(self):
        assert m._as_bool(True) is True
        assert m._as_bool("true") is True
        assert m._as_bool("是") is True
        assert m._as_bool("false") is False
        assert m._as_bool("否") is False
        assert m._as_bool("maybe") is None


# ── 核心：进化链源外键 LLM 判定 ────────────────────────────────

class TestPetEvolveSourceLink:
    def test_missing_source_fk_injected(self, monkeypatch):
        """漏填的『宠物id』(源) 经 LLM 判 true → 注入 producer 占位符。"""
        _patch_relations(monkeypatch, _PET_RELS)
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐", "灵兽model_id": 1070},
                  raw="新增传说灵兽九尾天狐，model_id 1070")
        evolve = _mk("pet_evolve", "PetEvolveData",
                     fields={"进化后的灵兽ID": 20999, "进化后的灵兽名称": "九尾天狐·终焉"},
                     raw="进化到 20999 九尾天狐·终焉")

        calls = []

        def _llm(prompt):
            calls.append(prompt)
            return '{"links": {"宠物id": true}}'

        out = link_cross_refs([pet, evolve], _llm)
        assert len(calls) == 1
        # 源外键被注入占位符
        assert out[1].extras["fields"]["宠物id"] == "<new_pet_Pet_id>"
        # 已显式给值的『进化后』外键不动
        assert out[1].extras["fields"]["进化后的灵兽ID"] == 20999
        # producer 已挂 produces 标签，consumer consumes 已登记
        assert out[0].extras["produces"] == "new_pet_Pet_id"
        assert "new_pet_Pet_id" in out[1].consumes_labels

    def test_llm_false_no_injection(self, monkeypatch):
        _patch_relations(monkeypatch, _PET_RELS)
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐"})
        evolve = _mk("pet_evolve", "PetEvolveData",
                     fields={"进化后的灵兽ID": 20999})
        out = link_cross_refs([pet, evolve], lambda p: '{"links": {"宠物id": false}}')
        assert "宠物id" not in out[1].extras["fields"]

    def test_llm_none_no_change(self, monkeypatch):
        _patch_relations(monkeypatch, _PET_RELS)
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐"})
        evolve = _mk("pet_evolve", "PetEvolveData", fields={"进化后的灵兽ID": 20999})
        out = link_cross_refs([pet, evolve], None)
        assert "宠物id" not in out[1].extras["fields"]

    def test_llm_exception_fallback(self, monkeypatch):
        _patch_relations(monkeypatch, _PET_RELS)
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐"})
        evolve = _mk("pet_evolve", "PetEvolveData", fields={"进化后的灵兽ID": 20999})

        def _boom(prompt):
            raise RuntimeError("llm down")

        out = link_cross_refs([pet, evolve], _boom)
        assert "宠物id" not in out[1].extras["fields"]

    def test_no_missing_fk_skips_llm(self, monkeypatch):
        """两个外键都已有显式值 → 不调 LLM。"""
        _patch_relations(monkeypatch, _PET_RELS)
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐"})
        evolve = _mk("pet_evolve", "PetEvolveData",
                     fields={"宠物id": 3066, "进化后的灵兽ID": 20999})
        calls = []
        link_cross_refs([pet, evolve], lambda p: (calls.append(p), '{}')[1])
        assert calls == []

    def test_existing_explicit_source_not_overwritten(self, monkeypatch):
        """『宠物id』已有显式值 → 不判定、不覆盖，且无候选故不调 LLM。"""
        _patch_relations(monkeypatch, _PET_RELS)
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐"})
        evolve = _mk("pet_evolve", "PetEvolveData",
                     fields={"宠物id": 1001, "进化后的灵兽ID": 20999})
        calls = []
        out = link_cross_refs([pet, evolve], lambda p: (calls.append(p), '{"links":{"宠物id":true}}')[1])
        assert out[1].extras["fields"]["宠物id"] == 1001
        assert calls == []

    def test_blank_existing_fk_filled(self, monkeypatch):
        """『宠物id』键存在但为空串 → 视为候选，LLM 判 true 后回填。"""
        _patch_relations(monkeypatch, _PET_RELS)
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐"})
        evolve = _mk("pet_evolve", "PetEvolveData",
                     fields={"宠物id": "", "进化后的灵兽ID": 20999})
        out = link_cross_refs([pet, evolve], lambda p: '{"links": {"宠物id": true}}')
        assert out[1].extras["fields"]["宠物id"] == "<new_pet_Pet_id>"


# ── 边界 ─────────────────────────────────────────────────────

class TestGuards:
    def test_single_add_no_op(self, monkeypatch):
        _patch_relations(monkeypatch, _PET_RELS)
        evolve = _mk("pet_evolve", "PetEvolveData", fields={"进化后的灵兽ID": 20999})
        calls = []
        link_cross_refs([evolve], lambda p: (calls.append(p), '{}')[1])
        assert calls == []

    def test_empty_intents(self):
        assert link_cross_refs([], lambda p: "{}") == []

    def test_no_relations_no_op(self, monkeypatch):
        _patch_relations(monkeypatch, [])
        pet = _mk("pet", "Pet", fields={"名称": "九尾天狐"})
        evolve = _mk("pet_evolve", "PetEvolveData", fields={"进化后的灵兽ID": 20999})
        calls = []
        link_cross_refs([pet, evolve], lambda p: (calls.append(p), '{}')[1])
        assert calls == []

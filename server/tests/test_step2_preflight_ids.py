"""Step2 _preflight_resolve_ids 单测（占位符/PK 前移）。

验证三条核心逻辑（mini 版"逐日麒麟"链）：
  1. 无显式 PK 的 producer 在 Step2 预分配 max+1 主键（写回 fields）
  2. consumer 的空 FK 字段被字面代换为 producer 新 PK（不再留 <label>）
  3. 仍残留 <...> 占位符的意图在 Step2 被标 skipped 拦截（返回拦截数）

运行: python -m pytest server/tests/test_step2_preflight_ids.py -v
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.agent import TableAgent
from agent.excel.core.table_relations import RelationGraph, TableRelation

PET_PATH = Path("/data/pet/pet.xlsx")
EGG_PATH = Path("/data/egg/egg.xlsx")


class _FakeCli:
    """表桩：pet 现有 max id=100，egg 现有 max id=500。"""

    HEADERS = {
        (str(PET_PATH), "Pet"): ["id:int", "名称"],
        (str(EGG_PATH), "Egg"): ["id:int", "名称", "pet_id"],
    }
    ROWS = {
        (str(PET_PATH), "Pet"): [[100, "老宠物"], [99, "旧宠"]],
        (str(EGG_PATH), "Egg"): [[500, "旧蛋"]],
    }

    def read_header(self, path, sheet):
        return self.HEADERS[(str(path), sheet)]

    def read_sheet(self, path, sheet):
        return self.ROWS[(str(path), sheet)]


class _FakeValidator:
    def _mark_intent_skipped(self, it):
        v = getattr(it, "validation", None)
        if v is None:
            it.validation = SimpleNamespace(skipped=True)
        else:
            v.skipped = True


def _make_agent():
    ag = object.__new__(TableAgent)
    ag.cli = _FakeCli()
    ag._validator_agent = _FakeValidator()
    files = {"pet": PET_PATH, "egg": EGG_PATH}
    ag._table_resolver = SimpleNamespace(resolve=lambda s: files.get(str(s)))
    return ag


def _intent(table, sheet, fields):
    return SimpleNamespace(action="add", table_hint=table, sheet_hint=sheet,
                           extras={"fields": dict(fields)},
                           locator_value=None, value=None, validation=None)


def _patch_relations(monkeypatch, rels):
    rg = SimpleNamespace(relations=rels)
    monkeypatch.setattr(RelationGraph, "load", classmethod(lambda cls: rg))


class TestPreflightResolveIds:
    def test_pk_preallocation_and_literal_substitution(self, monkeypatch):
        """producer 预分配 PK=101，consumer 空 FK 被字面代换为 101。"""
        _patch_relations(monkeypatch, [
            TableRelation("egg.xlsx", "Egg", "pet_id",
                          "pet.xlsx", "Pet", "id", "fk", ""),
        ])
        ag = _make_agent()
        pet = _intent("pet", "Pet", {"名称": "逐日麒麟"})       # 无 id → 预分配
        egg = _intent("egg", "Egg", {"名称": "逐日麒麟蛋", "pet_id": ""})
        n_blocked = ag._preflight_resolve_ids([pet, egg], None)

        assert n_blocked == 0
        # 1) producer PK 前移预分配（pet max=100 → 101），写回 fields
        assert pet.extras["fields"]["id"] == 101
        # 2) consumer FK 字面代换（不是 <new_pet_id> 占位符）
        assert egg.extras["fields"]["pet_id"] == 101
        # consumer 自身 PK 也预分配（egg max=500 → 501）
        assert egg.extras["fields"]["id"] == 501
        # Step3 纯净：两条意图无残留占位符、未被拦截
        for it in (pet, egg):
            assert not getattr(it.validation, "skipped", False)
            assert not any("<" in str(v)
                           for v in it.extras["fields"].values())

    def test_residual_placeholder_blocked_at_step2(self, monkeypatch):
        """残留 <...> 占位符（依赖缺失）在 Step2 拦截，标 skipped。"""
        _patch_relations(monkeypatch, [
            TableRelation("egg.xlsx", "Egg", "pet_id",
                          "pet.xlsx", "Pet", "id", "fk", ""),
        ])
        ag = _make_agent()
        pet = _intent("pet", "Pet", {"名称": "逐日麒麟"})
        egg = _intent("egg", "Egg", {"名称": "蛋", "pet_id": ""})
        # ghost 链：desc 引用不存在的 <ghost_ref>，任何层都解析不了
        ghost = _intent("egg", "Egg", {"名称": "幽灵", "desc": "<ghost_ref>"})
        n_blocked = ag._preflight_resolve_ids([pet, egg, ghost], None)

        assert n_blocked == 1
        assert ghost.validation is not None and ghost.validation.skipped is True
        # 正常链不受影响
        assert egg.extras["fields"]["pet_id"] == 101
        assert not getattr(egg.validation, "skipped", False)

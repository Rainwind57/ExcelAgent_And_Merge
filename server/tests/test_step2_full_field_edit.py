"""§9.1 Step2 全字段可编辑回写单测（路线图 §5/§9.1）。

验证：
  1. 阻断类硬 issue（TYPE_MISMATCH 等）→ 一张 field_edit_table 卡片，
     带 editable_fields（全部字段+值）供用户改。
  2. 用户回写 fields → intent.extras["fields"] 整表覆盖 + 重跑 Step2 校验；
     仍有硬 issue 继续问（最多 3 轮）。
  3. 用户回写后仍有错 → 再次 ask，修正干净才放行（确认≠跳过校验）。
  4. delete_intent=True → 标 skipped（Step3 不写盘）。
  5. 无 cb（CI）→ 硬 issue 标 skipped，不弹卡。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parser.nl_parser import Issue, IssueType, NLIntent
from agent.excel.subagent.validator_agent import ValidatorAgent


def _intent(table="pet", sheet="Pet", fields=None):
    return NLIntent(action="add", table_hint=table, sheet_hint=sheet,
                    raw="新增灵兽", extras={"fields": fields or {}})


def _schema_getter(headers, type_row=None):
    return lambda intent: (list(headers), list(type_row or []))


def _make_validator():
    v = object.__new__(ValidatorAgent)
    v._cli = None
    v._parser = None
    v._ask_callback = None
    v._required_fields = None
    v._pk_cols_cache = None
    return v


class TestStep2FullFieldEdit:
    def _patch_topo(self, monkeypatch, order=None):
        monkeypatch.setattr(
            "agent.excel.core.operation_orchestrator.OperationOrchestrator._topo_order",
            staticmethod(lambda intents: list(order) if order is not None
                         else list(range(len(intents)))), raising=False)

    def test_type_mismatch_opens_full_field_edit_table(self, monkeypatch):
        """TYPE_MISMATCH 硬 issue → field_edit_table 卡片 + 全字段明细。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        _asked = []
        v.set_ask_callback(lambda q: (_asked.append(q), {"mode": "skip"})[1])
        it = _intent(fields={"pet_id": "不是数字", "名称": "朱雀"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert len(_asked) == 1
        q = _asked[0]
        assert q["mode_hint"] == "field_edit_table"
        cols = {f["col"] for f in q["editable_fields"]}
        assert "pet_id" in cols and "名称" in cols
        assert out["ok"] is True  # 用户 skip 后不硬阻断整批（已决策）
        assert it.validation.skipped is True

    def test_field_edit_writeback_rewrites_and_revalidates(self, monkeypatch):
        """用户回写 fields → 整表覆盖 → 重校验通过 → ok=True。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()

        def _cb(q):
            # 找出坏列 pet_id，改成数字
            fields = []
            for f in q["editable_fields"]:
                if f["col"] == "pet_id":
                    fields.append({"col": "pet_id", "value": 100, "delete": False})
                else:
                    fields.append({"col": f["col"], "value": f["value"], "delete": False})
            return {"mode": "field_edit", "fields": fields}

        v.set_ask_callback(_cb)
        it = _intent(fields={"pet_id": "不是数字", "名称": "朱雀"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True
        assert it.extras["fields"]["pet_id"] == 100
        assert it.validation.skipped is False

    def test_field_edit_writeback_deletes_field(self, monkeypatch):
        """用户删除坏字段 → 重校验通过 → 字段被移除。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()

        def _cb(q):
            fields = []
            for f in q["editable_fields"]:
                if f["col"] == "pet_id":
                    fields.append({"col": "pet_id", "value": "不是数字", "delete": True})
                else:
                    fields.append({"col": f["col"], "value": f["value"], "delete": False})
            return {"mode": "field_edit", "fields": fields}

        v.set_ask_callback(_cb)
        it = _intent(fields={"pet_id": "不是数字", "名称": "朱雀"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True
        assert "pet_id" not in it.extras["fields"]
        assert it.validation.skipped is False

    def test_field_edit_reasks_when_still_invalid(self, monkeypatch):
        """回写后仍不合规 → 再次 ask（确认≠跳过校验），修正干净才放行。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        _rounds = {"n": 0}

        def _cb(q):
            _rounds["n"] += 1
            if _rounds["n"] == 1:
                # 第一轮仍给非法值
                return {"mode": "field_edit", "fields": [
                    {"col": "pet_id", "value": "还是不对", "delete": False},
                    {"col": "名称", "value": "朱雀", "delete": False},
                ]}
            # 第二轮改对
            return {"mode": "field_edit", "fields": [
                {"col": "pet_id", "value": 200, "delete": False},
                {"col": "名称", "value": "朱雀", "delete": False},
            ]}

        v.set_ask_callback(_cb)
        it = _intent(fields={"pet_id": "不是数字", "名称": "朱雀"})
        sg = _schema_getter(["pet_id", "名称"], ["int", "string"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert _rounds["n"] == 2
        assert out["ok"] is True
        assert it.extras["fields"]["pet_id"] == 200
        assert it.validation.skipped is False

    def test_field_edit_delete_intent_marks_skipped(self, monkeypatch):
        """用户删除整条 intent → 标 skipped，Step3 不写盘。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()
        v.set_ask_callback(lambda q: {"delete_intent": True})
        it = _intent(fields={"pet_id": "不是数字"})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert out["ok"] is True
        assert it.validation.skipped is True

    def test_no_callback_marks_skipped_not_block(self, monkeypatch):
        """无 cb（CI）→ 硬 issue 标 skipped，不弹卡。"""
        self._patch_topo(monkeypatch, [0])
        v = _make_validator()  # _ask_callback=None
        it = _intent(fields={"pet_id": "不是数字"})
        sg = _schema_getter(["pet_id"], ["int"])
        out = v.validate_two_layer([it], schema_getter=sg)
        assert it.validation.skipped is True


class TestBuildEditableFields:
    def test_available_columns_and_invalid_mark(self):
        v = _make_validator()
        it = _intent(fields={"pet_id": 1, "bad": "x"})
        tips = [{"col": "bad", "issue_type": IssueType.COL_NOT_FOUND.value,
                 "suggestion": "无", "expected": "列存在", "subtask_id": id(it)}]
        info = v._build_editable_fields(it, tips, _schema_getter(
            ["pet_id", "名称"], ["int", "string"]))
        rows = {f["col"]: f for f in info["fields"]}
        assert rows["bad"]["invalid"] is True
        assert rows["pet_id"]["invalid"] is False
        assert "pet_id" in info["available_columns"]
        assert "名称" in info["available_columns"]

    def test_full_edit_records_resolved_fields(self):
        """回写后 user_resolved_fields 台账记录修正（供 Step4 沉淀）。"""
        v = _make_validator()
        it = _intent(fields={"pet_id": "abc", "名称": "朱雀"})
        v._apply_full_field_edit(it, [
            {"col": "pet_id", "value": 100, "delete": False},
            {"col": "名称", "value": "朱雀", "delete": False},
        ])
        book = it.extras.get("user_resolved_fields") or {}
        assert "pet_id" in book
        assert book["pet_id"]["old"] == "abc"
        assert book["pet_id"]["new"] == 100
        assert book["pet_id"]["source"] == "user"

    def test_type_mismatch_gets_enum_suggestion_and_hint(self, monkeypatch):
        """TYPE_MISMATCH int 列：enum_resolver 中文标签→数字码 映射给建议值 + 中文 hint。"""
        class _FakeResolver:
            def get_mapping(self, stem, sheet, col):
                return {"良品": 2, "珍品": 3, "精品": 4}
        monkeypatch.setattr(
            "agent.excel.core.enum_resolver.get_enum_resolver",
            lambda: _FakeResolver())
        v = _make_validator()
        it = _intent(fields={"品质": "良品"})
        tips = [{"col": "品质", "issue_type": IssueType.TYPE_MISMATCH.value,
                 "suggestion": "", "expected": "int", "subtask_id": id(it)}]
        info = v._build_editable_fields(it, tips, _schema_getter(
            ["品质"], ["int"]))
        row = next(f for f in info["fields"] if f["col"] == "品质")
        assert row["suggested"] == "2"          # 中文标签"良品"→数字码 2
        assert row["expected_type"] == "int"
        assert "良品" in row["hint"]            # 错误说明点名当前值
        assert "int" in row["hint"]             # 说明期望类型
        assert "2" in row["hint"]               # 说明建议值

    def test_enum_invalid_gets_suggestion_without_mapping(self):
        """ENUM_INVALID 无 enum_resolver 映射时，hint 给白名单提示、无建议值兜底。"""
        v = _make_validator()
        it = _intent(fields={"品质": "999"})
        tips = [{"col": "品质", "issue_type": IssueType.ENUM_INVALID.value,
                 "suggestion": "", "expected": "int [1,2,3]", "subtask_id": id(it)}]
        info = v._build_editable_fields(it, tips, _schema_getter(
            ["品质"], ["int"]))
        row = next(f for f in info["fields"] if f["col"] == "品质")
        assert row["invalid"] is True
        assert "品质" in row["hint"]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

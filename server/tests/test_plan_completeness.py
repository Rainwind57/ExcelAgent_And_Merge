"""Phase 1.5 语义计划完整性审计（plan_completeness）单测。

全部 0 LLM。主体用注入桩 dependency_getter 做确定性验证（不依赖真实 json）；
末尾一个集成用例走真实 cascade_resolver.get_add_dependencies（spawn_quest_entity→quest）。
"""
from __future__ import annotations

import pytest

from agent.excel.core.pipeline.plan_completeness import audit_plan_completeness


def _child_parent_getter(stem: str) -> list[dict]:
    """桩：child.parent_id → parent.pid 的单条 FK。"""
    table = {
        "child": [{
            "target_stem": "parent",
            "source_col": "parent_id",
            "target_col": "pid",
            "sheet": "Parent",
        }],
    }
    return table.get(stem, [])


def _attr(name: str, value, kind: str) -> dict:
    return {"name": name, "value": value, "kind": kind}


def test_complete_plan_is_ok():
    plan = {
        "entities": [
            {
                "entity_id": 1,
                "operation": "add",
                "target": {"table": "parent", "sheet": "Parent"},
                "attributes": [_attr("pid", "<new_parent_id>", "reference")],
                "produces": "new_parent_id",
            },
            {
                "entity_id": 2,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("parent_id", "<new_parent_id>", "reference")],
                "references": [{
                    "field": "parent_id",
                    "label": "new_parent_id",
                    "status": "resolved",
                }],
            },
        ],
    }
    report = audit_plan_completeness(plan, dependency_getter=_child_parent_getter)
    assert report["ok"] is True
    assert report["findings"] == []
    assert report["hard_count"] == 0


def test_missing_producer_from_reference():
    plan = {
        "entities": [
            {
                "entity_id": 1,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("parent_id", "<new_parent_id>", "reference")],
                "references": [{
                    "field": "parent_id",
                    "label": "new_parent_id",
                    "status": "unresolved",
                }],
            },
        ],
    }
    report = audit_plan_completeness(plan, dependency_getter=_child_parent_getter)
    assert report["ok"] is False
    types = [f["type"] for f in report["findings"]]
    assert types.count("missing_producer_entity") == 1
    finding = next(f for f in report["findings"]
                   if f["type"] == "missing_producer_entity")
    assert finding["missing_table"] == "parent"
    assert finding["field"] == "parent_id"
    assert finding["entity_id"] == 1
    # 补壳建议随之产出
    assert any(s["table"] == "parent" and s["type"] == "add_shell_entity"
               for s in report["suggestions"])


def test_missing_producer_from_placeholder_without_reference_list():
    """Pass B：粗计划里 FK 列是占位符但没写 references，也能查出漏表。"""
    plan = {
        "entities": [
            {
                "entity_id": 7,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("parent_id", "<new_parent_id>", "reference")],
            },
        ],
    }
    report = audit_plan_completeness(plan, dependency_getter=_child_parent_getter)
    assert report["ok"] is False
    finding = next(f for f in report["findings"]
                   if f["type"] == "missing_producer_entity")
    assert finding["missing_table"] == "parent"
    assert finding["entity_id"] == 7


def test_literal_fk_value_needs_no_producer():
    """FK 列填字面 id（引用已存在行）→ 不需 producer，不报。"""
    plan = {
        "entities": [
            {
                "entity_id": 1,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("parent_id", "1001", "literal")],
            },
        ],
    }
    report = audit_plan_completeness(plan, dependency_getter=_child_parent_getter)
    assert report["ok"] is True
    assert report["findings"] == []


def test_unresolved_reference_when_field_not_fk():
    """引用指向不存在的产出、且字段不是已知 FK 列 → unresolved_reference。"""
    plan = {
        "entities": [
            {
                "entity_id": 1,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("note", "hello", "literal")],
                "references": [{
                    "field": "note_ref",
                    "label": "ghost_label",
                    "status": "unresolved",
                }],
            },
        ],
    }
    report = audit_plan_completeness(plan, dependency_getter=_child_parent_getter)
    assert report["ok"] is False
    types = [f["type"] for f in report["findings"]]
    assert "unresolved_reference" in types
    assert "missing_producer_entity" not in types
    finding = next(f for f in report["findings"]
                   if f["type"] == "unresolved_reference")
    assert finding["label"] == "ghost_label"


def test_reference_and_placeholder_dedupe_single_finding():
    """同 (entity, field) 既在 references 又在 attributes 占位符 → 只报一次。"""
    plan = {
        "entities": [
            {
                "entity_id": 3,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("parent_id", "<new_parent_id>", "reference")],
                "references": [{
                    "field": "parent_id",
                    "label": "new_parent_id",
                    "status": "unresolved",
                }],
            },
        ],
    }
    report = audit_plan_completeness(plan, dependency_getter=_child_parent_getter)
    missing = [f for f in report["findings"]
               if f["type"] == "missing_producer_entity"]
    assert len(missing) == 1


def test_entity_key_reference_resolution():
    """references 用 entity_key 指向存在的实体 → resolved，不报。"""
    plan = {
        "entities": [
            {
                "entity_id": 1,
                "entity_key": "q1",
                "operation": "add",
                "target": {"table": "parent", "sheet": "Parent"},
                "attributes": [_attr("pid", "abc", "literal")],
            },
            {
                "entity_id": 2,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("name", "c", "literal")],
                "references": [{"entity_key": "q1", "field": "misc"}],
            },
        ],
    }

    def _getter(stem: str) -> list[dict]:
        return []

    report = audit_plan_completeness(plan, dependency_getter=_getter)
    assert report["ok"] is True


def test_dangling_entity_key_reference_unresolved():
    plan = {
        "entities": [
            {
                "entity_id": 2,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [_attr("name", "c", "literal")],
                "references": [{"entity_key": "does_not_exist", "field": "misc"}],
            },
        ],
    }
    report = audit_plan_completeness(plan, dependency_getter=lambda s: [])
    assert report["ok"] is False
    finding = next(f for f in report["findings"]
                   if f["type"] == "unresolved_reference")
    assert finding["entity_key"] == "does_not_exist"


def test_empty_plan_is_ok():
    assert audit_plan_completeness({}, dependency_getter=lambda s: [])["ok"] is True
    assert audit_plan_completeness(
        {"entities": []}, dependency_getter=lambda s: [])["ok"] is True


# ── 集成：真实 cascade_resolver（spawn_quest_entity → quest）─────────

def test_real_schema_missing_quest_producer():
    from agent.excel.core.cascade_resolver import get_add_dependencies

    deps = get_add_dependencies("spawn_quest_entity")
    quest_dep = next((d for d in deps if d.get("target_stem") == "quest"), None)
    if quest_dep is None:
        pytest.skip("table_relations.json 未含 spawn_quest_entity→quest，跳过集成用例")
    fk_col = quest_dep["source_col"]

    plan = {
        "entities": [
            {
                "entity_id": 1,
                "operation": "add",
                "target": {"table": "spawn_quest_entity",
                           "sheet": "SpawnQuestEntity"},
                "attributes": [_attr(fk_col, "<new_quest_id>", "reference")],
            },
        ],
    }
    report = audit_plan_completeness(plan)  # 默认走真实 getter
    assert report["ok"] is False
    finding = next(f for f in report["findings"]
                   if f["type"] == "missing_producer_entity")
    assert finding["missing_table"] == "quest"


def test_real_schema_complete_quest_chain_is_ok():
    from agent.excel.core.cascade_resolver import get_add_dependencies

    deps = get_add_dependencies("spawn_quest_entity")
    quest_dep = next((d for d in deps if d.get("target_stem") == "quest"), None)
    if quest_dep is None:
        pytest.skip("table_relations.json 未含 spawn_quest_entity→quest，跳过集成用例")
    fk_col = quest_dep["source_col"]

    plan = {
        "entities": [
            {
                "entity_id": 1,
                "operation": "add",
                "target": {"table": "quest", "sheet": "Quest"},
                "attributes": [_attr("quest_id", "<new_quest_id>", "reference")],
                "produces": "new_quest_id",
            },
            {
                "entity_id": 2,
                "operation": "add",
                "target": {"table": "spawn_quest_entity",
                           "sheet": "SpawnQuestEntity"},
                "attributes": [_attr(fk_col, "<new_quest_id>", "reference")],
                "references": [{"field": fk_col, "label": "new_quest_id"}],
            },
        ],
    }
    report = audit_plan_completeness(plan)
    assert report["ok"] is True, report["findings"]


def test_step1_audit_surfaces_completeness_findings_report_only():
    """接线验证：_build_step1_audit 把完整性发现纳入报告，但不改 ok 门禁语义。"""
    from agent.excel.core.pipeline.step1_parse_subagent import _build_step1_audit
    from agent.excel.core.cascade_resolver import get_add_dependencies
    from agent.excel.parser.nl_parser import NLIntent

    deps = get_add_dependencies("spawn_quest_entity")
    quest_dep = next((d for d in deps if d.get("target_stem") == "quest"), None)
    if quest_dep is None:
        pytest.skip("table_relations.json 未含 spawn_quest_entity→quest，跳过")
    fk_col = quest_dep["source_col"]

    intents = [
        NLIntent(
            action="add",
            table_hint="spawn_quest_entity",
            sheet_hint="SpawnQuestEntity",
            extras={"fields": {fk_col: "<new_quest_id>"}},
        ),
    ]
    audit = _build_step1_audit(intents)
    assert "plan_completeness" in audit
    assert audit["plan_completeness"]["ok"] is False
    assert audit["metrics"]["completeness_missing_producer_count"] >= 1


def _incomplete_step1_audit():
    return {
        "plan_completeness": {
            "ok": False,
            "findings": [{
                "type": "missing_producer_entity",
                "entity_id": 1,
                "table": "spawn_quest_entity",
                "field": "quest_id",
                "missing_table": "quest",
                "label": "new_quest_id",
                "severity": "hard",
            }],
            "suggestions": [{
                "type": "add_shell_entity", "table": "quest",
                "for_entity": 1, "for_field": "quest_id",
            }],
            "hard_count": 1, "finding_count": 1,
        }
    }


def test_step2_completeness_soft_by_default(monkeypatch):
    monkeypatch.delenv("CODEMAKER_PLAN_COMPLETENESS_GATE", raising=False)
    from agent.excel.core.pipeline.step2_validate_subagent import Step2ValidateSubAgent

    errs = Step2ValidateSubAgent._plan_completeness_errors(_incomplete_step1_audit())
    assert len(errs) == 1
    assert errs[0].error_type == "completeness_missing_producer_entity"
    assert errs[0].is_hard is False           # 默认不阻断
    assert errs[0].suggestion                 # 附确定性修复建议
    assert "quest" in errs[0].message


def test_step2_completeness_hard_when_gated(monkeypatch):
    monkeypatch.setenv("CODEMAKER_PLAN_COMPLETENESS_GATE", "1")
    from agent.excel.core.pipeline.step2_validate_subagent import Step2ValidateSubAgent

    errs = Step2ValidateSubAgent._plan_completeness_errors(_incomplete_step1_audit())
    assert len(errs) == 1
    assert errs[0].is_hard is True            # 开关打开后阻断


def test_step2_completeness_noop_without_findings():
    from agent.excel.core.pipeline.step2_validate_subagent import Step2ValidateSubAgent

    assert Step2ValidateSubAgent._plan_completeness_errors(None) == []
    assert Step2ValidateSubAgent._plan_completeness_errors(
        {"plan_completeness": {"findings": []}}) == []



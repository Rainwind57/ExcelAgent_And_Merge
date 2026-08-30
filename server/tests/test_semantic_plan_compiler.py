from __future__ import annotations

from server.agent.excel.core.pipeline.semantic_plan import (
    compile_semantic_plan_to_intents,
    compile_semantic_plan_to_operation_items,
)


def test_compile_semantic_plan_add_with_references():
    plan = {
        "version": 1,
        "entities": [
            {
                "entity_id": 1,
                "operation": "add",
                "target": {"table": "parent", "sheet": "Parent"},
                "attributes": [
                    {"name": "id", "value": "<new_parent_id>"},
                    {"name": "name", "value": "P"},
                ],
                "produces": "new_parent_id",
                "raw": "make parent",
            },
            {
                "entity_id": 2,
                "operation": "add",
                "target": {"table": "child", "sheet": "Child"},
                "attributes": [
                    {"name": "parent_id", "value": "<new_parent_id>"},
                    {"name": "name", "value": "C"},
                ],
                "references": [
                    {"field": "parent_id", "label": "new_parent_id"},
                ],
            },
        ],
    }

    intents, report = compile_semantic_plan_to_intents(plan)

    assert report == {
        "ok": True,
        "entity_count": 2,
        "intent_count": 2,
        "issue_count": 0,
        "hard_count": 0,
        "issues": [],
    }
    assert intents[0].table_hint == "parent"
    assert intents[0].produces_label == "new_parent_id"
    assert intents[0].extras["fields"]["id"] == "<new_parent_id>"
    assert intents[1].consumes_labels == ["new_parent_id"]
    assert intents[1].extras["semantic_entity_id"] == 2


def test_compile_semantic_plan_set_single_attribute_as_target():
    plan = {
        "entities": [{
            "operation": "set",
            "target": {"table": "activity", "sheet": "Activity"},
            "locator": {"field": "id", "value": 3060},
            "attributes": [{"name": "name", "value": "九霄论剑"}],
        }],
    }

    intents, report = compile_semantic_plan_to_intents(plan)

    assert report["ok"] is True
    assert intents[0].action == "set"
    assert intents[0].locator_field == "id"
    assert intents[0].locator_value == 3060
    assert intents[0].target_field == "name"
    assert intents[0].value == "九霄论剑"


def test_compile_semantic_plan_reports_structural_issues():
    plan = {
        "entities": [{
            "operation": "merge",
            "target": {"table": "x", "sheet": "X"},
            "attributes": [
                {"name": "name", "value": "A"},
                {"name": "name", "value": "B"},
                {"value": "missing name"},
            ],
        }],
    }

    intents, report = compile_semantic_plan_to_intents(plan)

    assert len(intents) == 1
    assert intents[0].action == "add"
    issue_types = {x["type"] for x in report["issues"]}
    assert issue_types == {
        "invalid_operation",
        "duplicate_attribute",
        "attribute_name_missing",
    }
    assert report["hard_count"] == 3
    assert report["ok"] is False


def test_compile_semantic_plan_rewrites_reference_field_value():
    plan = {
        "entities": [{
            "operation": "add",
            "target": {"table": "school_spirit", "sheet": "SchoolSpirit"},
            "attributes": [
                {"name": "school_ability_id", "value": "太虚剑意"},
                {"name": "spirit_id", "value": "金灵根"},
            ],
            "references": [
                {"field": "school_ability_id", "label": "new_ability_1_id"},
                {"field": "spirit_id", "label": "existing_spirit_gold_id"},
            ],
        }],
    }

    intents, report = compile_semantic_plan_to_intents(plan)

    assert report["ok"] is True
    assert report["hard_count"] == 0
    assert report["issue_count"] == 2
    assert {x["type"] for x in report["issues"]} == {
        "reference_field_value_rewritten",
    }
    fields = intents[0].extras["fields"]
    assert fields["school_ability_id"] == "<new_ability_1_id>"
    assert fields["spirit_id"] == "<existing_spirit_gold_id>"
    assert intents[0].consumes_labels == [
        "new_ability_1_id",
        "existing_spirit_gold_id",
    ]


def test_compile_semantic_plan_to_operation_items_for_decompose_adapter():
    plan = {
        "entities": [{
            "entity_id": 1,
            "operation": "add",
            "target": {"table": "mail", "sheet": "MailTemplate"},
            "attributes": [
                {"name": "template_id", "value": "<new_template_id>"},
                {"name": "title", "value": "开服公告"},
            ],
            "produces": "new_template_id",
        }],
    }

    items, report = compile_semantic_plan_to_operation_items(plan)

    assert report["ok"] is True
    assert report["operation_item_count"] == 1
    assert items == [{
        "table": "mail",
        "sheet": "MailTemplate",
        "action": "add",
        "fields": {
            "template_id": "<new_template_id>",
            "title": "开服公告",
        },
        "produces": "new_template_id",
        "consumes": {},
        "locator_field": "",
        "locator_value": "",
        "locator_fields": [],
        "locator_values": [],
    }]


def test_compile_semantic_plan_to_operation_items_preserves_reference_fields():
    plan = {
        "entities": [{
            "operation": "add",
            "target": {"table": "mail", "sheet": "GlobalMail"},
            "attributes": [
                {"name": "template_id", "value": "月华庆典开启"},
            ],
            "references": [
                {"field": "template_id", "label": "new_template_id"},
            ],
        }],
    }

    items, report = compile_semantic_plan_to_operation_items(plan)

    assert report["ok"] is True
    assert items[0]["fields"]["template_id"] == "<new_template_id>"
    assert items[0]["consumes"] == {"template_id": "new_template_id"}


def test_compile_semantic_plan_infers_consumes_from_attribute_placeholders():
    plan = {
        "entities": [{
            "operation": "add",
            "target": {"table": "mail", "sheet": "GlobalMail"},
            "attributes": [
                {"name": "template_id", "value": "<new_template_id>"},
                {"name": "global_id", "value": 21},
            ],
        }],
    }

    intents, report = compile_semantic_plan_to_intents(plan)
    items, _ = compile_semantic_plan_to_operation_items(plan)

    assert report["ok"] is True
    assert intents[0].consumes_labels == ["new_template_id"]
    assert items[0]["consumes"] == {"template_id": "new_template_id"}


def test_compile_semantic_plan_remaps_attributes_with_schema_getter():
    plan = {
        "entities": [{
            "operation": "add",
            "target": {"table": "mail", "sheet": "MailTemplate"},
            "attributes": [
                {"name": "模板ID", "value": "<new_template_id>"},
                {"name": "标题", "value": "open notice"},
            ],
            "produces": "new_template_id",
        }],
    }

    def schema_getter(_entity):
        return ["模板ID", "标题"], ["template_id:int", "title:string"]

    intents, report = compile_semantic_plan_to_intents(
        plan, schema_getter=schema_getter)
    items, _ = compile_semantic_plan_to_operation_items(
        plan, schema_getter=schema_getter)

    assert report["ok"] is True
    assert report["hard_count"] == 0
    assert any(x["type"] == "attributes_remapped" for x in report["issues"])
    assert intents[0].extras["fields"] == {
        "template_id": "<new_template_id>",
        "title": "open notice",
    }
    assert items[0]["fields"]["template_id"] == "<new_template_id>"
    assert items[0]["consumes"] == {}


def test_compile_semantic_plan_skips_self_reference_consumes():
    plan = {
        "entities": [{
            "operation": "add",
            "target": {"table": "mail", "sheet": "MailTemplate"},
            "attributes": [
                {"name": "template_id", "value": "<new_template_id>"},
                {"name": "title", "value": "open notice"},
            ],
            "produces": "new_template_id",
            "references": [
                {"field": "template_id", "label": "new_template_id"},
            ],
        }],
    }

    intents, report = compile_semantic_plan_to_intents(plan)
    items, _ = compile_semantic_plan_to_operation_items(plan)

    assert report["ok"] is True
    assert intents[0].consumes_labels == []
    assert items[0]["consumes"] == {}

from __future__ import annotations

import re
from typing import Any

from ...parser.nl_parser import NLIntent
from .schema_mapper import remap_fields_to_schema

_PLACEHOLDER_RE = re.compile(r"<\s*([^>]+?)\s*>")


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _label(value: Any) -> str:
    return str(value or "").strip().strip("<>").strip()


def _attr_name(attr: dict) -> str:
    return str(attr.get("name") or attr.get("field") or attr.get("col") or "").strip()


def _attr_value(attr: dict) -> Any:
    if "value" in attr:
        return attr.get("value")
    return attr.get("val")


def _collect_placeholder_refs(value: Any) -> list[str]:
    refs: list[str] = []
    seen: set[str] = set()

    def visit(v: Any) -> None:
        if isinstance(v, str):
            for match in _PLACEHOLDER_RE.finditer(v):
                label = _label(match.group(1))
                if label and label.lower() != "auto" and label not in seen:
                    seen.add(label)
                    refs.append(label)
        elif isinstance(v, dict):
            for nested in v.values():
                visit(nested)
        elif isinstance(v, (list, tuple, set)):
            for nested in v:
                visit(nested)

    visit(value)
    return refs


def compile_semantic_plan_to_intents(
        semantic_plan: dict, schema_getter=None) -> tuple[list[NLIntent], dict]:
    plan = _as_dict(semantic_plan)
    entities = _as_list(plan.get("entities"))
    intents: list[NLIntent] = []
    issues: list[dict] = []

    def add_issue(issue_type: str, idx: int, severity: str = "hard",
                  **extra: Any) -> None:
        row = {"type": issue_type, "idx": idx, "severity": severity}
        row.update(extra)
        issues.append(row)

    for pos, entity in enumerate(entities, start=1):
        if not isinstance(entity, dict):
            add_issue("entity_not_object", pos)
            continue
        action = str(entity.get("operation") or entity.get("action") or "add").strip().lower()
        if action not in {"add", "set", "delete", "get", "col"}:
            add_issue("invalid_operation", pos, operation=action)
            action = "add"
        target = _as_dict(entity.get("target"))
        table = target.get("table") or entity.get("table")
        sheet = target.get("sheet") or entity.get("sheet")
        if not table:
            add_issue("target_table_missing", pos)
        locator = _as_dict(entity.get("locator"))
        attrs = _as_list(entity.get("attributes"))
        fields: dict[str, Any] = {}
        for attr in attrs:
            if not isinstance(attr, dict):
                continue
            name = _attr_name(attr)
            if not name:
                add_issue("attribute_name_missing", pos)
                continue
            if name in fields:
                add_issue("duplicate_attribute", pos, attribute=name)
            fields[name] = _attr_value(attr)
        if action == "add" and not fields:
            add_issue("add_attributes_missing", pos, severity="soft")

        target_field = entity.get("target_field")
        value = entity.get("value")
        if action == "set" and not target_field and len(fields) == 1:
            target_field, value = next(iter(fields.items()))
        if action == "set" and not target_field:
            add_issue("set_target_missing", pos)
        locator_has_value = bool(locator.get("field") and locator.get("value") not in (None, ""))
        locator_has_values = bool(_as_list(locator.get("fields")) and _as_list(locator.get("values")))
        if action in {"set", "delete", "get"} and not (locator_has_value or locator_has_values):
            add_issue("locator_missing", pos, severity="soft")

        produces = _label(entity.get("produces"))
        consumes: list[str] = []
        for value in fields.values():
            for label in _collect_placeholder_refs(value):
                if label != produces and label not in consumes:
                    consumes.append(label)
        for ref in _as_list(entity.get("references")):
            if not isinstance(ref, dict):
                continue
            label = _label(ref.get("label"))
            if label and label != produces and label not in consumes:
                consumes.append(label)
            field = str(ref.get("field") or "").strip()
            if field and label:
                expected = f"<{label}>"
                current = fields.get(field)
                if current != expected:
                    if current not in (None, ""):
                        add_issue(
                            "reference_field_value_rewritten",
                            pos,
                            severity="soft",
                            field=field,
                            label=label,
                            old_value=current,
                        )
                    fields[field] = expected
        for label in _as_list(entity.get("consumes")):
            label = _label(label)
            if label and label != produces and label not in consumes:
                consumes.append(label)

        if schema_getter is not None and fields:
            try:
                headers, type_row = schema_getter(entity)
            except Exception as exc:  # noqa: BLE001
                add_issue("schema_getter_failed", pos, error=type(exc).__name__)
                headers, type_row = [], []
            fields, mapping_report = remap_fields_to_schema(fields, headers, type_row)
            if mapping_report.get("renames"):
                add_issue(
                    "attributes_remapped",
                    pos,
                    severity="soft",
                    renames=mapping_report.get("renames"),
                )
            if mapping_report.get("collisions"):
                add_issue(
                    "attribute_mapping_collision",
                    pos,
                    collisions=mapping_report.get("collisions"),
                )
            if mapping_report.get("unmapped"):
                add_issue(
                    "attributes_unmapped",
                    pos,
                    severity="soft",
                    attributes=mapping_report.get("unmapped"),
                )

        extras = dict(_as_dict(entity.get("extras")))
        if fields:
            extras["fields"] = fields
        entity_id = entity.get("entity_id")
        if entity_id is not None:
            extras["semantic_entity_id"] = entity_id
        if produces:
            extras["produces"] = produces

        intent = NLIntent(
            action=action,
            table_hint=table,
            sheet_hint=sheet,
            locator_field=locator.get("field"),
            locator_value=locator.get("value"),
            locator_fields=list(_as_list(locator.get("fields"))),
            locator_values=list(_as_list(locator.get("values"))),
            target_field=target_field,
            value=value,
            raw=str(entity.get("raw") or ""),
            extras=extras,
            produces_label=produces or None,
            consumes_labels=consumes,
            source="llm_decompose",
        )
        intents.append(intent)

    hard_count = sum(1 for issue in issues if issue.get("severity") == "hard")
    report = {
        "ok": hard_count == 0,
        "entity_count": len(entities),
        "intent_count": len(intents),
        "issue_count": len(issues),
        "hard_count": hard_count,
        "issues": issues,
    }
    return intents, report


def compile_semantic_plan_to_operation_items(
        semantic_plan: dict, schema_getter=None) -> tuple[list[dict], dict]:
    intents, report = compile_semantic_plan_to_intents(
        semantic_plan, schema_getter=schema_getter)
    entities = _as_list(_as_dict(semantic_plan).get("entities"))
    items: list[dict] = []
    for idx, it in enumerate(intents):
        entity = entities[idx] if idx < len(entities) and isinstance(entities[idx], dict) else {}
        consumes: dict[str, str] = {}
        for ref in _as_list(entity.get("references")):
            if not isinstance(ref, dict):
                continue
            field = str(ref.get("field") or "").strip()
            label = _label(ref.get("label"))
            if field and label and label != (it.produces_label or ""):
                consumes[field] = label
        for field, value in dict((it.extras or {}).get("fields") or {}).items():
            labels = _collect_placeholder_refs(value)
            if labels:
                label = labels[0]
                if label != (it.produces_label or ""):
                    consumes.setdefault(str(field), label)
        if not consumes:
            consumes = {
                label: label
                for label in (it.consumes_labels or [])
                if label != (it.produces_label or "")
            }
        item = {
            "table": it.table_hint or "",
            "sheet": it.sheet_hint or "",
            "action": it.action or "add",
            "fields": dict((it.extras or {}).get("fields") or {}),
            "produces": it.produces_label or "",
            "consumes": consumes,
            "locator_field": it.locator_field or "",
            "locator_value": it.locator_value if it.locator_value is not None else "",
            "locator_fields": list(it.locator_fields or []),
            "locator_values": list(it.locator_values or []),
        }
        items.append(item)
    out_report = dict(report)
    out_report["operation_item_count"] = len(items)
    return items, out_report

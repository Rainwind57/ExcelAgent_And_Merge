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


def _compile_entity_keys(plan: dict) -> tuple[dict, list[dict]]:
    """§9.3：把 entity_key 引用编译为 produces 占位符协议（代码层唯一编译点）。

    输入语义计划，扫描 entities 里的 entity_key / entity_id / produces：
      - 有 produces → 该实体产出占位符，登记 label。
      - 有 entity_key 但无 produces → 若其为「新增实体」且被其他实体引用，
        分配稳定 produces label（new_<stem>_<key>_id），登记 key→label。
    产出：
      index: {key_str: {"idx": pos, "label": produces_label, "stem": table}}
      report: [{type, idx, severity, extra}]（重复 key / 缺 produces 提示）。

    这是纯数据变换（0 LLM），不绑定业务表；LLM 只需产出 entity_key + 中文名，
    不再随意编 placeholder 名称，减少占位符漂移导致的连锁失败。
    """
    entities = _as_list(plan.get("entities"))
    index: dict[str, dict] = {}
    report: list[dict] = []
    consumed_keys: set[str] = set()

    # 第一遍：登记显式 produces / entity_key
    for pos, entity in enumerate(entities, start=1):
        if not isinstance(entity, dict):
            continue
        key = str(entity.get("entity_key") or entity.get("entity_id") or "").strip()
        produces = _label(entity.get("produces"))
        target = _as_dict(entity.get("target"))
        stem = str(target.get("table") or entity.get("table") or "").strip()
        if key:
            if key in index:
                report.append({
                    "type": "duplicate_entity_key", "idx": pos,
                    "severity": "soft", "extra": {"entity_key": key},
                })
                continue
            if produces:
                index[key] = {"idx": pos, "label": produces, "stem": stem}
        # 收集被引用的 entity_key（references.entity_key）
        for ref in _as_list(entity.get("references")):
            if not isinstance(ref, dict):
                continue
            rk = str(ref.get("entity_key") or ref.get("ref") or "").strip()
            if rk:
                consumed_keys.add(rk)

    # 第二遍：被引用的新增实体若未显式 produces → 分配稳定 label
    for pos, entity in enumerate(entities, start=1):
        if not isinstance(entity, dict):
            continue
        key = str(entity.get("entity_key") or "").strip()
        if not key or key in index:
            continue
        op = str(entity.get("operation") or entity.get("action") or "add").strip().lower()
        if op != "add":
            continue  # set/delete/get 不产新 ID，不分配 produces
        target = _as_dict(entity.get("target"))
        stem = str(target.get("table") or entity.get("table") or "").strip()
        if key in consumed_keys or any(r.get("entity_key") == key
                                       for e2 in entities if isinstance(e2, dict)
                                       for r in _as_list(e2.get("references"))
                                       if isinstance(r, dict)):
            label = f"new_{stem}_{key}_id" if stem else f"new_{key}_id"
            index[key] = {"idx": pos, "label": label, "stem": stem}
            report.append({
                "type": "entity_key_compiled", "idx": pos,
                "severity": "soft", "extra": {"entity_key": key, "label": label},
            })
    return index, report


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

    # §9.3 entity reference 编译：LLM 输出 entity_key（稳定实体引用）+ 中文名，
    # 代码层把「同实体多表配置」的引用统一编译为 produces 占位符。编译器产出
    # {key, idx, produces_label} 映射，供 references 里 label 缺失时回填。
    # 禁止 LLM 直接控制占位符协议（<resolved_from_xxx> 等），此处是唯一编译点。
    _entity_key_index, _entity_compile_report = _compile_entity_keys(plan)
    key_to_label = {str(k): v["label"] for k, v in _entity_key_index.items()}
    for issue in _entity_compile_report:
        add_issue(issue["type"], issue["idx"], issue["severity"], **issue.get("extra", {}))

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
        _backfilled_loc = None
        if action == "set" and not target_field and len(fields) >= 2:
            _id_keys = [k for k in fields
                        if str(k).split(":")[0].strip().lower().split(".")[-1].replace("_","").endswith("id")
                        or "编号" in str(k) or str(k) in ("name","名称","名字")]
            if _id_keys:
                _loc_key = _id_keys[0]
                _backfilled_loc = (_loc_key, fields[_loc_key])
                target_field, value = _loc_key, fields[_loc_key]
                add_issue("set_target_missing", pos, severity="soft",
                          note="locator backfilled from fields")
            else:
                add_issue("set_target_missing", pos)
        locator_has_value = bool(locator.get("field") and locator.get("value") not in (None, ""))
        locator_has_values = bool(_as_list(locator.get("fields")) and _as_list(locator.get("values")))
        if _backfilled_loc and not locator_has_value:
            locator = {"field": _backfilled_loc[0], "value": _backfilled_loc[1]}
            locator_has_value = True
        if action in {"set", "delete", "get"} and not (locator_has_value or locator_has_values):
            add_issue("locator_missing", pos, severity="soft")

        produces = _label(entity.get("produces"))
        # §9.3：entity_key 被引用但未显式 produces → 采用编译器分配的稳定 label
        if not produces:
            _ek = str(entity.get("entity_key") or "").strip()
            if _ek and _ek in key_to_label:
                produces = key_to_label[_ek]
        consumes: list[str] = []
        for value in fields.values():
            for label in _collect_placeholder_refs(value):
                if label != produces and label not in consumes:
                    consumes.append(label)
        for ref in _as_list(entity.get("references")):
            if not isinstance(ref, dict):
                continue
            # §9.3 entity_key 引用：LLM 写 references 里 entity_key 时，
            # 由编译器把 key 映射到对应实体的 produces_label（代码层编译
            # 成 placeholder，LLM 不直接控制占位符协议）。
            label = _label(ref.get("label"))
            if not label:
                _ref_key = str(ref.get("entity_key") or ref.get("ref") or "").strip()
                if _ref_key:
                    _compiled = key_to_label.get(_ref_key) or key_to_label.get(_ref_key.lstrip("#"))
                    if _compiled:
                        label = _compiled
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

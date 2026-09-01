"""Phase 1.5：语义计划完整性审计（0 LLM，纯确定性）。

定位（路线图 plan-first §Phase 1.5）：在 LLM 产出 semantic_plan（Phase 1）之后、
编译成 intents（compile_semantic_plan_to_intents）之前，用 table_relations 关系图
对计划做 **FK 闭包完整性压力测试**，把"漏表 / 悬空引用"从静默丢失变成诚实错误。

两类诚实错误（均 hard）：
  1. missing_producer_entity —— consumer（add 实体）通过 FK 引用了某 producer 表，
     但计划里没有对应的 add 实体产出该表的新行。即"漏了一张表"。
     判据是外键结构（source_col → target_stem），不绑任何业务词。
  2. unresolved_reference —— 引用（references / 占位符）指向的 entity_key / 产出标签
     在计划里不存在任何实体产出。即"连线连到了空气"。

低误报原则：只有当 FK 源列在该实体中表现为"占位符引用"（kind=reference，即期望
一个由本计划新产出的 id）时才判 missing_producer；若该列填的是字面 id（引用一条
已存在的行），不需要 producer，不报。

设计约束：
  - 0 LLM、无副作用、可离线用真实 schema 确定性验证。
  - 依赖注入：dependency_getter 默认取 cascade_resolver.get_add_dependencies，
    单测可注入桩，不依赖真实 json 文件。
  - 只审计、不修改计划。可确定性补壳的候选以 suggestions 形式返回（非侵入），
    由调用方决定是否落地。
"""
from __future__ import annotations

import re
from typing import Any, Callable, Optional

_PLACEHOLDER_RE = re.compile(r"<\s*([^>]+?)\s*>")

DependencyGetter = Callable[[str], list[dict]]


def _as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def _label(value: Any) -> str:
    return str(value or "").strip().strip("<>").strip()


def _entity_table(entity: dict) -> str:
    target = _as_dict(entity.get("target"))
    return str(target.get("table") or entity.get("table") or "").strip()


def _entity_operation(entity: dict) -> str:
    return str(
        entity.get("operation") or entity.get("action") or "add"
    ).strip().lower()


def _entity_key(entity: dict) -> str:
    """实体的稳定标识：entity_key 优先，回退 entity_id（转字符串）。"""
    ek = str(entity.get("entity_key") or "").strip()
    if ek:
        return ek
    eid = entity.get("entity_id")
    return str(eid).strip() if eid is not None else ""


def _attr_name(attr: dict) -> str:
    return str(
        attr.get("name") or attr.get("field") or attr.get("col") or ""
    ).strip()


def _attr_is_reference(attr: dict) -> bool:
    """该属性是否为"占位符引用"（期望由本计划产出的 id 回填）。

    优先信任上游标注的 kind==reference（_build_step1_semantic_plan 会写）；
    否则回退到值里是否含 <...> 占位符（排除 <auto>）。
    """
    kind = str(attr.get("kind") or "").strip().lower()
    if kind == "reference":
        return True
    if kind and kind != "reference":
        # 明确标注为字面/数值等 → 非引用
        return kind not in {"literal", "int", "float", "bool", "empty"} and bool(
            _placeholder_labels(attr.get("value"))
        )
    return bool(_placeholder_labels(attr.get("value")))


def _placeholder_labels(value: Any) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    if isinstance(value, str):
        for m in _PLACEHOLDER_RE.finditer(value):
            lab = _label(m.group(1))
            if lab and lab.lower() != "auto" and lab not in seen:
                seen.add(lab)
                out.append(lab)
    return out


def _resolve_default_getter() -> DependencyGetter:
    from ..cascade_resolver import get_add_dependencies
    return get_add_dependencies


def audit_plan_completeness(
    semantic_plan: dict,
    dependency_getter: Optional[DependencyGetter] = None,
) -> dict:
    """对 semantic_plan 做 FK 闭包完整性审计。

    Args:
        semantic_plan: Phase 1 / step1 产出的语义计划 dict（含 entities）。
        dependency_getter: stem -> [{target_stem, source_col, target_col, sheet}]。
            默认取 cascade_resolver.get_add_dependencies。注入桩以脱离真实 json 单测。

    Returns:
        {
          "ok": bool,                 # 无 hard finding
          "findings": [ {...} ],      # 诚实错误清单
          "finding_count": int,
          "hard_count": int,
          "suggestions": [ {...} ],   # 可确定性补壳的候选（非侵入）
          "entity_count": int,
        }
    """
    getter = dependency_getter or _resolve_default_getter()
    plan = _as_dict(semantic_plan)
    entities = _as_list(plan.get("entities"))

    findings: list[dict] = []
    suggestions: list[dict] = []

    # ── 索引：产出标签 / 实体键 / add 实体的目标表 ──────────────
    produced_labels: set[str] = set()
    entity_keys: set[str] = set()
    add_tables: dict[str, list] = {}
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        prod = _label(entity.get("produces"))
        if prod:
            produced_labels.add(prod)
        ek = _entity_key(entity)
        if ek:
            entity_keys.add(ek)
        if _entity_operation(entity) == "add":
            stem = _entity_table(entity)
            if stem:
                add_tables.setdefault(stem, []).append(entity.get("entity_id"))

    def _ref_resolved(ref: dict) -> bool:
        if str(ref.get("status") or "").strip().lower() == "resolved":
            return True
        lab = _label(ref.get("label"))
        if lab and lab in produced_labels:
            return True
        rk = str(ref.get("entity_key") or ref.get("ref") or "").strip().lstrip("#")
        if rk and rk in entity_keys:
            return True
        return False

    # dedupe：同 (entity_id, field) 只报一次（Pass A / Pass B 之间）
    reported_fk: set[tuple] = set()

    # ── Pass A：显式 references 完整性 ──────────────────────────
    for entity in entities:
        if not isinstance(entity, dict):
            continue
        eid = entity.get("entity_id")
        stem = _entity_table(entity)
        dep_by_col = {
            str(dep.get("source_col") or "").strip(): str(
                dep.get("target_stem") or ""
            ).strip()
            for dep in (getter(stem) if stem else [])
            if isinstance(dep, dict)
        }
        for ref in _as_list(entity.get("references")):
            if not isinstance(ref, dict) or _ref_resolved(ref):
                continue
            field = str(ref.get("field") or "").strip()
            lab = _label(ref.get("label"))
            rk = str(ref.get("entity_key") or ref.get("ref") or "").strip()
            target_stem = dep_by_col.get(field) if field else ""
            if target_stem and target_stem not in add_tables:
                reported_fk.add((eid, field))
                findings.append({
                    "type": "missing_producer_entity",
                    "entity_id": eid,
                    "table": stem,
                    "field": field,
                    "missing_table": target_stem,
                    "label": lab,
                    "severity": "hard",
                })
                suggestions.append({
                    "type": "add_shell_entity",
                    "table": target_stem,
                    "reason": "fk_reference_no_producer",
                    "for_entity": eid,
                    "for_field": field,
                })
            else:
                findings.append({
                    "type": "unresolved_reference",
                    "entity_id": eid,
                    "table": stem,
                    "field": field or None,
                    "label": lab,
                    "entity_key": rk or None,
                    "severity": "hard",
                })

    # ── Pass B：FK 结构闭包（属性占位符但计划无 producer 表）────
    for entity in entities:
        if not isinstance(entity, dict) or _entity_operation(entity) != "add":
            continue
        eid = entity.get("entity_id")
        stem = _entity_table(entity)
        if not stem:
            continue
        deps = getter(stem)
        if not deps:
            continue
        attr_by_name = {
            _attr_name(a): a
            for a in _as_list(entity.get("attributes"))
            if isinstance(a, dict) and _attr_name(a)
        }
        seen_targets: set[str] = set()
        for dep in deps:
            if not isinstance(dep, dict):
                continue
            col = str(dep.get("source_col") or "").strip()
            target_stem = str(dep.get("target_stem") or "").strip()
            if not target_stem or target_stem == stem:
                continue
            if (eid, col) in reported_fk or target_stem in seen_targets:
                continue
            attr = attr_by_name.get(col)
            if attr is None or not _attr_is_reference(attr):
                continue  # 无该列 / 填字面 id → 引用已存在行，不需 producer
            # 占位符引用：若其标签已被某实体产出，则 producer 存在，不报
            labs = _placeholder_labels(attr.get("value"))
            if any(lab in produced_labels for lab in labs):
                continue
            if target_stem in add_tables:
                continue
            seen_targets.add(target_stem)
            reported_fk.add((eid, col))
            findings.append({
                "type": "missing_producer_entity",
                "entity_id": eid,
                "table": stem,
                "field": col,
                "missing_table": target_stem,
                "label": labs[0] if labs else "",
                "severity": "hard",
            })
            suggestions.append({
                "type": "add_shell_entity",
                "table": target_stem,
                "reason": "fk_placeholder_no_producer",
                "for_entity": eid,
                "for_field": col,
            })

    hard_count = sum(1 for f in findings if f.get("severity") == "hard")
    return {
        "ok": hard_count == 0,
        "findings": findings,
        "finding_count": len(findings),
        "hard_count": hard_count,
        "suggestions": suggestions,
        "entity_count": len(entities),
    }


__all__ = ["audit_plan_completeness"]

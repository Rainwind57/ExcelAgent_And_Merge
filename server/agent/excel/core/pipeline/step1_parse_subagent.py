"""4-Step V2 Step1 Parse SubAgent（§设计 S2）。

职责（严格限定）：
  - 输入分析、匹配表格、指令初形成。
  - split_multi_intent 分段（0 LLM）→ 每段独立 locate + decompose_segment（段小→快+可靠）
  - produces 推断（0 LLM）
  - 段级覆盖率对账：每段 ≥1 intent，0 条段重跑（便宜），仍空报 StepError（soft）

严禁：
  - AI 校验/字段校验/冲突处理（属 Step2）
  - 执行/写入（属 Step3）
  - 汇总/反模式归纳（属 Step4）

复用现有 ParseAgent（已含分段 + 段级对账），包装为统一 StepResult。
S1 阶段只做"包装 + 错误归属固定到 step1_parse"，ParseAgent 内部逻辑不动。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from collections import Counter
from typing import Any

from ...parse_agent import ParseAgent
from .contracts import STEP1_PARSE, StepContext, StepError, StepHardError, StepResult
from .semantic_plan import compile_semantic_plan_to_intents
from .plan_completeness import audit_plan_completeness

logger = logging.getLogger(__name__)

_ACTION_CN = {"add": "新增", "set": "修改", "delete": "删除", "get": "查询",
              "col": "列操作"}


def _format_intent_human(it: Any) -> str:
    """把 NLIntent 转成人类可读中文描述，供 Step1 结束打印对照。"""
    act = _ACTION_CN.get(getattr(it, "action", ""), getattr(it, "action", "?"))
    tbl = getattr(it, "table_hint", "") or "?"
    sheet = getattr(it, "sheet_hint", "") or ""
    loc = f"{tbl}.{sheet}" if sheet else tbl
    parts = [f"{act} {loc}"]
    # 单主键定位
    lf = getattr(it, "locator_field", None)
    lv = getattr(it, "locator_value", None)
    if lf and lv not in (None, ""):
        parts.append(f"定位 {lf}={lv}")
    # 复合主键定位
    lfs = getattr(it, "locator_fields", None) or []
    lvs = getattr(it, "locator_values", None) or []
    if lfs and lvs and len(lfs) == len(lvs):
        kv = ", ".join(f"{f}={v}" for f, v in zip(lfs, lvs))
        parts.append(f"定位 {kv}")
    # set 目标字段
    if getattr(it, "action", "") == "set" and getattr(it, "target_field", None):
        parts.append(f"{it.target_field}→{getattr(it, 'value', None)}")
    # add 写入字段
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if getattr(it, "action", "") == "add" and isinstance(fields, dict) and fields:
        kv = ", ".join(f"{k}={v}" for k, v in list(fields.items())[:12])
        if len(fields) > 12:
            kv += f", …(共{len(fields)}列)"
        parts.append(f"写入 {kv}")
    # produces/consumes 依赖标注
    if getattr(it, "produces_label", None):
        parts.append(f"产出 <{it.produces_label}>")
    if getattr(it, "consumes_labels", None):
        parts.append("消费 " + ", ".join(f"<{c}>" for c in it.consumes_labels))
    return "，".join(parts)


def _disp_w(s: str) -> int:
    """字符串显示宽度（中文/全角算 2 列，ASCII 算 1 列），供表格对齐。"""
    import unicodedata
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
               for ch in str(s))


def _pad(s: str, width: int) -> str:
    """按显示宽度左对齐填充。"""
    s = str(s)
    return s + " " * max(0, width - _disp_w(s))


def _clip(s: str, width: int) -> str:
    """按显示宽度截断超长单元格（尾部加省略号）。"""
    import unicodedata
    s = str(s)
    if _disp_w(s) <= width:
        return s
    out = ""
    w = 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width - 1:
            break
        out += ch
        w += cw
    return out + "…"


def _intent_fields(it: Any) -> list[dict]:
    """NLIntent → 字段键值对列表 [{col, value, type}]，供前端渲染「字段=值」表格。

    每个字段独立一行，值不截断（前端表格按列宽滚动展示），这样校验环节
    能看清每个值落在哪个字段、类型/匹配问题出在哪一行哪一列。
    """
    out: list[dict] = []
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if isinstance(fields, dict) and fields:
        for k, v in fields.items():
            if isinstance(v, (dict, list)):
                v = json.dumps(v, ensure_ascii=False)
            out.append({"col": str(k), "value": "" if v is None else str(v)})
    return out


def _intent_cells(it: Any) -> tuple:
    """NLIntent → 表格四元组 (操作, 表/Sheet, 定位, 关键信息)。"""
    act = _ACTION_CN.get(getattr(it, "action", ""), getattr(it, "action", "?"))
    tbl = getattr(it, "table_hint", "") or "?"
    sheet = getattr(it, "sheet_hint", "") or ""
    loc = f"{tbl}.{sheet}" if sheet else tbl
    # 定位（单主键 / 复合主键）
    loc_parts = []
    lf = getattr(it, "locator_field", None)
    lv = getattr(it, "locator_value", None)
    if lf and lv not in (None, ""):
        loc_parts.append(f"{lf}={lv}")
    lfs = getattr(it, "locator_fields", None) or []
    lvs = getattr(it, "locator_values", None) or []
    if lfs and lvs and len(lfs) == len(lvs):
        loc_parts.append(",".join(f"{f}={v}" for f, v in zip(lfs, lvs)))
    locate = ";".join(loc_parts) if loc_parts else "-"
    # 关键信息：set 目标 / add 写入字段 / produces / consumes
    info = []
    if getattr(it, "action", "") == "set" and getattr(it, "target_field", None):
        info.append(f"{it.target_field}→{getattr(it, 'value', None)}")
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if getattr(it, "action", "") == "add" and isinstance(fields, dict) and fields:
        fv = []
        for k, v in list(fields.items())[:8]:
            vs = str(v)
            if len(vs) > 18:
                vs = vs[:17] + "…"
            fv.append(f"{k}={vs}")
        if len(fields) > 8:
            fv.append(f"…共{len(fields)}列")
        info.append("写入 " + ", ".join(fv))
    if getattr(it, "produces_label", None):
        info.append(f"产出<{it.produces_label}>")
    if getattr(it, "consumes_labels", None):
        info.append("消费 " + ",".join(f"<{c}>" for c in it.consumes_labels))
    return act, loc, locate, "；".join(info) if info else "-"


def _format_intents_table(intents: list) -> str:
    """意图清单 → 对齐文本表格（logger 打印用，中文宽度对齐）。"""
    headers = ("#", "操作", "表/Sheet", "定位", "关键信息")
    rows = []
    for i, it in enumerate(intents, start=1):
        act, loc, locate, info = _intent_cells(it)
        rows.append((str(i), act, loc, locate, info))
    # 列宽：header 与数据取 max；关键信息列限宽 52 防爆
    widths = []
    for c in range(len(headers)):
        w = _disp_w(headers[c])
        for r in rows:
            w = max(w, _disp_w(r[c]))
        widths.append(min(w, 52) if c == len(headers) - 1 else w)

    def _row(cells):
        return "│ " + " │ ".join(
            _pad(_clip(cells[c], widths[c]), widths[c])
            for c in range(len(headers))) + " │"

    top = "┌" + "┬".join("─" * (w + 2) for w in widths) + "┐"
    mid = "├" + "┼".join("─" * (w + 2) for w in widths) + "┤"
    bot = "└" + "┴".join("─" * (w + 2) for w in widths) + "┘"
    lines = [top, _row(headers), mid]
    lines.extend(_row(r) for r in rows)
    lines.append(bot)
    return "\n".join(lines)


def _jsonable(o: Any, depth: int = 0) -> Any:
    """递归把不可 JSON 序列化对象转成可序列化形式（截断深度/长度防爆）。

    extras 里可能嵌套 ColumnLocateResult 等非 dataclass 对象（浅拷贝残留），
    json.dumps 会抛 TypeError。本函数逐层转换：
      dict/list/set/tuple → 递归；有 to_dict → 展开；有 __dict__ → 展开；
      其余 → str 截断。
    """
    if o is None or isinstance(o, (str, int, float, bool)):
        return o
    if depth > 6:
        try:
            return str(o)[:80]
        except Exception:
            return "<unserializable>"
    if isinstance(o, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in o.items()}
    if isinstance(o, (list, tuple, set)):
        return [_jsonable(x, depth + 1) for x in o]
    if hasattr(o, "to_dict") and callable(o.to_dict):
        try:
            return _jsonable(o.to_dict(), depth + 1)
        except Exception:
            pass
    if hasattr(o, "__dict__"):
        try:
            return _jsonable(o.__dict__, depth + 1)
        except Exception:
            pass
    try:
        return str(o)[:120]
    except Exception:
        return f"<{type(o).__name__}>"


def _intent_to_json(it: Any) -> dict:
    """NLIntent → 精简 JSON（只含意图核心字段）。

    剔除 to_checkpoint_dict 里的调试数据（extracted_columns_signal / hits /
    validation / execution 等），只保留用户关心的意图本体：
      action/table/sheet + 定位 + fields(写入列) + set 目标 + produces/consumes。
    """
    d: dict = {
        "action": getattr(it, "action", ""),
        "table": getattr(it, "table_hint", "") or None,
        "sheet": getattr(it, "sheet_hint", "") or None,
    }
    lf = getattr(it, "locator_field", None)
    lv = getattr(it, "locator_value", None)
    if lf and lv not in (None, ""):
        d["定位"] = {lf: lv}
    lfs = getattr(it, "locator_fields", None) or []
    lvs = getattr(it, "locator_values", None) or []
    if lfs and lvs and len(lfs) == len(lvs):
        d["定位"] = dict(zip(lfs, lvs))
    tf = getattr(it, "target_field", None)
    if tf:
        d["set"] = {tf: getattr(it, "value", None)}
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if isinstance(fields, dict) and fields:
        d["fields"] = {str(k): _jsonable(v) for k, v in fields.items()}
    pl = getattr(it, "produces_label", None)
    if pl:
        d["produces"] = pl
    cl = getattr(it, "consumes_labels", None)
    if cl:
        d["consumes"] = list(cl)
    return d


_PLACEHOLDER_RE = re.compile(r"<\s*([^>]+?)\s*>")


def _intent_produces(it: Any) -> str:
    label = getattr(it, "produces_label", None)
    if not label and getattr(it, "extras", None):
        label = (it.extras or {}).get("produces")
    return str(label or "").strip().strip("<>").strip()


def _intent_fields_map(it: Any) -> dict:
    fields = (getattr(it, "extras", None) or {}).get("fields")
    return fields if isinstance(fields, dict) else {}


def _collect_placeholder_refs(value: Any) -> set[str]:
    refs: set[str] = set()
    if isinstance(value, str):
        for m in _PLACEHOLDER_RE.finditer(value):
            label = m.group(1).strip()
            if label and label.lower() != "auto":
                refs.add(label)
    elif isinstance(value, dict):
        for nested in value.values():
            refs.update(_collect_placeholder_refs(nested))
    elif isinstance(value, (list, tuple, set)):
        for nested in value:
            refs.update(_collect_placeholder_refs(nested))
    return refs


def _has_real_field_value(fields: dict) -> bool:
    for value in fields.values():
        if value is None:
            continue
        s = str(value).strip()
        if not s:
            continue
        if s.startswith("<") and s.endswith(">"):
            continue
        return True
    return False


def _intent_evidence_values(it: Any) -> list[str]:
    values: list[str] = []
    fields = _intent_fields_map(it)
    for value in fields.values():
        if isinstance(value, (str, int, float)):
            s = str(value).strip()
            if len(s) >= 4 and not (s.startswith("<") and s.endswith(">")):
                values.append(s)
    for value in (
        getattr(it, "locator_value", None),
        getattr(it, "value", None),
    ):
        if isinstance(value, (str, int, float)):
            s = str(value).strip()
            if len(s) >= 4:
                values.append(s)
    return values


def _normalise_label(value: Any) -> str:
    return str(value or "").strip().strip("<>").strip()


def _build_step1_plan_graph(intents: list) -> dict:
    nodes: list[dict] = []
    label_to_idx: dict[str, int] = {}
    produced_labels: set[str] = set()
    for idx, it in enumerate(intents or [], start=1):
        fields = _intent_fields_map(it)
        produces = _intent_produces(it)
        if produces:
            produced_labels.add(produces)
            label_to_idx.setdefault(produces, idx)
        real_fields = [
            col for col, value in fields.items()
            if value is not None
            and str(value).strip()
            and not (str(value).strip().startswith("<")
                     and str(value).strip().endswith(">"))
        ]
        nodes.append({
            "idx": idx,
            "action": getattr(it, "action", "") or "",
            "table": getattr(it, "table_hint", "") or "",
            "sheet": getattr(it, "sheet_hint", "") or "",
            "produces": produces,
            "consumes": [
                _normalise_label(x)
                for x in (getattr(it, "consumes_labels", None) or [])
                if _normalise_label(x)
            ],
            "field_count": len(fields),
            "real_field_count": len(real_fields),
        })

    edges: list[dict] = []
    unresolved_refs: list[dict] = []
    by_key: dict[tuple, dict] = {}
    adjacency: dict[str, set[str]] = {label: set() for label in produced_labels}

    def add_ref(src_idx: int, src_label: str, dep: str, via: str) -> None:
        if not dep or dep == src_label:
            return
        dst_idx = label_to_idx.get(dep)
        if dst_idx is None:
            unresolved_refs.append({
                "idx": src_idx,
                "label": dep,
                "via": via,
            })
            return
        key = (src_idx, dst_idx, dep)
        existing = by_key.get(key)
        if existing is not None:
            vias = existing.setdefault("via", [])
            if via not in vias:
                vias.append(via)
            return
        edge = {
            "from_idx": src_idx,
            "to_idx": dst_idx,
            "label": dep,
            "via": [via],
        }
        by_key[key] = edge
        edges.append(edge)
        if src_label:
            adjacency.setdefault(src_label, set()).add(dep)

    for idx, it in enumerate(intents or [], start=1):
        own = _intent_produces(it)
        fields = _intent_fields_map(it)
        for col, value in fields.items():
            for dep in sorted(_collect_placeholder_refs(value)):
                add_ref(idx, own, dep, f"field:{col}")
        for dep in sorted({
            _normalise_label(x)
            for x in (getattr(it, "consumes_labels", None) or [])
            if _normalise_label(x)
        }):
            add_ref(idx, own, dep, "consumes")

    visiting: set[str] = set()
    visited: set[str] = set()
    stack: list[str] = []
    cycles: set[tuple[str, ...]] = set()

    def dfs(label: str) -> None:
        if label in visiting:
            try:
                start = stack.index(label)
                cycles.add(tuple(stack[start:] + [label]))
            except ValueError:
                pass
            return
        if label in visited:
            return
        visiting.add(label)
        stack.append(label)
        for dep in sorted(adjacency.get(label, set())):
            dfs(dep)
        stack.pop()
        visiting.remove(label)
        visited.add(label)

    for label in sorted(adjacency):
        dfs(label)

    cycle_rows = []
    for cyc in sorted(cycles):
        cycle_rows.append({
            "labels": list(cyc),
            "idxs": [label_to_idx.get(x) for x in cyc if label_to_idx.get(x)],
        })

    return {
        "nodes": nodes,
        "edges": edges,
        "unresolved_refs": unresolved_refs,
        "cycles": cycle_rows,
        "node_count": len(nodes),
        "edge_count": len(edges),
        "unresolved_ref_count": len(unresolved_refs),
        "cycle_count": len(cycle_rows),
    }


def _semantic_value_kind(value: Any) -> str:
    if value is None:
        return "empty"
    text = str(value).strip()
    if not text:
        return "empty"
    refs = _collect_placeholder_refs(value)
    if refs:
        return "reference"
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    return "literal"


def _build_step1_semantic_plan(intents: list, plan_graph: dict | None = None) -> dict:
    if plan_graph is None:
        plan_graph = _build_step1_plan_graph(intents)
    produced_to_idx = {
        str(node.get("produces") or ""): node.get("idx")
        for node in plan_graph.get("nodes", []) or []
        if node.get("produces")
    }
    entities: list[dict] = []
    refs: list[dict] = []
    for idx, it in enumerate(intents or [], start=1):
        fields = _intent_fields_map(it)
        own = _intent_produces(it)
        attrs = []
        entity_refs = []
        for col, value in fields.items():
            attr = {
                "name": str(col),
                "value": _jsonable(value),
                "kind": _semantic_value_kind(value),
            }
            labels = sorted(_collect_placeholder_refs(value))
            dep_labels = [label for label in labels if label != own]
            if labels:
                if dep_labels:
                    attr["refs"] = dep_labels
                if own in labels:
                    attr["produces_ref"] = own
            attrs.append(attr)
            for label in dep_labels:
                ref = {
                    "from_entity": idx,
                    "field": str(col),
                    "label": label,
                    "to_entity": produced_to_idx.get(label),
                    "status": "resolved" if label in produced_to_idx else "unresolved",
                }
                refs.append(ref)
                entity_refs.append(ref)
        for label in sorted({
            _normalise_label(x)
            for x in (getattr(it, "consumes_labels", None) or [])
            if _normalise_label(x)
        }):
            if any(r["label"] == label for r in entity_refs):
                continue
            if label == own:
                continue
            ref = {
                "from_entity": idx,
                "field": None,
                "label": label,
                "to_entity": produced_to_idx.get(label),
                "status": "resolved" if label in produced_to_idx else "unresolved",
            }
            refs.append(ref)
            entity_refs.append(ref)
        entities.append({
            "entity_id": idx,
            "operation": getattr(it, "action", "") or "",
            "target": {
                "table": getattr(it, "table_hint", "") or "",
                "sheet": getattr(it, "sheet_hint", "") or "",
            },
            "locator": {
                "field": getattr(it, "locator_field", None),
                "value": _jsonable(getattr(it, "locator_value", None)),
                "fields": list(getattr(it, "locator_fields", None) or []),
                "values": [
                    _jsonable(x) for x in (getattr(it, "locator_values", None) or [])
                ],
            },
            "attributes": attrs,
            "produces": own,
            "references": entity_refs,
            "raw": getattr(it, "raw", "") or "",
        })
    relations = [
        {
            "from_entity": edge.get("from_idx"),
            "to_entity": edge.get("to_idx"),
            "label": edge.get("label"),
            "via": list(edge.get("via") or []),
        }
        for edge in plan_graph.get("edges", []) or []
    ]
    return {
        "version": 1,
        "entities": entities,
        "relations": relations,
        "refs": refs,
        "entity_count": len(entities),
        "relation_count": len(relations),
        "unresolved_ref_count": sum(1 for r in refs if r.get("status") == "unresolved"),
    }


def _step1_quality_report(intents: list, plan_graph: dict | None = None) -> dict:
    produced = [_intent_produces(it) for it in intents or []]
    produced = [p for p in produced if p]
    produced_counts = Counter(produced)
    produced_set = set(produced_counts)
    issues: list[dict] = []

    for label, count in sorted(produced_counts.items()):
        if count > 1:
            issues.append({
                "type": "duplicate_produces",
                "label": label,
                "count": count,
                "severity": "hard",
            })

    for idx, it in enumerate(intents or [], start=1):
        table = getattr(it, "table_hint", "") or ""
        sheet = getattr(it, "sheet_hint", "") or ""
        fields = _intent_fields_map(it)
        field_refs = _collect_placeholder_refs(fields)
        consumes = {
            str(x).strip().strip("<>").strip()
            for x in (getattr(it, "consumes_labels", None) or [])
            if str(x).strip()
        }
        own = _intent_produces(it)
        if getattr(it, "action", "") == "add" and not _has_real_field_value(fields):
            issues.append({
                "type": "empty_add",
                "idx": idx,
                "table": table,
                "sheet": sheet,
                "severity": "hard",
            })
        for label in sorted(field_refs):
            if label == own:
                continue
            if label not in produced_set:
                issues.append({
                    "type": "unresolved_placeholder",
                    "idx": idx,
                    "table": table,
                    "sheet": sheet,
                    "label": label,
                    "severity": "hard",
                })
        non_id_refs: set[str] = set()
        for col, value in fields.items():
            if str(col).strip().lower().replace("_", "") == "id":
                continue
            non_id_refs.update(_collect_placeholder_refs(value))
        for col, value in fields.items():
            if str(col).strip().lower().replace("_", "") != "id":
                continue
            id_refs = _collect_placeholder_refs(value)
            if not id_refs:
                continue
            for label in sorted(id_refs & produced_set):
                if label != own and label in non_id_refs:
                    issues.append({
                        "type": "foreign_placeholder_primary_key",
                        "idx": idx,
                        "table": table,
                        "sheet": sheet,
                        "label": label,
                        "severity": "hard",
                    })
        dangling = consumes - field_refs
        for label in sorted(dangling):
            issues.append({
                "type": "dangling_consumes",
                "idx": idx,
                "table": table,
                "sheet": sheet,
                "label": label,
                "severity": "soft",
            })

    if plan_graph is None:
        plan_graph = _build_step1_plan_graph(intents)
    for cyc in plan_graph.get("cycles", []) or []:
        issues.append({
            "type": "producer_dependency_cycle",
            "labels": list(cyc.get("labels") or []),
            "idxs": list(cyc.get("idxs") or []),
            "severity": "hard",
        })

    hard_count = sum(1 for x in issues if x.get("severity") == "hard")
    return {
        "ok": hard_count == 0,
        "hard_count": hard_count,
        "issue_count": len(issues),
        "issues": issues,
        "produces": sorted(produced_set),
        "placeholder_count": sum(len(_collect_placeholder_refs(_intent_fields_map(it)))
                                 for it in intents or []),
        "plan_nodes": plan_graph.get("node_count", 0),
        "plan_edges": plan_graph.get("edge_count", 0),
        "plan_cycles": plan_graph.get("cycle_count", 0),
        "plan_unresolved_refs": plan_graph.get("unresolved_ref_count", 0),
    }


def _build_step1_audit(intents: list,
                       quality: dict | None = None,
                       plan_graph: dict | None = None,
                       semantic_plan: dict | None = None,
                       semantic_compile_report: dict | None = None,
                       locator_results: list | None = None) -> dict:
    """Step1 JSON 审计报告（路线图 §8/§9.5）。

    把 Step1 输出失败原因固定为可统计的分层指标，供评测体系与答辩口径消费：
      - 意图层：表命中、字段命中、空壳 add、幻觉字段（schema 校验期数）、
        引用数、悬空引用数、依赖环数。
      - 占位符层：占位符总量、未解数、解析率、producer 缺失数。
      - 候选层：全段候选表合并去重数与覆盖。
      - 问题分布：按 type×severity 聚合（quality + semantic_compile 双源）。

    纯函数、0 LLM、确定性，可在评测脚本里离线跑，作为
    「表命中率/字段命中率/幻觉字段数/空壳 intent 数/placeholder 正确率」
    的统一取数口。
    """
    plan_graph = plan_graph or _build_step1_plan_graph(intents)
    quality = quality or _step1_quality_report(intents, plan_graph)
    semantic_plan = semantic_plan or _build_step1_semantic_plan(intents, plan_graph)

    intent_rows: list[dict] = []
    field_total = 0
    field_hit = 0
    empty_add = 0
    placeholder_total = 0
    unresolved_placeholder = 0
    producer_missing = 0
    produced_labels: set[str] = set()

    for idx, it in enumerate(intents or [], start=1):
        fields = _intent_fields_map(it)
        table = getattr(it, "table_hint", "") or ""
        sheet = getattr(it, "sheet_hint", "") or ""
        real_fields = [
            col for col, value in fields.items()
            if value is not None and str(value).strip()
            and not (str(value).strip().startswith("<")
                     and str(value).strip().endswith(">"))
        ]
        produces = _intent_produces(it)
        if produces:
            produced_labels.add(produces)
        refs = set()
        for value in fields.values():
            refs.update(_collect_placeholder_refs(value))
        own_refs = {r for r in refs if r == produces}
        dep_refs = refs - own_refs
        placeholder_total += len(dep_refs)
        for label in sorted(dep_refs):
            if label not in produced_labels:
                unresolved_placeholder += 1
        field_total += len(fields)
        field_hit += len(real_fields)
        if getattr(it, "action", "") == "add" and not real_fields:
            empty_add += 1
        intent_rows.append({
            "idx": idx,
            "action": getattr(it, "action", "") or "",
            "table": table,
            "sheet": sheet,
            "field_total": len(fields),
            "field_hit": len(real_fields),
            "produces": produces,
            "consumes": sorted({
                _normalise_label(x)
                for x in (getattr(it, "consumes_labels", None) or [])
                if _normalise_label(x)
            }),
            "dependency_refs": sorted(dep_refs),
        })

    # producer 缺失：被引用但无任何 intent 产出的占位符标签。
    ref_labels: set[str] = set()
    for it in intents or []:
        for value in _intent_fields_map(it).values():
            ref_labels.update(_collect_placeholder_refs(value))
    producer_missing = len({label for label in ref_labels
                            if label not in produced_labels})

    # 候选层：全段 locator 候选 stem 合并去重。
    cand_stems: list[str] = []
    _seen: set = set()
    per_segment: list[dict] = []
    # 候选分层（MVP #3）：跨段合并 required/dependency/context（0 LLM，附加观测）。
    _grp_required: set = set()
    _grp_dependency: set = set()
    _grp_context: set = set()
    for i, lr in enumerate(locator_results or [], start=1):
        stems: list[str] = []
        for c in (getattr(lr, "candidates", None) or []):
            s = getattr(c, "stem", None)
            if s:
                stems.append(s)
                if s not in _seen:
                    _seen.add(s)
                    cand_stems.append(s)
        _g = getattr(lr, "candidate_groups", None) or {}
        _grp_required.update(_g.get("required", []) or [])
        _grp_dependency.update(_g.get("dependency", []) or [])
        _grp_context.update(_g.get("context", []) or [])
        per_segment.append({"segment_idx": i, "candidates": stems,
                            "candidate_count": len(stems)})
    # required 优先归属：一个 stem 若在任一段被判 required，则不再计入 dep/context。
    _grp_dependency -= _grp_required
    _grp_context -= (_grp_required | _grp_dependency)

    # 问题分布（quality + semantic_compile 双源聚合）。
    issue_dist: dict[str, dict] = {}
    for src_name, report in (
        ("quality", quality),
        ("semantic_compile", semantic_compile_report),
    ):
        for issue in (report or {}).get("issues", []) or []:
            if not isinstance(issue, dict):
                continue
            key = f"{src_name}:{issue.get('type')}"
            severity = issue.get("severity", "soft")
            row = issue_dist.setdefault(
                key, {"source": src_name, "type": issue.get("type"),
                      "severity": severity, "count": 0})
            row["count"] += 1

    hard_issue_count = sum(
        1 for row in issue_dist.values() if row["severity"] == "hard")
    resolved_placeholders = placeholder_total - unresolved_placeholder

    # Phase 1.5：FK 闭包完整性审计（0 LLM，只报告不阻断）。
    # 用关系图对 semantic_plan 做「漏表/悬空引用」压力测试，产出结构化 findings +
    # 补壳建议。此处仅纳入审计报告供 step2 与评测消费，不改变 step1 的 ok 门禁。
    completeness = {"ok": True, "findings": [], "suggestions": [],
                    "hard_count": 0, "finding_count": 0}
    try:
        completeness = audit_plan_completeness(semantic_plan)
    except Exception:  # noqa: BLE001 - 审计失败不得影响 step1 主流程
        logger.warning("plan_completeness 审计异常（已忽略，report-only）",
                       exc_info=True)

    _first_raw = ""
    for _it in (intents or []):
        _first_raw = getattr(_it, "raw", "") or ""
        break
    return {
        "version": 1,
        "input_len": len(str(_first_raw)),
        "segment_count": len(per_segment),
        "segments": per_segment,
        "candidates": cand_stems,
        "candidate_count": len(cand_stems),
        "candidate_groups": {
            "required": sorted(_grp_required),
            "dependency": sorted(_grp_dependency),
            "context": sorted(_grp_context),
        },
        "candidate_required_count": len(_grp_required),
        "candidate_dependency_count": len(_grp_dependency),
        "candidate_context_count": len(_grp_context),
        "intents": intent_rows,
        "intent_count": len(intents or []),
        "metrics": {
            "table_hit": sum(1 for it in intents or []
                             if getattr(it, "table_hint", "")),
            "table_hit_rate": round(
                sum(1 for it in intents or []
                    if getattr(it, "table_hint", "")) / max(1, len(intents or [])), 4),
            "field_hit": field_hit,
            "field_total": field_total,
            "field_hit_rate": round(field_hit / max(1, field_total), 4),
            "hallucinated_fields": 0,  # schema_getter 未注入时为 0，见 schema_checked
            "schema_checked": False,
            "empty_add_count": empty_add,
            "placeholder_total": placeholder_total,
            "unresolved_placeholder_count": unresolved_placeholder,
            "producer_missing_count": producer_missing,
            "placeholder_resolved_rate": round(
                resolved_placeholders / max(1, placeholder_total), 4),
            "reference_count": plan_graph.get("edge_count", 0),
            "unresolved_ref_count": plan_graph.get("unresolved_ref_count", 0),
            "cycle_count": plan_graph.get("cycle_count", 0),
            "hard_issue_count": hard_issue_count,
            "issue_count": len(issue_dist),
            "completeness_missing_producer_count": sum(
                1 for f in completeness.get("findings", [])
                if f.get("type") == "missing_producer_entity"),
            "completeness_unresolved_ref_count": sum(
                1 for f in completeness.get("findings", [])
                if f.get("type") == "unresolved_reference"),
        },
        "issue_distribution": {
            key: dict(row) for key, row in sorted(issue_dist.items())
        },
        "plan_completeness": completeness,
        "ok": bool(quality.get("ok")) and hard_issue_count == 0,
    }


_SEGMENT_ACTION_RE = re.compile(
    r"(?:"
    r"\u65b0\u589e|\u589e\u52a0|\u6dfb\u52a0|"
    r"\u4fee\u6539|\u6539\u6210|\u6539\u4e3a|"
    r"\u5220\u9664|\u53bb\u6389|\u79fb\u9664|\u6e05\u9664|"
    r"\u67e5\u770b|\u67e5\u8be2|"
    r"\u914d\u4e00\u4e2a|\u5efa\u4e00\u4e2a|\u9020\u4e00\u4e2a"
    r")"
)
_SEGMENT_ENUM_PREFIX_RE = re.compile(
    r"^\s*(?:\u7b2c[\u4e00\u4e8c\u4e09\u56db\u4e94\u516d\u4e03"
    r"\u516b\u4e5d\u5341\d]+\u6761|[\u4e00\u4e8c\u4e09\u56db"
    r"\u4e94\u516d\u4e03\u516b\u4e5d\u5341\d]+[、.．])"
)
_SEGMENT_STRUCTURED_MARKER_RE = re.compile(
    r"(?:\d|=|：|:|key|type|id|ID|"
    r"\u7f16\u53f7|\u7c7b\u578b|\u65f6\u95f4|\u540d\u79f0)"
)


def _is_low_signal_uncovered_segment(seg_text: str, idx: int, total: int) -> bool:
    text = str(seg_text or "").strip()
    if not text:
        return True
    if _SEGMENT_ACTION_RE.search(text):
        return False
    if _SEGMENT_ENUM_PREFIX_RE.search(text):
        has_row_markers = bool(re.search(
            r"(?:key|type|id|ID|\u7f16\u53f7|\u7c7b\u578b|=|:|：|\d)",
            text))
        return not has_row_markers
    if idx == 0 and total > 1 and not _SEGMENT_STRUCTURED_MARKER_RE.search(text):
        return True
    return False


class Step1ParseSubAgent:
    """Step1：输入分析、匹配表格、指令初形成。"""

    def __init__(self, parser=None, thinking_sink=None, cli=None,
                 locator_agent=None, decompose_agent=None):
        self._parser = parser
        self._thinking_sink = thinking_sink
        self._cli = cli
        # 复用现有 ParseAgent（已含 split_multi_intent 分段 + 段级对账 + decompose_segment）
        self._parse_agent = ParseAgent(
            parser=parser, thinking_sink=thinking_sink, cli=cli,
            locator_agent=locator_agent, decompose_agent=decompose_agent)
        # metrics
        self._llm_calls = 0

    def execute(self, ctx: StepContext) -> StepResult:
        """Step1 执行：text → list[NLIntent]（装进 artifacts）。

        错误归属：所有错误 step_id=STEP1_PARSE。
        - 全空 + 兜底也空 → hard error（后续步无法跑）
        - 某段 0 intent → soft error（segment_idx 标注，不阻断）
        - 内部异常 → soft error（legacy fallback 仍可尝试）

        §段级对账：本层调 split_multi_intent 取 segments（parse 内部同源调用，结果一致），
        用于段级覆盖对账；产空走 splitter_baseline 兜底。
        """
        t0 = time.time()
        errors: list[StepError] = []
        warnings: list[str] = []
        intents: list = []
        segments: list = []
        suppressed_segment_no_intent = 0
        # §中危 4 修复：execute 前后读 counter 差值 = 本步 LLM 调用数（替代硬编码 0）。
        # Step1 的 decompose/locate LLM 经 parser._llm_counter 累计（共享 counter），
        # 差值法隔离出本步调用，避免 Step3 metrics 被本步累计污染。
        _cnt = getattr(self._parser, "_llm_counter", None)
        _cnt_before = 0
        try:
            _cnt_before = int(_cnt.peek_total()) if _cnt else 0
        except Exception:
            _cnt_before = 0
        # StepTrace §P0：本步 LLM 可观测性（耗时/prompt 体量/超时）差值快照。
        _trace_before: dict = {}
        try:
            _trace_before = _cnt.as_dict() if _cnt else {}
        except Exception:
            _trace_before = {}

        try:
            # §split 复用：parse 内部已调 split_multi_intent 并缓存到 _last_segments，
            # Step1 读它做段级对账，不再重复调 split（消除冗余 + 双源风险）。
            # §主线2：清空上一轮的"被丢弃 intent"台账，避免跨 run 泄漏。
            try:
                self._parse_agent._dropped_intents = []
            except Exception:
                pass
            intents = self._parse_agent.parse(ctx.user_text)
            segments = getattr(self._parse_agent, "_last_segments", []) or []
            ctx.segments = segments

            # §去硬模板：Step1 层的二次 splitter_baseline 兜底（cross_table_splitter
            # 11 模板）默认关闭，理由同 decompose_agent._splitter_baseline——硬编码
            # 正则抽字段会越权替 LLM 做业务判断。可用
            # CODEMAKER_DECOMPOSE_DISABLE_TEMPLATE_FALLBACK=0 显式重新开启。
            if not intents and segments and os.environ.get(
                    "CODEMAKER_DECOMPOSE_DISABLE_TEMPLATE_FALLBACK", "1") != "1":
                warnings.append("ParseAgent 产空,尝试 splitter_baseline 兜底")
                try:
                    from ...core.cross_table_splitter import (
                        CrossTableIntentSplitter, detect_cross_table_action)
                    if detect_cross_table_action(ctx.user_text):
                        splitter = CrossTableIntentSplitter()
                        split_intents = splitter.split(ctx.user_text)
                        if split_intents:
                            intents = self._parse_agent.parse_baseline(
                                ctx.user_text, split_intents)
                            if intents:
                                warnings.append(
                                    f"splitter_baseline 兜底成功,产 {len(intents)} 条")
                except Exception:  # noqa: BLE001
                    logger.warning("Step1 splitter_baseline 兜底失败",
                                   exc_info=True)
        except StepHardError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("Step1 ParseAgent 异常", exc_info=True)
            errors.append(StepError(
                step_id=STEP1_PARSE, error_type="parse_internal",
                message="指令解析失败",
                root_cause=f"{type(e).__name__}: {e}", is_hard=False))

        # §增强：段级覆盖对账（精确全文匹配，非前缀15）
        # §P2-11 原对账 seg in raw 恒真（raw=seg全文），无区分度。补充动作数校验：
        # 段含 N 个动作词（新增/修改/删除/查看）应产 ≥ N 条意图，少于则报段内漏产 soft warning。
        if segments and len(segments) > 1 and intents:
            covered = set()
            # 段→产意图数映射（按 raw 文本包含归属）
            seg_intent_count: dict[int, int] = {}
            for it in intents:
                raw = (getattr(it, "raw", "") or "").strip()
                if not raw:
                    continue
                for i, seg in enumerate(segments):
                    if i in covered:
                        # 仍计数（一个段可能多 intent）
                        pass
                    seg_text = (getattr(seg, "text", seg)
                                if not isinstance(seg, str) else seg).strip()
                    # 双向包含：段文本在 raw 内 或 raw 在段文本内（段被 LLM 扩写）
                    field_covered = any(
                        v in seg_text for v in _intent_evidence_values(it)
                    )
                    if seg_text and (seg_text in raw or raw in seg_text or field_covered):
                        covered.add(i)
                        seg_intent_count[i] = seg_intent_count.get(i, 0) + 1
            import re as _re
            # 动作数校验：段含的动作词数应 ≤ 产意图数
            _action_re = _re.compile(r'(?:新增|增加|添加|修改|改成|改为|删除|去掉|移除|清除|查看|查询|配一个|建一个|造一个|给一个)')
            for i, seg in enumerate(segments):
                seg_text = (getattr(seg, "text", seg)
                            if not isinstance(seg, str) else seg).strip()
                if i not in covered:
                    if _is_low_signal_uncovered_segment(seg_text, i, len(segments)):
                        suppressed_segment_no_intent += 1
                        continue
                    errors.append(StepError(
                        step_id=STEP1_PARSE, error_type="segment_no_intent",
                        message=f"第{i+1}段「{(seg_text or '')[:20]}」未能解析出意图",
                        is_hard=False, segment_idx=i))
                    continue
                # 段内动作数 vs 产意图数
                n_actions = len(_SEGMENT_ACTION_RE.findall(seg_text))
                n_intents = seg_intent_count.get(i, 0)
                if n_actions > 1 and n_intents < n_actions:
                    errors.append(StepError(
                        step_id=STEP1_PARSE, error_type="segment_partial_coverage",
                        message=f"第{i+1}段含{n_actions}个动作但仅产{n_intents}条意图，"
                                f"可能有子句漏解析",
                        is_hard=False, segment_idx=i))

        # 全空 → hard（后续步无法跑）
        if not intents:
            errors.append(StepError(
                step_id=STEP1_PARSE, error_type="parse_empty",
                message="未解析出任何可执行意图",
                suggestion="请简化指令或检查表格是否存在",
                is_hard=True))

        # 产出存入 ctx 供后续步只读
        # intents 适配为 NLIntent[]（ParseAgent.parse 已返回 NLIntent[]）
        # locator_results 显式产出（替代 Step2 探 _last_locator_result 私态）：
        # Step1 持有 parse_agent 句柄，读其 _last_locator_results 全段收集，
        # 写入 s1.artifacts["locator_results"]，Step2 改读 artifacts。
        locator_results = getattr(self._parse_agent, "_last_locator_results", []) or []
        # §中危 8：把全段 candidates stems 合并去重，注入每条 intent.extras
        # ["locator_candidates"]。V2 Step3 路径（execute_no_llm）下 _phase_partition
        # 读此短路 _resolve_table 重跑（行索引策略1 可能用 locator_value 误命中它表，
        # 覆盖 decompose 已选定的 table_hint）。candidates 是表级全局信号，多段
        # 时合并去重后作"Step1 已探测合法候选表集合"供 partition 校验。
        _cand_stems: list[str] = []
        _seen: set = set()
        for _lr in locator_results:
            for _c in (getattr(_lr, "candidates", None) or []):
                _s = getattr(_c, "stem", None)
                if _s and _s not in _seen:
                    _seen.add(_s)
                    _cand_stems.append(_s)
        for _it in intents:
            try:
                if _it.extras is None:
                    _it.extras = {}
                _it.extras["locator_candidates"] = list(_cand_stems)
            except Exception:
                pass
        plan_graph = _build_step1_plan_graph(intents)
        semantic_plan = _build_step1_semantic_plan(intents, plan_graph)
        _, semantic_compile_report = compile_semantic_plan_to_intents(semantic_plan)
        quality = _step1_quality_report(intents, plan_graph)
        audit = _build_step1_audit(intents, quality, plan_graph, semantic_plan,
                                   semantic_compile_report, locator_results)
        if quality.get("issue_count"):
            warnings.append(
                "Step1 quality issues: "
                f"{quality.get('hard_count', 0)} hard / "
                f"{quality.get('issue_count', 0)} total")
        strict_quality = os.getenv("CODEMAKER_STEP1_QUALITY_GATE", "0").lower() in (
            "1", "true", "yes", "on")
        if strict_quality and quality.get("hard_count"):
            errors.append(StepError(
                step_id=STEP1_PARSE,
                error_type="step1_quality_gate",
                message="Step1 output has unresolved structural issues",
                root_cause=json.dumps(quality.get("issues", [])[:8], ensure_ascii=False),
                suggestion="Fix Step1 references before Step2/Step3 execution",
                is_hard=True))
        # §主线2：被丢弃的 intent（孤立空壳等）不再静默——转成 soft 错误让 Step4
        # 报"部分完成"而非干净成功（error_type 属 Step4._DROPPED_INTENT_ERROR_TYPES）。
        _dropped = getattr(self._parse_agent, "_dropped_intents", None) or []
        for _dp in _dropped:
            errors.append(StepError(
                step_id=STEP1_PARSE,
                error_type="segment_partial_coverage",
                message=(f"子任务被丢弃（{_dp.get('reason','')}）："
                         f"{_dp.get('table_stem','?')}/{_dp.get('table_sheet','?')}"),
                root_cause=(f"该 add 无字段值且未被本批消费，已丢弃；"
                            f"raw={_dp.get('raw','')}"),
                suggestion="若该子任务是用户真实意图，请补齐其字段或检查指令覆盖",
                is_hard=False))
        # 本步 LLM 调用数（差值法）
        _llm_calls = 0
        try:
            _llm_calls = max(0, int(_cnt.peek_total()) - _cnt_before) if _cnt else 0
        except Exception:
            _llm_calls = 0
        # StepTrace §P0：本步 LLM 可观测性差值（耗时/prompt 体量/响应/超时/错误）。
        _trace_after: dict = {}
        try:
            _trace_after = _cnt.as_dict() if _cnt else {}
        except Exception:
            _trace_after = {}

        def _trace_delta(key: str) -> int:
            try:
                return max(0, int(_trace_after.get(key, 0)) - int(_trace_before.get(key, 0)))
            except Exception:
                return 0
        # Step1 结束：打印意图清单（对齐文本表格），便于后续 Step2 校验对照
        if intents:
            _table = _format_intents_table(intents)
            logger.info("Step1 解析意图清单（%d 条）:\n%s", len(intents), _table)
            # 推 thinking 事件（前端 Step1 气泡 Thinking 区逐条显示）。
            # 单行格式：前端 thinking_steps 用 Vue 插值渲染，\n 会折叠成空格，
            # 故每条意图推一个独立事件，phase=意图序号，detail=规整表格单元格。
            if self._thinking_sink is not None:
                for _i, _it in enumerate(intents, start=1):
                    _act, _loc, _locate, _info = _intent_cells(_it)
                    try:
                        self._thinking_sink(
                            f"意图{_i}",
                            f"{_act}｜{_loc}｜定位 {_locate}｜{_info}")
                    except Exception:  # noqa: BLE001
                        pass
                # 结构化 intent_list：phase 以 __json: 前缀标记，detail 是 JSON 字符串。
                # 前端识别该前缀把 detail 解析成数组，渲染成可对齐的 HTML 表格卡片，
                # 清晰展示"序号/操作/表·Sheet/定位/关键信息"，便于人工校验 Step1
                # 是否漏意图/错路由（vs 仅文本行不易对齐对照）。
                # 每行额外带 fields 键值对列表（不截断），前端渲染「字段=值」展开表，
                # 让校验环节能看清每个值对应哪个字段（匹配/类型问题定位到具体列）。
                _rows = []
                for _i, _it in enumerate(intents, start=1):
                    _act, _loc, _locate, _info = _intent_cells(_it)
                    _rows.append({
                        "idx": _i, "action": _act, "loc": _loc,
                        "locate": _locate, "info": _info,
                        "produces": getattr(_it, "produces_label", None),
                        "consumes": list(getattr(_it, "consumes_labels", []) or []),
                        "fields": _intent_fields(_it),
                    })
                try:
                    self._thinking_sink("__json:intent_list", json.dumps(
                        {"total": len(intents), "rows": _rows},
                        ensure_ascii=False))
                except Exception:  # noqa: BLE001
                    pass
        ok = bool(intents) and not (strict_quality and quality.get("hard_count"))
        return StepResult(
            step_id=STEP1_PARSE, ok=ok,
            errors=errors, warnings=warnings,
            metrics={
                "dur_ms": int((time.time() - t0) * 1000),
                "segments": len(segments),
                "intents": len(intents),
                "llm_calls": _llm_calls,
                "step1_llm_dur_ms": _trace_delta("total_dur_ms"),
                "step1_llm_prompt_chars": _trace_delta("total_prompt_chars"),
                "step1_llm_resp_chars": _trace_delta("total_resp_chars"),
                "step1_llm_timeouts": _trace_delta("total_timeouts"),
                "step1_llm_errors": _trace_delta("total_errors"),
                "candidate_count": audit.get("candidate_count", 0),
                "candidate_required_count": audit.get("candidate_required_count", 0),
                "candidate_dependency_count": audit.get("candidate_dependency_count", 0),
                "candidate_context_count": audit.get("candidate_context_count", 0),
                "step1_quality_hard": quality.get("hard_count", 0),
                "step1_quality_issues": quality.get("issue_count", 0),
                "step1_plan_nodes": plan_graph.get("node_count", 0),
                "step1_plan_edges": plan_graph.get("edge_count", 0),
                "step1_plan_cycles": plan_graph.get("cycle_count", 0),
                "semantic_entities": semantic_plan.get("entity_count", 0),
                "semantic_relations": semantic_plan.get("relation_count", 0),
                "semantic_unresolved_refs": semantic_plan.get("unresolved_ref_count", 0),
                "semantic_compile_issues": semantic_compile_report.get("issue_count", 0),
                "suppressed_segment_no_intent": suppressed_segment_no_intent,
                "audit_table_hit_rate": (audit.get("metrics") or {}).get("table_hit_rate", 0),
                "audit_field_hit_rate": (audit.get("metrics") or {}).get("field_hit_rate", 0),
                "audit_placeholder_resolved_rate": (audit.get("metrics") or {}).get("placeholder_resolved_rate", 0),
                "audit_empty_add_count": (audit.get("metrics") or {}).get("empty_add_count", 0),
            },
            artifacts={"intents": intents, "segments": segments,
                       "locator_result": getattr(self._parse_agent, "_last_locator_result", None),
                       "locator_results": locator_results,
                       "step1_quality": quality,
                       "plan_graph": plan_graph,
                       "semantic_plan": semantic_plan,
                       "semantic_compile_report": semantic_compile_report,
                       "step1_audit": audit})


__all__ = ["Step1ParseSubAgent"]

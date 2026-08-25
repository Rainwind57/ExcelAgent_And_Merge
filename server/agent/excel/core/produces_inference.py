"""关系图驱动的 produces 推断层（替代 splitter per-template 硬编码 produces）。

设计动机（cascade_resolver docstring 已预告"未来自动级联扩展"）：
splitter 的 _build_*_intents 每种链型硬编码 produces 标签 + <new_X> 占位符消费。
新链型（pet/mail/...）无模板 → produces 缺失 → 通用 topo 引擎
(_compute_deps/_topo_order/_capture_produced)虽通用但无 produces 边可建 →
引用一致 0.00（R8 cross-chain 变体实证）。

本层在解析后(Step1)、表解析(Step2)前对意图列表做一次 produces 推断：
  1. 同指令内 add 集合中，被其他表 FK 引用的 add → producer，挂 produces 标签
  2. consumer add 的 FK 字段（经 relation graph）若指向同指令 producer →
     字段值替换为 <producer_label> 占位符（仅当值为空/<auto>/占位符时替换，
     不覆盖显式已存在 id 引用）

不覆盖 splitter 已标注的 produces（模板保留其精确标签）。
让通用 topo 引擎对任意有 FK 关系的跨表链闭环，无需 per-template 手写。

查询接口依赖 cascade_resolver（基于 table_relations.json 声明式 FK 数据）。
"""
from __future__ import annotations

import re
from pathlib import Path

from .table_relations import RelationGraph


def _stem_of_path(p: str) -> str:
    """relation path → stem：'pet/pet.xlsx' → 'pet'。"""
    s = str(p).replace("\\", "/").rstrip("/")
    if s.endswith(".xlsx"):
        s = s[:-5]
    return s.rsplit("/", 1)[-1]


def _stem_of_hint(table_hint: str | None) -> str:
    """table_hint → stem：'pet/pet' / 'pet' / 'item.xlsx' → 'pet' / 'item'。"""
    if not table_hint:
        return ""
    return _stem_of_path(str(table_hint))


def _sheet_key(stem: str, sheet: str | None) -> tuple[str, str]:
    """(stem, sheet) 复合键，区分同文件跨 sheet（ItemBase vs Equipment）。"""
    return (stem, sheet or "")


def _norm(s: str) -> str:
    """列名归一：去类型后缀/空白/括号注释/大小写，供 FK 列名宽松匹配。"""
    if not s:
        return ""
    s = str(s).split(":")[0]
    s = re.split(r"[（(]", s)[0]
    return re.sub(r"\s+", "", s).lower()


def _field_matches_col(field_key: str, fk_col: str) -> bool:
    """字段名是否匹配 FK 列名（容忍中英文/点号/类型后缀差异）。

    P12 修复：原 `fk in k or k in fk` 子串匹配过宽——fk='id' 命中
    'model_id'/'item_id'/'prefab_id' 任意含 id 字段，触发 produces_inference
    把 producer 非 PK id 字段误当代换源 + forward_ref LLM 假阳性。审计建议
    「精确等值 + 后缀」但后缀 `k.endswith('_'+fk)` 仍让 'model_id' 命中 'id'
    （model_id 以 '_id' 结尾），未达消假阳性目标。故改为**精确等值 only**
    （点分键取末段 + 归一后 ==），彻底消除 id→model_id 类假阳性。
    """
    fk = _norm(fk_col)
    if not fk:
        return False
    # 点分键取末段（option_function.data.1.conv_id → conv_id）再比对
    k = _norm(str(field_key).split(".")[-1])
    if not k:
        return False
    if k == fk:
        return True
    # 全名归一比对（点分键整名）
    kf = _norm(str(field_key))
    return kf == fk


def _should_consume(value) -> bool:
    """字段值是否应替换为 producer 占位符。

    替换：空/None/<new>/占位符（消费方未给显式 id → 指向 producer 新 id）。
    不替换：显式数字/字符串 id（可能引用已存在行，值已自洽）；
    也不替换 `<auto>`（P17：用户没提的可选列，留空不转占位，避免触发
    `_phase_execute` placeholder_unresolved 二次 ask）。
    """
    if value is None:
        return True
    s = str(value).strip()
    if s == "<auto>":
        return False  # P17：可选列留空，不转 producer 占位符
    if s == "":
        return True
    if s.startswith("<") and s.endswith(">"):
        return True
    return False


def _is_blank_fk(value) -> bool:
    """FK 字段值是否"真空白"（仅 None/空串），用于消费方**只补空、不覆盖**。

    与 `_should_consume` 的关键区别：已含 `<placeholder>` 的字段视为**非空白**，
    不再被 sheet 级自动推断覆盖。原实现对任意 `<...>` 占位都覆盖成 sheet 级标签，
    会把 DecomposeAgent(LLM) 对**同 sheet 多行**（如对话树多个 conv/option）已正确
    连好的逐行占位符 `<new_encourage_conv_id>` 冲成同一个 `<new_interaction_id>`，
    → 多 producer 塌成一个 + conv↔option 假环。改为只补空白后：LLM 的逐行连线被
    保留，sheet 级推断仅兜底 LLM 漏填的空 FK（单 producer 链仍可自动闭环）。
    """
    if value is None:
        return True
    return str(value).strip() == ""



def infer_produces_consumes(intents: list) -> list:
    """对意图列表做关系图驱动的 produces 推断（原地修改 + 返回）。

    步骤：
      1. 收集 add 意图，标记 producer 候选（被其他表 FK 引用的 add 表）
      2. 给无 produces 标注的 producer 挂 produces=f"new_{stem}_id"
      3. consumer add 的 FK 字段（get_add_dependencies）若指向同指令 producer，
         且字段值 _should_consume → 替换为 <producer_label>

    幂等：已标注 produces 的意图保留；已含显式 id 的 FK 字段不替换。
    """
    if not intents:
        return intents
    add_idxs = [i for i, it in enumerate(intents)
                if it is not None and getattr(it, "action", "") == "add"]
    if len(add_idxs) < 2:
        # 单 add 无跨表链可建（producer/consumer 至少各一）
        return intents

    # 加载关系图（(from_stem, from_sheet) consumer → (to_stem, to_sheet) producer）
    try:
        rels = RelationGraph.load().relations
    except Exception:
        rels = []

    # 1. producer 候选：同指令 add 集合中，(stem, sheet) 作为 relation `to` 端（被引用）
    add_keys: dict[tuple, int] = {}  # (stem, sheet) -> intent idx
    for i in add_idxs:
        it = intents[i]
        stem = _stem_of_hint(getattr(it, "table_hint", None))
        if not stem:
            continue
        # P10：setdefault 保留首个 producer 候选，避免同 (stem,sheet) 多 add
        # 后写覆盖前写（与 _suppress_over_produce「一表一 op 契约」语义一致）
        add_keys.setdefault(_sheet_key(stem, getattr(it, "sheet_hint", None)), i)

    producers: dict[tuple, tuple[int, str]] = {}  # (stem, sheet) -> (idx, label)
    # producer 显式 PK 值（fields 中与 to_column 匹配的非 consume 值）→ 直接代换字面值，
    # 绕过 _capture_produced（显式 PK add 不一定回传 result_rows，占位符会悬空）
    producer_pk_values: dict[tuple, object] = {}
    for r in rels:
        to_key = _sheet_key(_stem_of_path(r.to_path), r.to_sheet)
        if to_key not in add_keys:
            continue
        idx = add_keys[to_key]
        it = intents[idx]
        extras = it.extras if getattr(it, "extras", None) is not None else {}
        existing = extras.get("produces")
        if isinstance(existing, str) and existing.strip():
            label = existing.strip()
        else:
            # P11：sheet-aware 标签，避免同 stem 多 producer（如 entity_prefab
            # 的 candidate/formal 两 sheet）撞同一 `new_{stem}_id` → produced
            # 字典后写覆盖 → consumer 占位符解析到错 PK
            stem_part = _stem_of_path(r.to_path)
            sheet_part = (r.to_sheet or "").strip()
            if sheet_part:
                label = f"new_{stem_part}_{sheet_part}_id"
            else:
                label = f"new_{stem_part}_id"
            extras["produces"] = label
            it.extras = extras
        producers[to_key] = (idx, label)
        # 提取 producer 显式 PK 值（fields 中匹配 to_column 的非 consume 值）
        fields = extras.get("fields")
        if isinstance(fields, dict) and to_key not in producer_pk_values:
            for k, v in fields.items():
                if _field_matches_col(k, r.to_column) and not _should_consume(v):
                    producer_pk_values[to_key] = v
                    break

    if not producers:
        return intents

    # 2. consumer：relation `from` 端为同指令 add，且 `to` 端是 producer → 替换 FK 字段值
    for r in rels:
        from_key = _sheet_key(_stem_of_path(r.from_path), r.from_sheet)
        to_key = _sheet_key(_stem_of_path(r.to_path), r.to_sheet)
        if from_key not in add_keys or to_key not in producers:
            continue
        cidx = add_keys[from_key]
        pidx, plabel = producers[to_key]
        if cidx == pidx:
            continue  # 不自引用
        it = intents[cidx]
        extras = it.extras if getattr(it, "extras", None) is not None else {}
        fields = extras.get("fields")
        if not isinstance(fields, dict):
            continue
        source_col = r.from_column
        # producer 有显式 PK → 直接代换字面值；否则用占位符（运行时 _capture_produced 解析）
        subst = producer_pk_values.get(to_key)
        if subst is None:
            subst = f"<{plabel}>"
        for k in list(fields.keys()):
            if _field_matches_col(k, source_col) and _is_blank_fk(fields[k]):
                fields[k] = subst
        extras["fields"] = fields
        it.extras = extras
    return intents


__all__ = ["infer_produces_consumes"]

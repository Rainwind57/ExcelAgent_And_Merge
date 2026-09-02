"""跨记录引用的 LLM 判定层（LLM 优先，规则搭骨架）。

动机（对 produces_inference 的补全）：
  produces_inference 是 0-LLM 的关系图驱动层，只对消费方**已存在且为空**的
  外键字段回填 producer 占位符。但当 DecomposeAgent 漏输出某个外键字段时
  （字段键根本不存在），或同一消费方有**多条外键指向同一 producer 表**时，
  规则层无从判断「哪一列引用的是本批新增实体」——这是纯语义 assignment 判定。

  典型场景（灵兽进化）：
    「新增九尾天狐 … 进化到 20999 九尾天狐·终焉」
    pet_evolve 有两条外键都指向 pet.灵兽id：
      - 宠物id（进化前/源）      ← 应引用本批新增的九尾天狐
      - 进化后的灵兽ID（进化后）  ← 是另一个灵兽 20999（原文显式给了）
    规则无法区分二者；「宠物id」还被 DecomposeAgent 整个漏掉了。

本层做法（按项目「匹配/划分判定 → LLM 优先」原则）：
  1. 规则/关系图（机械）：找出「同指令 producer(add) ↔ consumer(add)、to 端是
     本批 producer」的外键边，且消费方该外键列**缺失或为空**（已有显式值/占位
     符的一律不动，尊重既有决策）。
  2. LLM（唯一判定点）：给出 producer 新增实体摘要 + 消费方行 + 候选外键列
     + 原文，让 LLM 判定每个候选列是否引用该新增实体。
  3. 回写（机械）：判为 true 的列 → 注入 `<producer_label>` 占位符（复用运行时
     _capture_produced 解析为真实新 ID）；判为 false → 不动。

通用：不绑任何业务表/列名，靠 table_relations.json 的 FK 声明驱动，覆盖任意
add↔add 跨表外键链。LLM 不可用/超时/异常 → 全部降级为不改动，零回归。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Callable, Optional

from .produces_inference import (
    _field_matches_col,
    _is_blank_fk,
    _sheet_key,
    _stem_of_hint,
    _stem_of_path,
)
from .table_relations import RelationGraph

logger = logging.getLogger(__name__)

# LLM 调用适配器签名：prompt -> 原始响应文本（失败返回 ""/None）
LLMCall = Callable[[str], Optional[str]]


def _producer_label(intent) -> str:
    """读取 producer 已有的 produces 标签（produces_inference 已挂）。

    优先 produces_label，其次 extras['produces']；都没有则计算 sheet-aware 标签
    （与 produces_inference 同规则），并回写到 intent，保证 _compute_deps 能建边。
    """
    label = getattr(intent, "produces_label", None)
    if isinstance(label, str) and label.strip():
        return label.strip()
    extras = intent.extras if getattr(intent, "extras", None) is not None else {}
    ex = extras.get("produces")
    if isinstance(ex, str) and ex.strip():
        return ex.strip()
    # 计算并回写（与 produces_inference 命名一致：sheet 非空则 sheet-aware）
    stem = _stem_of_hint(getattr(intent, "table_hint", None))
    sheet = (getattr(intent, "sheet_hint", None) or "").strip()
    label = f"new_{stem}_{sheet}_id" if sheet else f"new_{stem}_id"
    extras["produces"] = label
    intent.extras = extras
    try:
        if not getattr(intent, "produces_label", None):
            intent.produces_label = label
    except Exception:
        pass
    return label


def _consumer_has_col(fields: dict, col: str) -> tuple[bool, bool]:
    """消费方 fields 是否已含匹配 col 的键，及该键是否为空白。

    返回 (has_key, is_blank)。has_key=False 表示该外键列被整段漏掉。
    """
    if not isinstance(fields, dict):
        return (False, True)
    for k, v in fields.items():
        if _field_matches_col(k, col):
            return (True, _is_blank_fk(v))
    return (False, True)


def _trim_fields(fields: dict, limit: int = 24) -> dict:
    """截断 fields 供 prompt 展示（避免超长；占位符/空值原样保留信息量）。"""
    if not isinstance(fields, dict):
        return {}
    out = {}
    for i, (k, v) in enumerate(fields.items()):
        if i >= limit:
            break
        out[str(k)] = v
    return out


def _extract_json_obj(text: str) -> Optional[dict]:
    """从 LLM 原始文本里提取首个平衡的 JSON 对象（括号深度扫描，防贪婪误吞）。"""
    if not text:
        return None
    s = str(text)
    # 快路径：整体就是 JSON
    try:
        obj = json.loads(s)
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    start = s.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(s)):
            ch = s[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    frag = s[start:i + 1]
                    try:
                        obj = json.loads(frag)
                        if isinstance(obj, dict):
                            return obj
                    except Exception:
                        break
        start = s.find("{", start + 1)
    return None


def _as_bool(v) -> Optional[bool]:
    """宽松布尔解析：true/false / 是/否 / 1/0。无法判定返回 None。"""
    if isinstance(v, bool):
        return v
    s = str(v).strip().lower()
    if s in ("true", "1", "yes", "是", "y"):
        return True
    if s in ("false", "0", "no", "否", "n"):
        return False
    return None


def _build_prompt(consumer, cand_cols: list[dict], producers_seen: dict) -> str:
    """构造判定 prompt。cand_cols: [{col, producer_idx, to_stem, to_sheet, to_col, desc}]。"""
    c_stem = _stem_of_hint(getattr(consumer, "table_hint", None))
    c_sheet = (getattr(consumer, "sheet_hint", None) or "").strip()
    c_fields = _trim_fields((consumer.extras or {}).get("fields", {}))
    c_raw = (getattr(consumer, "raw", "") or "").strip()

    lines: list[str] = []
    lines.append("你在校对游戏配置表的跨表新增数据。本批指令同时新增了多条记录，")
    lines.append("需要判断某个新增行的「外键列」是否应当引用本批里另一个正在新增的实体。")
    lines.append("")
    lines.append("【本批正在新增、可被引用的实体】")
    for pidx, p in producers_seen.items():
        p_stem = _stem_of_hint(getattr(p, "table_hint", None))
        p_sheet = (getattr(p, "sheet_hint", None) or "").strip()
        p_fields = _trim_fields((p.extras or {}).get("fields", {}))
        p_raw = (getattr(p, "raw", "") or "").strip()
        lines.append(f"- 实体#{pidx}：表 {p_stem}/{p_sheet}；原文「{p_raw}」；"
                     f"已知字段 {json.dumps(p_fields, ensure_ascii=False)}")
    lines.append("")
    lines.append("【待判定的新增行（消费方）】")
    lines.append(f"- 表 {c_stem}/{c_sheet}；原文「{c_raw}」；"
                 f"已知字段 {json.dumps(c_fields, ensure_ascii=False)}")
    lines.append("")
    lines.append("【待判定的外键列】（按表关系指向上述实体所在表，当前为空或缺失）")
    for c in cand_cols:
        desc = c.get("desc") or ""
        lines.append(
            f"- 列「{c['col']}」→ 外键指向实体#{c['producer_idx']} 所在表 "
            f"{c['to_stem']}/{c['to_sheet']}.{c['to_col']}"
            + (f"（关系说明：{desc}）" if desc else ""))
    lines.append("")
    lines.append("判断规则：")
    lines.append("- 若原文语义表明该列引用的正是本批新增的那个实体（如"
                 "「新增X…X进化/升级/来源/由X…」把 X 当作本行的来源/前置/父）→ true")
    lines.append("- 若该列指向的是另一个已存在或不同的实体（原文另给了具体 ID/名称）→ false")
    lines.append("- 拿不准 → false（保守，不乱填）")
    lines.append("")
    lines.append('只输出 JSON，形如 {"links":{"列名":true,"列名2":false}}，不要任何解释。')
    return "\n".join(lines)


def link_cross_refs(intents: list, llm_call: Optional[LLMCall],
                    *, thinking: Optional[Callable[[str, str], None]] = None) -> list:
    """对意图列表做跨记录引用的 LLM 判定（原地修改 + 返回）。

    仅在存在「缺失/空白的跨表外键 + 同指令 producer」时才调 LLM；否则零调用、
    零改动。llm_call 为 None 或任何异常 → 全部降级为不改动（零回归）。
    """
    if not intents or llm_call is None:
        return intents
    try:
        add_idxs = [i for i, it in enumerate(intents)
                    if it is not None and getattr(it, "action", "") == "add"]
        if len(add_idxs) < 2:
            return intents

        try:
            rels = RelationGraph.load().relations
        except Exception:
            return intents
        if not rels:
            return intents

        # (stem, sheet) -> add intent idx（保留首个，与 produces_inference 一致）
        add_keys: dict[tuple, int] = {}
        for i in add_idxs:
            it = intents[i]
            stem = _stem_of_hint(getattr(it, "table_hint", None))
            if not stem:
                continue
            add_keys.setdefault(_sheet_key(stem, getattr(it, "sheet_hint", None)), i)
        if len(add_keys) < 2:
            return intents

        # consumer_idx -> {"cands": [...], "producers": {pidx: intent}}
        by_consumer: dict[int, dict] = {}
        for r in rels:
            if getattr(r, "relation_type", "foreign_key") not in ("foreign_key",):
                continue
            from_key = _sheet_key(_stem_of_path(r.from_path), r.from_sheet)
            to_key = _sheet_key(_stem_of_path(r.to_path), r.to_sheet)
            if from_key not in add_keys or to_key not in add_keys:
                continue
            cidx = add_keys[from_key]
            pidx = add_keys[to_key]
            if cidx == pidx:
                continue  # 自引用不处理
            consumer = intents[cidx]
            fields = (consumer.extras or {}).get("fields", {})
            has_key, is_blank = _consumer_has_col(fields, r.from_column)
            # 已有显式值/占位符（非空白）→ 尊重既有决策，不判定
            if has_key and not is_blank:
                continue
            slot = by_consumer.setdefault(cidx, {"cands": [], "producers": {}})
            slot["cands"].append({
                "col": r.from_column,
                "producer_idx": pidx,
                "to_stem": _stem_of_path(r.to_path),
                "to_sheet": r.to_sheet,
                "to_col": r.to_column,
                "desc": getattr(r, "description", ""),
            })
            slot["producers"][pidx] = intents[pidx]

        if not by_consumer:
            return intents

        changed = 0
        for cidx, slot in by_consumer.items():
            consumer = intents[cidx]
            cand_cols = slot["cands"]
            # 去重同名列（同列多关系只判一次）
            seen_cols = set()
            uniq_cands = []
            for c in cand_cols:
                if c["col"] in seen_cols:
                    continue
                seen_cols.add(c["col"])
                uniq_cands.append(c)
            prompt = _build_prompt(consumer, uniq_cands, slot["producers"])
            try:
                raw = llm_call(prompt)
            except Exception:
                logger.warning("cross_ref_linker LLM 调用异常，跳过", exc_info=True)
                continue
            obj = _extract_json_obj(raw or "")
            if not obj:
                continue
            links = obj.get("links")
            if not isinstance(links, dict):
                continue
            extras = consumer.extras if getattr(consumer, "extras", None) is not None else {}
            fields = extras.get("fields")
            if not isinstance(fields, dict):
                fields = {}
            for c in uniq_cands:
                col = c["col"]
                decision = _as_bool(links.get(col))
                if decision is not True:
                    continue
                # 二次确认该列仍空白/缺失（LLM 期间未被其他层填过）
                has_key, is_blank = _consumer_has_col(fields, col)
                if has_key and not is_blank:
                    continue
                plabel = _producer_label(intents[c["producer_idx"]])
                placeholder = f"<{plabel}>"
                # 若已有匹配键（空白）则改其值，否则按列名新增键
                target_key = None
                for k in list(fields.keys()):
                    if _field_matches_col(k, col):
                        target_key = k
                        break
                fields[target_key or col] = placeholder
                # 同步 consumes_labels（与 DecomposeAgent 契约一致）
                cl = getattr(consumer, "consumes_labels", None)
                if isinstance(cl, list) and plabel not in cl:
                    cl.append(plabel)
                changed += 1
                if thinking:
                    try:
                        thinking("解析",
                                 f"跨记录引用：{c['to_stem']}.{col} 判定引用本批新增实体 "
                                 f"→ 填占位符 {placeholder}")
                    except Exception:
                        pass
            extras["fields"] = fields
            consumer.extras = extras

        if changed:
            logger.info("cross_ref_linker 注入 %d 处跨记录外键占位符", changed)
        return intents
    except Exception:
        logger.warning("cross_ref_linker 整体异常，保留原 intent", exc_info=True)
        return intents


__all__ = ["link_cross_refs"]

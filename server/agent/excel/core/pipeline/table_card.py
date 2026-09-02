"""最小清单#2 + 路线A#1：TableCard 轻量表卡（纯函数，0 LLM）。

背景（docs §"LLM 不应该背 schema" + §2"建立 Table Card 索引"）：不要把 row1/row2
完整表头整块塞给 LLM。先为每个 sheet 生成轻量 card（用途/主键/必填/FK/别名/命中列），
检索阶段只给 top cards，完整 schema 仅在编译某表时才拉。

本模块只做**确定性派生**（从 headers/type_row/PK/别名派生 card + 渲染紧凑文本），
纯函数、无 IO、无 LLM。先用于日志/shadow prompt，不改现有主链路（符合灰度纪律）。
"""
from __future__ import annotations

import re
from typing import Iterable, Optional

__all__ = ["build_table_card", "render_card_text"]

_FK_HINT_RE = re.compile(r"(_id$|id$|编号$)")


def _clean(h: object) -> str:
    return str(h if h is not None else "").split(":")[0].strip()


def _type_of(type_row_cell: object) -> str:
    s = str(type_row_cell if type_row_cell is not None else "")
    # type row 常形如 "value:string" / "int" / "编号:int"
    if ":" in s:
        return s.split(":", 1)[1].strip().lower()
    return s.strip().lower()


def build_table_card(
    stem: str,
    sheet: str,
    headers: Iterable[object],
    type_row: Optional[Iterable[object]] = None,
    *,
    pk_cols: Optional[Iterable[object]] = None,
    fk_map: Optional[dict] = None,
    aliases: Optional[Iterable[object]] = None,
    hit_columns: Optional[Iterable[object]] = None,
    purpose: str = "",
) -> dict:
    """从 schema 派生轻量 table card。

    Args:
        headers: 列名列表（可含 :type 后缀）。
        type_row: 类型行（与 headers 对齐），用于识别 int/string。
        pk_cols: 主键列名（rules primary_key overlay）；空时取首个含 id/编号 的列。
        fk_map: {列名: "目标表.目标列"} FK 声明。
        aliases: 该表别名。
        hit_columns: 本次用户输入命中的列（用于检索排序展示）。
        purpose: 一句话用途（可选）。

    Returns: 结构化 card dict（另含 card_text 紧凑文本）。
    """
    hdr = [_clean(h) for h in (headers or []) if _clean(h)]
    trow = list(type_row or [])
    fk_map = dict(fk_map or {})
    alias_list = [str(a).strip() for a in (aliases or []) if str(a).strip()]
    hits = [_clean(c) for c in (hit_columns or []) if _clean(c)]

    # 主键：优先 pk_cols，否则首个含 id/编号 的列，再否则首列
    pk = [_clean(c) for c in (pk_cols or []) if _clean(c)]
    if not pk and hdr:
        for h in hdr:
            hl = h.lower()
            if "id" in hl or "编号" in hl:
                pk = [h]
                break
        if not pk:
            pk = [hdr[0]]

    # FK 列：来自 fk_map 键，或列名形态启发（_id/编号，排除主键）
    fk_cols: list[dict] = []
    for i, h in enumerate(hdr):
        target = fk_map.get(h)
        if target:
            fk_cols.append({"column": h, "target": str(target)})
        elif h not in pk and _FK_HINT_RE.search(h.lower()):
            fk_cols.append({"column": h, "target": ""})

    # 必填列（轻量近似）：主键 + FK 列（业务必填由 required_fields 另行提供，
    # 这里只做 card 展示，不做校验）
    required = list(dict.fromkeys(pk + [f["column"] for f in fk_cols]))

    cols = []
    raw_hdr = [str(h if h is not None else "") for h in (headers or []) if _clean(h)]
    for i, h in enumerate(hdr):
        if i < len(trow) and _type_of(trow[i]):
            t = _type_of(trow[i])
        else:
            # 无 type_row 时回退到表头自带的 :type 后缀
            _raw = raw_hdr[i] if i < len(raw_hdr) else ""
            t = _raw.split(":", 1)[1].strip().lower() if ":" in _raw else ""
        cols.append({"name": h, "type": t})

    card = {
        "stem": str(stem or ""),
        "sheet": str(sheet or ""),
        "purpose": str(purpose or ""),
        "primary_key": pk,
        "required_columns": required,
        "fk_columns": fk_cols,
        "aliases": alias_list,
        "hit_columns": hits,
        "columns": cols,
    }
    card["card_text"] = render_card_text(card)
    return card


def render_card_text(card: dict) -> str:
    """把 card 渲染成紧凑单块文本（供 shadow prompt / 日志），远短于完整表头块。"""
    stem = card.get("stem", "")
    sheet = card.get("sheet", "")
    pk = "、".join(card.get("primary_key") or []) or "-"
    fk = card.get("fk_columns") or []
    fk_txt = "、".join(
        (f"{f['column']}→{f['target']}" if f.get("target") else f["column"])
        for f in fk) or "-"
    aliases = "、".join(card.get("aliases") or []) or "-"
    hits = "、".join(card.get("hit_columns") or []) or "-"
    purpose = card.get("purpose") or "-"
    lines = [
        f"- {stem}/{sheet}",
        f"  用途: {purpose}",
        f"  主键: {pk}",
        f"  FK: {fk_txt}",
        f"  别名: {aliases}",
        f"  命中列: {hits}",
    ]
    return "\n".join(lines)

"""Decompose schema 预算裁剪（文档「候选分层 + schema_budget」MVP #4）。

动机（诊断文档已核实问题 #2/#3）：DecomposeAgent 把所有候选表的 row1/row2 表头
无差别注入 prompt，候选/FK 扩展一多，prompt 就膨胀 → codemaker serve 变慢甚至超时；
现状只能靠调大 CODEMAKER_DECOMPOSE_TIMEOUT 掩盖，属治标。

本模块提供**纯函数**预算裁剪（0 LLM、无副作用、确定性）：按候选分层
（candidate_grouping 的 required/dependency/context）决定每张表的注入粒度——
  - required：完整列（动作主语表，LLM 必须看清）。
  - dependency：仅主键 + 命中列 + id/编号 类列（够判 FK 关系即可）。
  - context：默认不注入（弱旁证表，别吃 token）。

只有「启用预算(char_budget>0) 且 完整 schema 超预算」时才裁剪；否则原样返回，
保证关闭(默认)时零行为改动。目标是**缩小问题**（prompt 变短）而非拉长 timeout。
"""
from __future__ import annotations

from typing import Iterable, Optional


def _looks_id(col: str) -> bool:
    """列名是否 id/编号 类（FK/主键判定所需的最小信息）。"""
    c = str(col or "")
    low = c.split("（", 1)[0].split("(", 1)[0]
    return ("id" in low.lower()) or ("编号" in low)


def render_full(record: dict) -> str:
    """完整列渲染：'- stem/sheet: c1 | c2 | ...'。"""
    cols = record.get("cols", []) or []
    return f"- {record.get('stem','')}/{record.get('sheet','')}: " + " | ".join(cols)


def render_summary(record: dict, *, max_cols: int = 6) -> str:
    """摘要渲染：主键（首列）+ 命中列 + id/编号 类列，去重保序、限量。"""
    cols = record.get("cols", []) or []
    sig = set(record.get("sig_cols", set()) or set())
    picked: list[str] = []
    seen: set = set()

    def _add(c: str):
        if c and c not in seen:
            seen.add(c)
            picked.append(c)

    if cols:
        _add(cols[0])                       # 主键（首列）
    for c in cols:                           # 命中列
        if c in sig:
            _add(c)
    for c in cols:                           # id/编号 类
        if _looks_id(c):
            _add(c)
        if len(picked) >= max_cols:
            break
    return f"- {record.get('stem','')}/{record.get('sheet','')}: " + " | ".join(picked[:max_cols])


def total_chars(lines: Iterable[str]) -> int:
    """行集合近似字符体量（含换行）。"""
    return sum(len(l) + 1 for l in lines)


def apply_schema_budget(records: list[dict], groups: Optional[dict],
                        char_budget: int, *, summary_max_cols: int = 6) -> tuple[list[str], bool]:
    """按分层 + 字符预算裁剪 schema 行。

    Args:
        records: [{stem, sheet, cols:[...], sig_cols:set}]，按注入顺序。
        groups: candidate_grouping.classify_candidates 结果（required/dependency/context）。
        char_budget: 字符预算；<=0 表示不启用（原样返回完整渲染）。
        summary_max_cols: dependency 表摘要最多列数。

    Returns:
        (lines, applied)。applied=False 表示未裁剪（未启用或未超预算）。

    安全兜底：groups 为空/未知 stem 一律按 required（完整）保留，绝不误删动作主语表。
    """
    full_lines = [render_full(r) for r in records]
    if char_budget <= 0 or total_chars(full_lines) <= char_budget:
        return full_lines, False

    tier_of: dict[str, str] = {}
    if groups:
        for tier in ("required", "dependency", "context"):
            for stem in groups.get(tier, []) or []:
                tier_of.setdefault(stem, tier)

    out: list[str] = []
    for r in records:
        tier = tier_of.get(r.get("stem", ""), "required")  # 未知 → required（安全保留）
        if tier == "context":
            continue  # 弱旁证表：默认不注入
        if tier == "dependency":
            out.append(render_summary(r, max_cols=summary_max_cols))
        else:
            out.append(render_full(r))
    # 极端兜底：全被裁空（都是 context）→ 退回完整，避免 prompt 无 schema 致 LLM 空转
    if not out:
        return full_lines, False
    return out, True


__all__ = ["apply_schema_budget", "render_full", "render_summary", "total_chars"]

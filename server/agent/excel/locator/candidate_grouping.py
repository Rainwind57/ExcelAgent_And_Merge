"""候选表分层（StepTrace §P0 / 文档「候选分层」MVP #3）。

动机（诊断文档「候选池 + FK 扩展撑长 prompt → serve 变慢」）：
  当前 LocatorResult 用单一 `candidates` 平铺承载所有含义——规则强命中的动作主语
  表、列名反查/子串弱命中的旁证表、FK 扩展拉进来的依赖表混在一起。它们进入
  DecomposeAgent 时被无差别注入完整 row1/row2 schema，导致 prompt 膨胀。

本模块提供**纯函数**分层（0 LLM、无副作用、确定性），把候选按语义角色分成三级：
  - required：规则强命中（动作主语表）或 LLM 复核判定真正涉及的表。
  - dependency：由 FK 推导/扩展进来、或与 required 有 FK 边相连的表（用于校验/补引用）。
  - context：仅弱信号（列名反查/子串）命中、且未与 required 经 FK 相连的旁证表。

分层结果先作为**附加观测数据**挂到 LocatorResult.candidate_groups，不改变现有
`candidates` 字段与任何注入逻辑（保持零回归）。后续 schema_budget 阶段可据此决定
「required 给完整 schema，dependency 只给 PK/FK 摘要，context 默认不注入」。
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

# 弱信号 level：列名反查 / 子串命中，属旁证，不作动作主语
_WEAK_LEVELS = {"column_extract", "column_reverse", "substring"}
# FK 派生 level：由关系图扩展/推断进来的依赖表
_FK_LEVELS = {"fk_inferred", "fk_expanded"}


def classify_candidates(candidates: Iterable[Any],
                        fk_edges: Optional[Iterable[Any]] = None,
                        llm_relevant: Optional[Iterable[str]] = None) -> dict:
    """把候选表分成 required / dependency / context 三级（保序、去重）。

    Args:
        candidates: CandidateTable 列表（需含 .stem / .level）。
        fk_edges: FKEdge 列表（含 .from_stem / .to_stem），用于把与 required
            经 FK 相连的弱命中表提升为 dependency。
        llm_relevant: LLM 复核判定「真正涉及」的 stem 集合（视为 required）。

    Returns:
        {"required": [stem...], "dependency": [stem...], "context": [stem...]}
        每个 stem 只出现在一级，顺序沿用 candidates 首次出现的顺序。
    """
    llm_relevant = set(llm_relevant or [])
    tier: dict[str, str] = {}
    order: list[str] = []
    for c in candidates or []:
        stem = getattr(c, "stem", None)
        if not stem or stem in tier:
            continue
        order.append(stem)
        level = (getattr(c, "level", "") or "").strip()
        if stem in llm_relevant:
            tier[stem] = "required"
        elif level in _FK_LEVELS:
            tier[stem] = "dependency"
        elif level in _WEAK_LEVELS:
            tier[stem] = "context"
        else:
            tier[stem] = "required"

    # FK 相连提升：context 表若与某 required 表有 FK 边 → 提升为 dependency
    required_stems = {s for s, t in tier.items() if t == "required"}
    if required_stems:
        for e in fk_edges or []:
            fs = getattr(e, "from_stem", "")
            ts = getattr(e, "to_stem", "")
            for a, b in ((fs, ts), (ts, fs)):
                if tier.get(a) == "context" and b in required_stems:
                    tier[a] = "dependency"

    groups: dict[str, list[str]] = {"required": [], "dependency": [], "context": []}
    for stem in order:
        groups[tier.get(stem, "context")].append(stem)
    return groups


def group_counts(groups: dict) -> dict:
    """三级计数（供 metrics/audit 直接展示）。"""
    return {
        "required_count": len(groups.get("required", []) if groups else []),
        "dependency_count": len(groups.get("dependency", []) if groups else []),
        "context_count": len(groups.get("context", []) if groups else []),
    }


__all__ = ["classify_candidates", "group_counts"]

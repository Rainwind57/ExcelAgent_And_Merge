"""P3：Step3 dry-run / preflight 报告（纯函数，0 LLM）。

背景（docs §P3"Step3 应从逐条执行升级为两阶段提交感：先 dry-run 解析所有行/列/
PK/FK/写位，全过再写"）。整文件事务快照/回滚风险大、非本次范围（step3:296 已注明），
但"先预演产报告"可先落地：把每条 intent 的可写性（目标表可解析、无悬空占位符）
汇成一份 preflight 报告，供执行前判断 + Step4 诊断。

本模块只做确定性汇总（逐条事实 → 报告 + go/no-go），纯函数、无 IO、不写盘。
"""
from __future__ import annotations

from typing import Any, Iterable

__all__ = ["build_preflight_report"]


def build_preflight_report(items: Iterable[dict]) -> dict:
    """把逐条 intent 的预演事实汇成 preflight 报告。

    Args:
        items: 每项 {index, table, sheet, action, resolvable(bool),
                unresolved_placeholders(list), reason(str)}。

    Returns:
        {
          "total": int,
          "ready": int,                 # 可安全写入的条数
          "blocked": int,               # 有阻断的条数
          "ok": bool,                   # 是否全部 ready（可整批安全写）
          "blockers": [ {index, table, sheet, reason, unresolved} ],
        }
    """
    total = 0
    ready = 0
    blockers: list[dict] = []
    for it in (items or []):
        total += 1
        resolvable = bool(it.get("resolvable", True))
        unresolved = list(it.get("unresolved_placeholders") or [])
        if resolvable and not unresolved:
            ready += 1
            continue
        reason = it.get("reason") or (
            "目标表/sheet 无法解析" if not resolvable
            else f"存在未解析上游占位符: {unresolved}")
        blockers.append({
            "index": it.get("index"),
            "table": it.get("table", ""),
            "sheet": it.get("sheet", ""),
            "action": it.get("action", ""),
            "reason": reason,
            "unresolved": unresolved,
        })
    return {
        "total": total,
        "ready": ready,
        "blocked": len(blockers),
        "ok": total > 0 and not blockers,
        "blockers": blockers,
    }

"""Pre-flight hold 机制：apply 前漏行/引用悬挂预检 + pre_commit_hold 事件通道。

ca-overview §2.3.1 痛点：testbranch 全量覆盖丢 id=10500——有 detection 无 prevention。
本模块在 apply 写盘前做漏行预检，命中 hold 则阻断（环境开关控制）+ audit 留痕 +
SSE pre_commit_hold 事件产出（前端拦截卡留后续接入）。

首版范围：后端漏行预检 + CODEMAKER_PREFLIGHT_HOLD 环境开关阻断 + audit 留痕。
SSE 事件产出函数就位，前端拦截卡 + override 弹窗留后续（前端 minified 需重建）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class PreCommitHoldEvent:
    """pre_commit_hold 事件结构（SSE payload + audit extra 通用）。"""
    kind: str            # missing_rows | dangling_refs | formula_loss | comment_loss | ...
    severity: str        # hold | warning
    count: int = 0
    sheets: Dict[str, Any] = field(default_factory=dict)  # {sheet: {lost_ids: [...]}}
    message: str = ""
    recommendation: str = ""  # override | recalc | manual_fix | ...

    def to_dict(self) -> dict:
        return {
            "type": "pre_commit_hold",
            "kind": self.kind,
            "severity": self.severity,
            "count": self.count,
            "sheets": self.sheets,
            "message": self.message,
            "recommendation": self.recommendation,
        }


@dataclass
class PreflightReport:
    """漏行预检报告。"""
    lost_rows: List[dict] = field(default_factory=list)
    # [{table, sheet, id, was_in_base}]
    will_silently_drop: bool = False
    holds: List[PreCommitHoldEvent] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "lost_rows": self.lost_rows,
            "will_silently_drop": self.will_silently_drop,
            "holds": [h.to_dict() for h in self.holds],
        }


def preflight_row_manifest(mr, base_pks: Dict[str, set]) -> PreflightReport:
    """漏行预检：base 行 id 列 ∪ 合并结果行 id 列，lost_ids = base_ids - mergeset_ids。

    ca-overview §2.3.1：apply 前预检"合并此 patch 将丢失哪些 base id"。
    base 有但合并结果没有的 id = 漏行（或用户主动删除，hold 提示性让人确认）。

    Args:
        mr: MergeRequest（合并结果，sheets 含每 sheet 的 rows）
        base_pks: {sheet_name: 落盘 base 文件全量主键集合}（collect_disk_sheet_pks 产出）

    Returns:
        PreflightReport：lost_rows + will_silently_drop + holds
    """
    report = PreflightReport()
    for s in mr.sheets:
        sheet_name = s.name
        base_ids = base_pks.get(sheet_name) or set()
        if not base_ids:
            continue
        # 合并结果行 id 集合
        mergeset_ids = set()
        for r in s.rows:
            if r.key:
                mergeset_ids.add(str(r.key))
        lost = base_ids - mergeset_ids
        if lost:
            lost_sorted = sorted(lost)
            for lid in lost_sorted:
                report.lost_rows.append({
                    "table": mr.group_name,
                    "sheet": sheet_name,
                    "id": lid,
                    "was_in_base": True,
                })
            report.holds.append(PreCommitHoldEvent(
                kind="missing_rows",
                severity="hold",
                count=len(lost_sorted),
                sheets={sheet_name: {"lost_ids": lost_sorted}},
                message=f"合并将丢失 {len(lost_sorted)} 个 base 行 id（{sheet_name}）",
                recommendation="override（确认预期删除）或补回漏行",
            ))
    report.will_silently_drop = bool(report.lost_rows)
    return report


def record_hold_audit(auditor, event: PreCommitHoldEvent, path: str,
                      sheet: str = "", extra: Optional[dict] = None) -> None:
    """记 pre_commit_hold audit 留痕（复用 BackupAuditor.record）。

    失败静默（不阻断主流程）。merge_branch 路径若用 _append_audit 模式，调用方
    自行将 event.to_dict() 并入 _append_audit 的 tables 字段。
    """
    if auditor is None:
        return
    try:
        audit_extra = event.to_dict()
        audit_extra.pop("type", None)
        if extra:
            audit_extra.update(extra)
        auditor.record(
            operation="pre_commit_hold",
            path=path,
            sheet=sheet,
            extra=audit_extra,
        )
    except Exception:
        pass


def emit_hold_sse(task_emit_fn, task_id: str, event: PreCommitHoldEvent) -> None:
    """推 pre_commit_hold SSE 事件（复用 _compare_task_emit pattern）。

    task_emit_fn: _compare_task_emit 或同等 emit 函数（task_id, event_dict）。
    首版前端无消费分支，事件产出就位待前端接。
    """
    if task_emit_fn is None or not task_id:
        return
    try:
        task_emit_fn(task_id, event.to_dict())
    except Exception:
        pass

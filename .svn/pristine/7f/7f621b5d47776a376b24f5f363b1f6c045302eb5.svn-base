"""操作安全与回滚机制：备份 + 审计日志 + 回滚。

流程：
  - 备份：执行表格修改操作前，把原文件复制到 backups/ 目录，文件名带微秒时间戳。
  - 审计：记录操作类型、目标文件、Sheet、列、操作时间、操作前后的值、备份文件路径。
  - 回滚：根据审计日志条目或备份文件名，把备份还原回原路径（回滚前会再次备份当前版本）。

审计日志采用 JSONL（每行一条），追加写入，便于长期累积与回溯。
backups/ 与 audit_log.jsonl 默认放在 server/backups/，可通过参数覆盖。
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, asdict, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


@dataclass
class AuditEntry:
    """单条审计记录。"""
    timestamp: str                       # ISO8601 带微秒
    operation: str                       # add_column / write_cell / append_row / delete_row / rollback / ...
    path: str                            # 目标文件相对 workspace 的路径
    sheet: str = ""
    column: str = ""
    before: Any = None                   # 操作前的值/状态
    after: Any = None                    # 操作后的值/状态
    backup_file: Optional[str] = None    # 备份文件相对 backups_dir 的路径（无备份则 None）
    extra: dict = field(default_factory=dict)


def _now() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def _now_iso() -> str:
    return datetime.now().isoformat(timespec="microseconds")


class BackupAuditor:
    """备份 + 审计 + 回滚管理器。

    Args:
        workspace: 资源根目录（resources/），用于解析相对路径。
        backups_dir: 备份目录，默认 server/backups/。
        audit_log_path: 审计日志 JSONL 路径，默认 server/backups/audit_log.jsonl。
    """

    def __init__(self, workspace: Path | str,
                 backups_dir: Path | str | None = None,
                 audit_log_path: Path | str | None = None):
        self.workspace = Path(workspace)
        # 默认 server/backups/（agent 父级再上一级的 backups）
        default_backups = Path(__file__).resolve().parent.parent.parent / "backups"
        self.backups_dir = Path(backups_dir) if backups_dir else default_backups
        self.backups_dir.mkdir(parents=True, exist_ok=True)
        self.audit_log_path = (Path(audit_log_path) if audit_log_path
                               else self.backups_dir / "audit_log.jsonl")

    # ── 备份 ─────────────────────────────────────────────────

    def backup(self, path: Path | str) -> Optional[str]:
        """把目标文件复制到 backups/，返回备份文件相对 backups_dir 的路径。

        文件不存在或复制失败时返回 None（不抛异常，保证主流程不被备份失败打断）。
        备份文件名：{stem}_{YYYYMMDD_HHMMSS_ffffff}.xlsx
        """
        src = self._abs(path)
        if not src.exists() or not src.is_file():
            return None
        stem = src.stem
        backup_name = f"{stem}_{_now()}{src.suffix}"
        backup_path = self.backups_dir / backup_name
        try:
            shutil.copy2(src, backup_path)
        except Exception:
            return None
        return backup_name

    # ── 审计 ─────────────────────────────────────────────────

    def record(self, operation: str, path: str, sheet: str = "",
               column: str = "", before: Any = None, after: Any = None,
               backup_file: Optional[str] = None, extra: Optional[dict] = None) -> AuditEntry:
        """追加一条审计记录到 JSONL，返回 AuditEntry。"""
        entry = AuditEntry(
            timestamp=_now_iso(),
            operation=operation,
            path=path,
            sheet=sheet,
            column=column,
            before=before,
            after=after,
            backup_file=backup_file,
            extra=extra or {},
        )
        line = json.dumps(asdict(entry), ensure_ascii=False)
        # buffering=1 行缓冲：table_case_eval / multi 并发追加时单行完整 flush，
        # 降低多线程/多进程同时 append 致行交错截断的风险
        with open(self.audit_log_path, "a", encoding="utf-8", buffering=1) as f:
            f.write(line + "\n")
        return entry

    def backup_and_record(self, operation: str, path: str, **kwargs) -> AuditEntry:
        """便捷方法：先备份再记录审计（最常用的"操作前"调用）。

        kwargs 透传给 record（sheet/column/before/after/extra）。
        备份成功时 backup_file 自动填入。
        """
        backup_file = self.backup(path)
        return self.record(operation, path, backup_file=backup_file, **kwargs)

    def log_entries(self) -> list[AuditEntry]:
        """读取全部审计日志条目（按时间顺序）。损坏行跳过。"""
        if not self.audit_log_path.exists():
            return []
        out: list[AuditEntry] = []
        with open(self.audit_log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    out.append(AuditEntry(**d))
                except Exception:
                    continue
        return out

    def recent_entries(self, n: int = 20) -> list[AuditEntry]:
        """返回最近 n 条审计记录。"""
        entries = self.log_entries()
        return entries[-n:]

    # ── 回滚 ─────────────────────────────────────────────────

    def rollback_to_backup(self, backup_file: str, target_path: Optional[str] = None) -> bool:
        """把指定备份文件还原回原路径。

        Args:
            backup_file: 备份文件相对 backups_dir 的路径（或绝对路径）。
            target_path: 还原目标相对 workspace 的路径；None 时从审计日志反查该备份
                         最近一次记录的 path。

        回滚前会先把当前文件再备份一次并记录审计（operation="rollback"），
        保证回滚本身可追溯。
        """
        backup_path = self._resolve_backup(backup_file)
        if not backup_path.exists():
            return False
        if target_path is None:
            target_path = self._find_path_for_backup(backup_file)
            if target_path is None:
                return False
        target_abs = self._abs(target_path)
        # 回滚前再备份当前版本
        pre_backup = self.backup(target_path)
        try:
            shutil.copy2(backup_path, target_abs)
        except Exception:
            return False
        self.record(
            operation="rollback",
            path=target_path,
            backup_file=pre_backup,
            extra={"restored_from": backup_file},
        )
        return True

    def rollback_by_index(self, index: int) -> bool:
        """按审计日志索引回滚：还原该条记录对应的 backup_file 到其 path。

        Args:
            index: log_entries() 中的下标（0-based）。
        """
        entries = self.log_entries()
        if index < 0 or index >= len(entries):
            return False
        e = entries[index]
        if not e.backup_file:
            return False
        return self.rollback_to_backup(e.backup_file, target_path=e.path)

    # ── 内部工具 ─────────────────────────────────────────────

    def _abs(self, path: Path | str) -> Path:
        """把相对 workspace 的路径转绝对路径；已是绝对路径则直接返回。"""
        p = Path(path)
        if p.is_absolute():
            return p
        return self.workspace / p

    def _resolve_backup(self, backup_file: str) -> Path:
        p = Path(backup_file)
        if p.is_absolute():
            return p
        return self.backups_dir / p

    def _find_path_for_backup(self, backup_file: str) -> Optional[str]:
        """从审计日志反查某备份文件最近一次记录的目标 path。"""
        for e in reversed(self.log_entries()):
            if e.backup_file == backup_file and e.path:
                return e.path
        return None


if __name__ == "__main__":
    # 自测：在一个临时副本上跑 备份→记录→回滚 流程
    import tempfile, os
    tmp = Path(tempfile.mkdtemp())
    ws = tmp / "data"
    ws.mkdir()
    src = ws / "demo.xlsx"
    # 用 openpyxl 建一个最小 xlsx
    import openpyxl
    wb = openpyxl.Workbook(); wb.active.title = "S"; wb.active["A1"] = "v1"; wb.save(src)

    ba = BackupAuditor(workspace=ws, backups_dir=tmp / "bk", audit_log_path=tmp / "audit.jsonl")
    print("backup:", ba.backup(src))
    e = ba.backup_and_record("write_cell", "demo.xlsx", sheet="S", column="A1",
                             before="v1", after="v2")
    print("recorded:", e.operation, e.backup_file)
    print("entries:", len(ba.log_entries()))
    ok = ba.rollback_to_backup(e.backup_file, target_path="demo.xlsx")
    print("rollback ok:", ok)
    wb2 = openpyxl.load_workbook(src)
    print("after rollback A1:", wb2["S"]["A1"].value)
    shutil.rmtree(tmp, ignore_errors=True)

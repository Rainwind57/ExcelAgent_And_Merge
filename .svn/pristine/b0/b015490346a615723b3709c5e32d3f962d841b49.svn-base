"""merge 比对结果历史记录（R13）。

保留时长 1 天，过期自动清理。
存储位置：merge/history/{session_id}.json
"""
from __future__ import annotations

import json
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from config import MERGE_HISTORY_DIR, MERGE_HISTORY_TTL_SECONDS


def _ensure_dir() -> None:
    MERGE_HISTORY_DIR.mkdir(parents=True, exist_ok=True)


def _history_path(session_id: str) -> Path:
    return MERGE_HISTORY_DIR / f"{session_id}.json"


def _now_ts() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _summarize_sheets(groups: dict) -> list[dict]:
    """从 CompareResponse.groups 提取每个 sheet 的裁决摘要。

    groups 形如 {prefix: FileGroup}，FileGroup.sheets 为 {sheet_name: SheetDiff}。
    SheetDiff.rows 每项含 key/cells/conflict/changed/versions。
    """
    sheets_out: list[dict] = []
    for prefix, group in groups.items():
        group_sheets = getattr(group, "sheets", {}) or {}
        if isinstance(group_sheets, dict):
            for sname, sdiff in group_sheets.items():
                rows = getattr(sdiff, "rows", []) or []
                resolved = 0
                conflicts_remaining = 0
                adopted_map: dict[str, str] = {}
                for row in rows:
                    cells = getattr(row, "cells", []) or []
                    key = getattr(row, "key", "")
                    row_type = getattr(row, "row_type", "") or ""
                    for cell in cells:
                        conflict = getattr(cell, "conflict", False)
                        versions = getattr(cell, "versions", {}) or {}
                        if conflict:
                            conflicts_remaining += 1
                        elif versions:
                            # 非冲突但有版本值 → 已采纳某版本
                            resolved += 1
                            if key:
                                # 取第一个非空版本作为采纳值
                                for vname, vval in versions.items():
                                    if vval is not None:
                                        adopted_map[f"{key}:{getattr(cell,'col_letter','')}"] = vname
                                        break
                sheets_out.append({
                    "group": prefix,
                    "name": sname,
                    "resolved_count": resolved,
                    "conflicts_remaining": conflicts_remaining,
                    "adopted_map": adopted_map,
                })
    return sheets_out


def record_compare(session_id: str, groups: dict,
                   base_file: str = "", derived_files: Optional[list] = None) -> None:
    """compare 后记录初始比对状态。"""
    _ensure_dir()
    _cleanup_expired()
    entry = {
        "session_id": session_id,
        "ts": _now_ts(),
        "base_file": base_file,
        "derived_files": derived_files or [],
        "sheets": _summarize_sheets(groups),
        "exported": False,
    }
    _history_path(session_id).write_text(
        json.dumps(entry, ensure_ascii=False), encoding="utf-8"
    )


def record_merge(session_id: str, group_name: str, sheets: list,
                 exported: bool = True) -> None:
    """merge 导出后更新历史记录，标记 exported=True 并刷新 sheet 摘要。

    sheets 直接用前端提交的 MergeRequest.sheets（已是 list[SheetData]）。
    """
    _ensure_dir()
    _cleanup_expired()
    path = _history_path(session_id)
    entry: dict = {}
    if path.is_file():
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            entry = {}
    entry["session_id"] = session_id
    entry["ts"] = _now_ts()
    entry["group_name"] = group_name
    entry["exported"] = exported

    # 从 MergeRequest.sheets 提取最终裁决摘要
    sheets_out: list[dict] = []
    for sd in sheets or []:
        sname = getattr(sd, "name", "") or (sd.get("name", "") if isinstance(sd, dict) else "")
        rows = getattr(sd, "rows", []) or (sd.get("rows", []) if isinstance(sd, dict) else [])
        resolved = 0
        conflicts_remaining = 0
        adopted_map: dict[str, str] = {}
        for row in rows:
            row_type = getattr(row, "row_type", "") or (row.get("row_type", "") if isinstance(row, dict) else "")
            if row_type == "deleted":
                continue
            cells = getattr(row, "cells", []) or (row.get("cells", []) if isinstance(row, dict) else [])
            key = getattr(row, "key", "") or (row.get("key", "") if isinstance(row, dict) else "")
            for cell in cells:
                conflict = getattr(cell, "conflict", False) or (cell.get("conflict", False) if isinstance(cell, dict) else False)
                versions = getattr(cell, "versions", {}) or (cell.get("versions", {}) if isinstance(cell, dict) else {})
                if conflict:
                    conflicts_remaining += 1
                elif versions:
                    resolved += 1
                    if key:
                        for vname, vval in versions.items():
                            if vval is not None:
                                col_letter = getattr(cell, "col_letter", "") or (cell.get("col_letter", "") if isinstance(cell, dict) else "")
                                adopted_map[f"{key}:{col_letter}"] = vname
                                break
        sheets_out.append({
            "name": sname,
            "resolved_count": resolved,
            "conflicts_remaining": conflicts_remaining,
            "adopted_map": adopted_map,
        })
    entry["sheets"] = sheets_out
    path.write_text(json.dumps(entry, ensure_ascii=False), encoding="utf-8")


def list_history(since_hours: int = 24) -> list[dict]:
    """返回近 since_hours 小时内的历史记录列表（按 ts 倒序）。"""
    _ensure_dir()
    _cleanup_expired()
    cutoff = time.time() - since_hours * 3600
    items: list[dict] = []
    for p in MERGE_HISTORY_DIR.glob("*.json"):
        if p.stat().st_mtime < cutoff:
            continue
        try:
            entry = json.loads(p.read_text(encoding="utf-8"))
            items.append({
                "session_id": entry.get("session_id", p.stem),
                "ts": entry.get("ts", ""),
                "base_file": entry.get("base_file", ""),
                "derived_files": entry.get("derived_files", []),
                "group_name": entry.get("group_name", ""),
                "exported": entry.get("exported", False),
                "sheets_count": len(entry.get("sheets", [])),
                "conflicts_remaining": sum(s.get("conflicts_remaining", 0) for s in entry.get("sheets", [])),
            })
        except Exception:
            continue
    items.sort(key=lambda x: x.get("ts", ""), reverse=True)
    return items


def get_history(session_id: str) -> Optional[dict]:
    """返回指定 session 的完整历史记录。"""
    _ensure_dir()
    path = _history_path(session_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _cleanup_expired() -> int:
    """删除 mtime 超过 TTL 的历史文件，返回删除数。"""
    _ensure_dir()
    cutoff = time.time() - MERGE_HISTORY_TTL_SECONDS
    deleted = 0
    for p in MERGE_HISTORY_DIR.glob("*.json"):
        try:
            if p.stat().st_mtime < cutoff:
                p.unlink()
                deleted += 1
        except Exception:
            continue
    return deleted

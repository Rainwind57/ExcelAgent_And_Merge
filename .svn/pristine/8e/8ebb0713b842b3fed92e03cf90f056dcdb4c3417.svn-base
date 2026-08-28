"""Schema 血缘联动（方法 E）：trunk 加列后预检 _capped.xlsx / ca/dev/* 同名表缺此列。

建图：扫 resources_dir + 子分支根目录所有 .xlsx 的数据 sheet 列定义（header+type 行），
合成 {table_stem -> {sheet -> {column -> {type, not_empty, present_in_branches}}}}。
对比 trunk 主表 vs capped/dev 同名表列集差异，输出缺列清单。

schema 复用 schema_infer.scan_sheet（读 row1=header, row2=type, row3=constraints），
但 scan_sheet 跳过 CONFIG sheet — 本模块直接读数据 sheet（非 CONFIG），不依赖 CONFIG 解析。

接入方式：
  - 独立路由 `POST /api/structural/sync_preview?table=xxx&sheet=yyy` 返回缺列文件清单
  - structural.py compute_structural_changes 增 column_added/column_changed kind
  - agent 写 trunk 主表加列前可调本预检（E4 follow-up，需 testtest 有 capped 目录）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ColumnLineageEntry:
    """单列血缘条目。"""
    column: str
    col_type: str = ""
    not_empty: bool = False
    present_in_branches: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "column": self.column,
            "type": self.col_type,
            "not_empty": self.not_empty,
            "present_in_branches": self.present_in_branches,
        }


@dataclass
class ColumnLineageGraph:
    """全目录列血缘图。

    {table_stem -> {sheet -> {column_name -> ColumnLineageEntry}}}。
    present_in_branches 记录该列出现在哪些分支根目录（trunk/capped/dev 等）。
    """
    tables: dict[str, dict[str, dict[str, ColumnLineageEntry]]] = field(default_factory=dict)
    scanned_files: int = 0
    branches_scanned: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "tables": {
                t: {s: {c: e.to_dict() for c, e in cols.items()}
                    for s, cols in sheets.items()}
                for t, sheets in self.tables.items()
            },
            "scanned_files": self.scanned_files,
            "branches_scanned": self.branches_scanned,
        }

    def sync_preview(self, table: str, sheet: str) -> dict:
        """预检：table/sheet 在 trunk 有但其他分支缺的列清单。

        返回 {table, sheet, trunk_columns, missing_in_branches: [{branch, missing_columns}]}。
        trunk_columns = 该 table/sheet 在 trunk 分支的列集；
        missing_in_branches = 其他分支同名 table/sheet 缺的列（相对 trunk）。
        无 trunk 数据 → 空 missing。
        """
        result = {"table": table, "sheet": sheet, "trunk_columns": [],
                  "missing_in_branches": []}
        sheets = self.tables.get(table)
        if not sheets:
            return result
        cols = sheets.get(sheet)
        if not cols:
            return result
        # trunk 列 = present_in_branches 含 "trunk" 的列
        trunk_cols = [c for c, e in cols.items() if "trunk" in e.present_in_branches]
        result["trunk_columns"] = trunk_cols
        # 各分支的列集（按 present_in_branches 反查）
        branch_cols: dict[str, set[str]] = {}
        for c, e in cols.items():
            for b in e.present_in_branches:
                branch_cols.setdefault(b, set()).add(c)
        for b in self.branches_scanned:
            if b == "trunk":
                continue
            b_cols = branch_cols.get(b, set())
            missing = [c for c in trunk_cols if c not in b_cols]
            if missing:
                result["missing_in_branches"].append({
                    "branch": b, "missing_columns": missing,
                })
        return result


def _scan_sheet_columns(ws) -> Optional[dict[str, dict]]:
    """扫单 sheet 列定义 → {column_name: {type, not_empty}}。

    复用 schema_infer 的 row1=header + row2=type + row3=constraints 约定，
    但不跳过 CONFIG（本模块需读所有 sheet 列定义做血缘对比）。
    返回 None 表示无有效列（空 sheet）。
    """
    try:
        rows = list(ws.iter_rows(min_row=1, max_row=4, values_only=True))
    except Exception:
        return None
    if not rows or not rows[0]:
        return None
    headers_raw = rows[0]
    types_raw = rows[1] if len(rows) > 1 else []
    constraints_raw = rows[2] if len(rows) > 2 else []
    max_col = len(headers_raw)
    while max_col > 0 and headers_raw[max_col - 1] is None:
        max_col -= 1
    if max_col == 0:
        return None
    out: dict[str, dict] = {}
    for i in range(max_col):
        h = headers_raw[i]
        if h is None:
            continue
        col_name = str(h).strip()
        if not col_name:
            continue
        t = types_raw[i] if i < len(types_raw) else None
        c = constraints_raw[i] if i < len(constraints_raw) else None
        out[col_name] = {
            "type": str(t) if t is not None else "",
            "not_empty": bool(c and str(c).strip() in ("1", "True", "true", "必填")),
        }
    return out


def compute_column_lineage(branch_roots: list[Path]) -> ColumnLineageGraph:
    """扫多分支根目录建列血缘图（方法 E1）。

    对每 branch_root 重复扫描所有 .xlsx 数据 sheet 列定义，合并到同一 graph。
    location 维度 = branch（根目录名），列的 present_in_branches 记录出现在哪些分支。
    同分支同 stem 跨 sheet 不合并（sheet 维度独立）。向后兼容：单根退化为单分支。
    """
    import openpyxl
    graph = ColumnLineageGraph()
    if not branch_roots:
        return graph
    for root in branch_roots:
        branch_name = root.name or str(root)
        if branch_name not in graph.branches_scanned:
            graph.branches_scanned.append(branch_name)
        for p in sorted(root.rglob("*.xlsx")):
            if p.name.startswith("~$") or p.name == "id_mgr.xlsx":
                continue
            graph.scanned_files += 1
            try:
                wb = openpyxl.load_workbook(p, data_only=True, read_only=True)
            except Exception as e:
                logger.debug("[ColumnLineage] 跳过不可读表 %s: %s", p.name, e)
                continue
            try:
                stem = p.stem
                # 去 _capped 后缀统一 table key（pet vs pet_capped 同 table）
                table_key = stem[:-len("_capped")] if stem.endswith("_capped") else stem
                for sn in wb.sheetnames:
                    if sn.upper() == "CONFIG" or "说明" in sn:
                        continue
                    ws = wb[sn]
                    cols = _scan_sheet_columns(ws)
                    if not cols:
                        continue
                    table_sheets = graph.tables.setdefault(table_key, {})
                    sheet_cols = table_sheets.setdefault(sn, {})
                    for col_name, meta in cols.items():
                        entry = sheet_cols.get(col_name)
                        if entry is None:
                            entry = ColumnLineageEntry(
                                column=col_name,
                                col_type=meta.get("type", ""),
                                not_empty=meta.get("not_empty", False),
                                present_in_branches=[],
                            )
                            sheet_cols[col_name] = entry
                        if branch_name not in entry.present_in_branches:
                            entry.present_in_branches.append(branch_name)
            finally:
                wb.close()
    return graph

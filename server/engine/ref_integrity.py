"""引用完整性校验：检测合并后的外键悬空，并消费 ID 重映射表同步外键值。

解决场景：
  - 分支A 删除 id=5 行，分支B 新增内容引用 id=5 → 合并后外键悬空（可检测）
  - 分支B 的 id=99 被重映射为 100，同表/跨表外键引用旧 99 → 需按 (B,99) 同步更新，
    否则外键仍指向 99。若 99 恰好被分支A 占用 → 静默错配（最坏情况）。

外键识别：
  - 表内：第一列为主键，其余 strategy=base_priority 的 ID 列视为外键候选
  - 跨表：列名等于另一 sheet 主键列名 → 校验该 sheet 的主键集合
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from .id_resolver import (
    PK_COL, _cell_val, _cell_versions, _pk_str, _row_cells, _row_type,
)


def _load_fk_columns(headers: List[str], table_stem: str, sheet_name: str) -> List[int]:
    """从 merge_strategies 推断外键列索引（base_priority 且非主键列）。"""
    from .merge_engine import _get_column_strategy
    fks: List[int] = []
    for ci, h in enumerate(headers):
        if ci == PK_COL:
            continue
        strategy, _ = _get_column_strategy(table_stem, sheet_name, str(h) if h else "")
        if strategy == "base_priority":
            fks.append(ci)
    return fks


def validate_sheet_references(
    rows: List[dict],
    headers: List[str],
    table_stem: str,
    sheet_name: str,
    id_mapping: List[dict],
    cross_sheet_pks: Dict[str, set] = None,
    extra_local_pks: set = None,
) -> dict:
    """校验单个 sheet 的外键引用完整性，并同步重映射后的外键值。

    参数:
      rows: 合并后的行（dict 列表，主键已重映射）
      headers: 表头
      table_stem/sheet_name: 用于查 merge_strategies 外键列
      id_mapping: ID 重映射表（来自 id_resolver，带分支标记 (file, old_pk) -> new_pk）
      cross_sheet_pks: {列名: 主键集合}，跨表外键校验用。外键列名命中则校验该集合

    返回:
      {
        dangling: [{ri, ci, col_header, value, reason}],
        remapped_refs: N,   # 同步更新了的外键引用数
        checked: N,
      }
    """
    cross_sheet_pks = cross_sheet_pks or {}
    fk_cols = _load_fk_columns(headers, table_stem, sheet_name)
    # 无外键列时无需扫描全表：local_pks/cross_sheet_pks/remap_lookup 均不会被用到，直接返回空结果
    if not fk_cols:
        return {"dangling": [], "remapped_refs": 0, "checked": 0}

    # 本表主键集合（合并后，已含重映射后的新主键）
    # apply 前端只传差异行时，extra_local_pks 并入落盘表全量主键，避免漏检/误报
    local_pks: set = set()
    for row in rows:
        cells = _row_cells(row)
        if cells:
            local_pks.add(_pk_str(_cell_val(cells[PK_COL])))
    if extra_local_pks:
        local_pks |= extra_local_pks

    # 重映射查表：(file, old_pk) -> new_pk
    remap_lookup: Dict[Tuple[str, str], str] = {
        (m["file"], m["old_pk"]): m["new_pk"] for m in id_mapping
    }
    # old_pk -> [new_pk...] 的无分支回退表（用于显示值更新，无法确定分支时）
    # M5: 改为多值列表，跨分支同 old_pk 重映射到不同 new_pk 时记录歧义，
    # 兜底取值时若多值则不盲取首个，避免外键显示值指向错误行。
    old_to_new: Dict[str, List[str]] = {}
    for m in id_mapping:
        old_to_new.setdefault(m["old_pk"], []).append(m["new_pk"])

    dangling: List[dict] = []
    remapped_refs = 0
    checked = 0

    for ri, row in enumerate(rows):
        if _row_type(row) == "deleted":
            continue
        cells = _row_cells(row)
        for ci in fk_cols:
            if ci >= len(cells):
                continue
            cell = cells[ci]
            if not isinstance(cell, dict):
                continue
            col_header = headers[ci] if ci < len(headers) else ""
            versions = dict(_cell_versions(cell))
            changed = False
            for fn, v in list(versions.items()):
                v_str = _pk_str(v)
                if not v_str:
                    continue
                checked += 1
                # 若该 (fn, v) 命中重映射表 → 同步更新外键值
                new_pk = remap_lookup.get((fn, v_str))
                if new_pk:
                    versions[fn] = new_pk
                    v_str = new_pk
                    remapped_refs += 1
                    changed = True
                # 校验：本表主键 or 跨表主键集合
                valid = v_str in local_pks
                if not valid and col_header in cross_sheet_pks:
                    valid = v_str in cross_sheet_pks[col_header]
                if not valid:
                    dangling.append({
                        "ri": ri, "ci": ci,
                        "col_header": col_header,
                        "value": v_str,
                        "reason": "外键目标不存在（悬空引用）",
                    })
            if changed:
                cell["versions"] = versions
                # 更新显示值：若旧显示值被重映射，同步取新值
                # M5: old_to_new 为多值列表，歧义时不盲取首个（跨分支同 old_pk
                # 重映射到不同 new_pk 时取首个会指向错误行），仅在唯一映射时更新。
                cur_val = _pk_str(_cell_val(cell))
                if cur_val and cur_val in old_to_new:
                    new_vals = old_to_new[cur_val]
                    if len(new_vals) == 1:
                        cell["value"] = new_vals[0]
                    # 多值歧义时保留原显示值，避免误导（数据值已由 remap_lookup 精确更新）

    return {
        "dangling": dangling,
        "remapped_refs": remapped_refs,
        "checked": checked,
    }


def collect_cross_sheet_pks(sheets_data: List[dict]) -> Dict[str, set]:
    """聚合所有 sheet 的主键集合，按主键列名索引（用于跨表外键校验）。

    sheets_data: [{"name", "headers", "rows"}, ...]
    返回: {主键列名: {所有主键值}}
    """
    pk_sets: Dict[str, set] = {}
    for sd in sheets_data:
        headers = sd.get("headers", []) or []
        rows = sd.get("rows", []) or []
        if not headers or len(headers) <= PK_COL:
            continue
        pk_col_name = str(headers[PK_COL]) if headers[PK_COL] else ""
        if not pk_col_name:
            continue
        s = pk_sets.setdefault(pk_col_name, set())
        for row in rows:
            cells = _row_cells(row)
            if cells:
                pk = _pk_str(_cell_val(cells[PK_COL]))
                if pk:
                    s.add(pk)
    return pk_sets

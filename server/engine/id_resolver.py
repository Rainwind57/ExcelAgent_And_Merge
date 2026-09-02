"""ID 冲突重映射：多分支新增行主键撞车时，先到先得 + 后到者分配未占用新 ID。

解决场景：
  分支A、分支B 各自基于基准新增主键=99 的行 → 合并时主键冲突。
  先合并的分支保留 99，后到者分配新主键（max+1，跳过已占用）。

重映射表带分支标记 (file, old_pk) -> new_pk，避免同名主键跨分支误读：
  分支B 内若有外键引用旧 99，需按 (B, 99) 查表更新，不会误命中分支A 的 99。

同时修正 compare 阶段的错误合并：多分支同主键但内容不同的 inserted 行
会被拆成多条独立记录，再做重映射。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

PK_COL = 0


def _pk_str(v: Any) -> str:
    if v is None:
        return ""
    return str(v).strip()


def _cell_val(cell: Any) -> Any:
    if isinstance(cell, dict):
        return cell.get("value")
    return getattr(cell, "value", None)


def _cell_versions(cell: Any) -> Dict[str, Any]:
    if isinstance(cell, dict):
        return cell.get("versions", {}) or {}
    return getattr(cell, "versions", {}) or {}


def _row_cells(row: Any) -> List[Any]:
    if isinstance(row, dict):
        return row.get("cells", []) or []
    return getattr(row, "cells", []) or []


def _row_type(row: Any) -> str:
    if isinstance(row, dict):
        return row.get("row_type", "")
    return getattr(row, "row_type", "")


def _row_to_dict(row: Any) -> dict:
    if isinstance(row, dict):
        rt = row.get("row_type", "")
        # R25-fix: 之前这里只挑 key/cells/row_type/id_remapped/original_pk 几个
        # 字段重建行 dict，调用方附加的其它字段（尤其是 presence——前端 split
        # 视图靠它判断"某分支到底有没有这一行"，是区分"单侧删除"和"单侧改动
        # 为空值"的唯一线索）会被静默丢弃。且本函数对**所有**行（不只 inserted）
        # 都会跑一遍 _row_to_dict，等于 presence 在实践中永远传不到前端。
        # 改成整行浅拷贝：非 inserted 行 cells 仍共享原引用（保留 M7-2 的免拷贝
        # 性能优化，dict() 浅拷贝不会连带拷贝嵌套的 cells 列表），inserted 行才
        # 深拷贝 cells（split/重映射要改写单元格，不能共享引用）。
        d = dict(row)
        d["row_type"] = rt
        if rt == "inserted":
            d["cells"] = [dict(c) if isinstance(c, dict) else c for c in row.get("cells", []) or []]
        return d
    cells = []
    for c in getattr(row, "cells", []) or []:
        if hasattr(c, "model_dump"):
            cells.append(c.model_dump())
        elif isinstance(c, dict):
            cells.append(dict(c))
        else:
            cells.append(c)
    d = {"key": getattr(row, "key", ""), "cells": cells, "row_type": getattr(row, "row_type", "")}
    # R25-fix: 同上，非 dict（pydantic model 等）行也尽量带出常见附加字段
    for _extra in ("presence", "source_file", "source_version"):
        if hasattr(row, _extra):
            d[_extra] = getattr(row, _extra)
    if getattr(row, "id_remapped", False):
        d["id_remapped"] = True
        d["original_pk"] = getattr(row, "original_pk", "")
    return d


def _inserted_source_files(row: dict, base_name: str, all_files: List[str]) -> List[str]:
    """返回该 inserted 行主键列中有数据的衍生文件列表（按 all_files 顺序）。"""
    cells = row.get("cells", []) or []
    if not cells:
        return []
    versions = _cell_versions(cells[PK_COL])
    return [fn for fn in all_files if fn != base_name and versions.get(fn) is not None]


def _row_signature_from_file(row: dict, fname: str) -> Tuple:
    """取某文件视角下的整行值签名（用于判断多分支内容是否真不同）。"""
    cells = row.get("cells", []) or []
    return tuple(_pk_str(_cell_versions(c).get(fname)) for c in cells)


def resolve_id_conflicts(
    rows: List[Any],
    headers: List[str],
    base_name: str,
    all_files: List[str],
    mode: str = "split",
) -> dict:
    """检测并解决多分支新增行（inserted）的主键冲突。

    当多分支各自新增同主键且内容不同时，提供两种处理模式：

      mode="split"（默认）：视为两条独立新行，版本 id 重叠 → 按版本顺序
        先到先得，后到者主键自增到未占用的新 id。适用于"版本不一致导致 id 重叠"。
      mode="conflict"：视为同一行发生冲突 → 不拆分不重映射，保留原合并行
        并标记 _pk_conflict，交人工裁决。适用于"确实是同一行，内容冲突"。

    步骤：
      1. 多分支同主键 inserted 行：
         - split 模式 → 拆成多条独立行
         - conflict 模式 → 保留一条，标记主键冲突待裁决
      2. 收集已占用主键集合（matched + inserted 主键）
      3. split 模式下：多分支同主键 → 先到先得，后到者分配新主键
      4. 产出带分支标记的映射表，并就地应用到行

    注：单条新增 ID 撞基准已有记录的校验不在此处，归表格处理（TableAgent._do_append
    写入前校验主键唯一性并提示）。本函数只管多版本合并时的跨分支 ID 冲突。

    返回:
      {
        resolved_rows: [...],                          # 主键已更新的行列表（dict）
        id_mapping: [{file, old_pk, new_pk, reason}],  # 带分支标记的重映射记录
        conflicts: [{file, old_pk, new_pk, ri, reason}],
        pk_conflicts: [{files, pk, reason}],           # 待人工裁决的主键冲突（conflict 模式）
        stats: {rows_split, conflicts_resolved, ids_remapped, pk_conflicts}
      }

    注：被重映射的行会在 dict 上打 id_remapped=True 与 original_pk=<原编号>，
    供前端回写 state 与导出时在主键单元格写批注（原先编号→merge冲突修改为）。
    """
    other_files = [f for f in all_files if f != base_name]

    # ── 步骤1: 处理多分支同主键的 inserted 行 ──
    split_rows: List[dict] = []
    rows_split = 0
    pk_conflicts: List[dict] = []  # 待人工裁决的主键冲突（conflict 模式）
    for row in rows:
        d = _row_to_dict(row)
        if d["row_type"] != "inserted":
            split_rows.append(d)
            continue
        src_files = _inserted_source_files(d, base_name, all_files)
        if len(src_files) <= 1:
            split_rows.append(d)
            continue
        # 多分支：检查内容是否真不同
        sigs = {fn: _row_signature_from_file(d, fn) for fn in src_files}
        if len(set(sigs.values())) <= 1:
            # 内容相同 → 保留单条
            split_rows.append(d)
            continue
        # 多分支同主键 + 内容不同
        pk_val = _pk_str(_cell_val(d["cells"][PK_COL])) if d.get("cells") else ""
        if mode == "conflict":
            # 方案1：视为同一行发生冲突，保留原合并行交人工裁决
            d["_pk_conflict"] = True
            d["_conflict_files"] = src_files
            pk_conflicts.append({
                "files": src_files,
                "pk": pk_val,
                "reason": "多分支新增同 ID 且内容不同，需人工裁决（视为同一行冲突）",
            })
            split_rows.append(d)
            continue
        # 方案2（split）：拆分为独立新行，后续步骤3 重映射
        for fn in src_files:
            new_d: dict = {"key": "", "cells": [], "row_type": "inserted"}
            for c in d["cells"]:
                nc = dict(c) if isinstance(c, dict) else c
                if isinstance(nc, dict):
                    v = _cell_versions(nc).get(fn)
                    nc["value"] = v
                    nc["versions"] = {fn: v}
                new_d["cells"].append(nc)
            if new_d["cells"]:
                new_d["key"] = _pk_str(_cell_val(new_d["cells"][PK_COL]))
            split_rows.append(new_d)
        rows_split += 1

    # ── 步骤2: 收集已占用主键 + 记录 inserted 行来源 ──
    used_pks: set = set()
    inserted_info: List[Tuple[int, str, str]] = []  # (ri, source_file, pk)
    for ri, d in enumerate(split_rows):
        cells = d.get("cells", []) or []
        if not cells:
            continue
        pk_val = _pk_str(_cell_val(cells[PK_COL]))
        if d["row_type"] == "inserted":
            src = _inserted_source_files(d, base_name, all_files)
            inserted_info.append((ri, src[0] if src else "", pk_val))
        if pk_val:
            used_pks.add(pk_val)

    # ── 步骤3: 冲突检测 + 重映射 ──
    id_mapping: List[dict] = []
    conflicts: List[dict] = []

    max_num = 0
    for pk in used_pks:
        try:
            max_num = max(max_num, int(pk))
        except (ValueError, TypeError):
            pass

    def _next_pk(start: int) -> str:
        # M19: 加最大迭代上限，防止构造连续主键到极大值使循环不终止。
        _MAX_ITER = 100000
        n = start
        steps = 0
        while str(n) in used_pks:
            n += 1
            steps += 1
            if steps > _MAX_ITER:
                raise RuntimeError(f"主键自增超过 {_MAX_ITER} 次仍冲突，疑似异常输入")
        s = str(n)
        used_pks.add(s)
        return s

    file_priority = {fn: i for i, fn in enumerate(other_files)}

    # 判定每条 inserted 行是否需重映射及原因
    needs_remap: Dict[int, Tuple[str, str, str]] = {}  # ri -> (file, old_pk, reason)

    # 多分支 inserted 同主键 → 先到先得，后到者重映射
    pk_to_sources: Dict[str, List[Tuple[int, str]]] = {}
    for ri, fn, pk in inserted_info:
        pk_to_sources.setdefault(pk, []).append((ri, fn))
    for pk, sources in pk_to_sources.items():
        if len(sources) <= 1:
            continue
        sources_sorted = sorted(sources, key=lambda x: file_priority.get(x[1], 999))
        for ri, fn in sources_sorted[1:]:
            needs_remap[ri] = (fn, pk, "多分支新增同 ID 冲突")

    # 分配新主键
    remap_by_ri: Dict[int, str] = {}
    for ri, (fn, pk, reason) in needs_remap.items():
        new_pk = _next_pk(max_num + 1)
        try:
            max_num = max(max_num, int(new_pk))
        except (ValueError, TypeError):
            pass
        remap_by_ri[ri] = new_pk
        id_mapping.append({"file": fn, "old_pk": pk, "new_pk": new_pk, "reason": reason})
        conflicts.append({"file": fn, "old_pk": pk, "new_pk": new_pk, "ri": ri, "reason": reason})

    # ── 步骤4: 应用重映射到行主键 ──
    for ri, fn, pk in inserted_info:
        new_pk = remap_by_ri.get(ri)
        if not new_pk:
            continue
        d = split_rows[ri]
        cells = d.get("cells", []) or []
        if cells and isinstance(cells[PK_COL], dict):
            # 保留数值类型：重映射后的主键若为纯数字则写 int，避免导出成文本导致编表不认
            try:
                new_pk_typed = int(new_pk)
            except (ValueError, TypeError):
                new_pk_typed = new_pk
            cells[PK_COL]["value"] = new_pk_typed
            vers = dict(cells[PK_COL].get("versions", {}))
            vers[fn] = new_pk_typed
            cells[PK_COL]["versions"] = vers
            d["key"] = new_pk
            d["id_remapped"] = True
            d["original_pk"] = pk

    return {
        "resolved_rows": split_rows,
        "id_mapping": id_mapping,
        "conflicts": conflicts,
        "pk_conflicts": pk_conflicts,
        "stats": {
            "rows_split": rows_split,
            "conflicts_resolved": len(conflicts),
            "ids_remapped": len(id_mapping),
            "pk_conflicts": len(pk_conflicts),
        },
    }

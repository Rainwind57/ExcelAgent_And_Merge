"""结构增删标注（表/sheet 级，标注来源 source/target）。

三方结构 diff：以 base（fork 点）为参照，判定每个表/sheet 是 common / source_added /
target_added / source_deleted / target_deleted / both_deleted / both_added，并标注 origin。

- 分支模式（src_scope_only=False）：完整三方结构 diff，in_base & !in_src & in_tgt 视为
  source_deleted（分支删除了继承的表）。
- 子目录模式（src_scope_only=True）：子目录只含其开发的表，"in_base & !in_src & in_tgt"
  （子目录未含的目标既有表）不算删除（out of scope），跳过 source_deleted / both_deleted。
"""
from typing import Dict, List, Optional, Set, Tuple

_ORIGIN = {
    "source_added": "source", "target_added": "target",
    "source_deleted": "source", "target_deleted": "target",
    "both_deleted": "both", "both_added": "both", "common": "",
}


def table_status(in_base: bool, in_src: bool, in_tgt: bool, src_scope_only: bool) -> Optional[str]:
    """表级结构状态。返回 status 或 None（不报）。"""
    if in_base and in_src and in_tgt:
        return "common"
    if in_src and in_tgt and not in_base:
        return "both_added"
    if in_src and not in_tgt:
        return "target_deleted" if in_base else "source_added"
    if not in_src and in_tgt:
        if not in_base:
            return "target_added"
        return None if src_scope_only else "source_deleted"
    if in_base and not in_src and not in_tgt:
        return None if src_scope_only else "both_deleted"
    return None


def sheet_status(in_base: bool, in_src: bool, in_tgt: bool) -> Optional[str]:
    """sheet 级结构状态（sheet 在其所属表内，presence 即真实增删，不做 scope 跳过）。"""
    if in_base and in_src and in_tgt:
        return "common"
    if in_src and in_tgt and not in_base:
        return "both_added"
    if in_src and not in_tgt:
        return "target_deleted" if in_base else "source_added"
    if not in_src and in_tgt:
        return "source_deleted" if in_base else "target_added"
    if in_base and not in_src and not in_tgt:
        return "both_deleted"
    return None


def compute_structural_changes(
    base: Dict[str, Set[str]],
    src: Dict[str, Set[str]],
    tgt: Dict[str, Set[str]],
    src_scope_only: bool = False,
) -> List[dict]:
    """计算表级 + sheet 级结构增删条目。

    base/src/tgt: {group_prefix: set(sheet_names)} 三方各自的表→sheet 集合。
    返回 [{kind, table, sheet, status, origin, detail}]（common 不返回；仅返回增删/结构变更）。
    """
    out: List[dict] = []
    all_groups = set(base) | set(src) | set(tgt)
    for g in sorted(all_groups):
        b_sh = base.get(g, set())
        s_sh = src.get(g, set())
        t_sh = tgt.get(g, set())
        tstat = table_status(bool(b_sh), bool(s_sh), bool(t_sh), src_scope_only)
        if tstat and tstat != "common":
            out.append({
                "kind": "table", "table": g, "sheet": "",
                "status": tstat, "origin": _ORIGIN.get(tstat, ""),
                "detail": _table_detail(tstat, g),
            })
        # sheet 级：仅对存在单元格比对的表（common/both_added，即 src 与 tgt 都有）做 sheet 结构 diff
        if tstat in ("common", "both_added"):
            all_sheets = b_sh | s_sh | t_sh
            for sh in sorted(all_sheets):
                sstat = sheet_status(sh in b_sh, sh in s_sh, sh in t_sh)
                if sstat and sstat != "common":
                    out.append({
                        "kind": "sheet", "table": g, "sheet": sh,
                        "status": sstat, "origin": _ORIGIN.get(sstat, ""),
                        "detail": _sheet_detail(sstat, g, sh),
                    })
    return out


def compute_column_changes(
    src_columns: Dict[str, Dict[str, Set[str]]],
    tgt_columns: Dict[str, Dict[str, Set[str]]],
) -> List[dict]:
    """列级结构 diff（方法 E2）：trunk 加列/改列检测。

    src_columns/tgt_columns: {table -> {sheet -> set(column_names)}}。
    返回 [{kind, table, sheet, status, detail}]，kind="column_added"/"column_changed"。
    - column_added：src（trunk）有 tgt 无的列（trunk 加列后 capped/dev 缺此列）
    - column_changed：列名相同但 src/tgt 两侧均存在（暂只标 added，changed 需 type 比对留后续）
    仅返回 added（changed 需 type 数据，本函数签名仅 set，留 follow-up）。
    """
    out: List[dict] = []
    all_tables = set(src_columns) | set(tgt_columns)
    for t in sorted(all_tables):
        s_sheets = src_columns.get(t, {})
        t_sheets = tgt_columns.get(t, {})
        all_sheets = set(s_sheets) | set(t_sheets)
        for sh in sorted(all_sheets):
            s_cols = s_sheets.get(sh, set())
            t_cols = t_sheets.get(sh, set())
            # src 有 tgt 无 → column_added（trunk 加列后 capped 缺）
            added = s_cols - t_cols
            for c in sorted(added):
                out.append({
                    "kind": "column_added", "table": t, "sheet": sh,
                    "column": c, "status": "source_added",
                    "detail": f"源 {t}.{sh} 新增列 {c}（目标无）",
                })
    return out
    m = {
        "source_added": f"源新增表格 {g}（目标无）",
        "target_added": f"目标新增表格 {g}（源无）",
        "source_deleted": f"源删除表格 {g}（目标保留）",
        "target_deleted": f"目标删除表格 {g}（源保留）",
        "both_deleted": f"双方均已删除表格 {g}",
        "both_added": f"源与目标各自新增表格 {g}（base 无）",
    }
    return m.get(status, status)


def _sheet_detail(status: str, g: str, sh: str) -> str:
    m = {
        "source_added": f"源新增 sheet {g}/{sh}",
        "target_added": f"目标新增 sheet {g}/{sh}",
        "source_deleted": f"源删除 sheet {g}/{sh}",
        "target_deleted": f"目标删除 sheet {g}/{sh}",
        "both_deleted": f"双方均已删除 sheet {g}/{sh}",
        "both_added": f"源与目标各自新增 sheet {g}/{sh}",
    }
    return m.get(status, status)


# ── 只读展示构建：让"仅一侧存在"的表/sheet 在前端可点击查看内容 ──

def build_display_sheet(file_path, sheet_name: str, src_rows: list, status: str, origin: str):
    """由单侧文件的原始行构建只读 SheetDiff（无冲突/变更，versions 仅含该文件）。

    file_path: 展示内容来源文件路径；sheet_name: sheet 名；
    src_rows: read_excel 得到的二维数组（含表头行）。
    """
    from openpyxl.utils import get_column_letter
    from engine.models import CellData, RowData, SheetDiff

    fname = file_path.name if hasattr(file_path, "name") else str(file_path).replace("\\", "/").rsplit("/", 1)[-1]
    headers = [("" if h is None else str(h)) for h in (src_rows[0] if src_rows else [])]
    body = []
    for r in src_rows[1:]:
        cells = [
            CellData(col=i, col_letter=get_column_letter(i + 1), value=v, versions={fname: v})
            for i, v in enumerate(r)
        ]
        body.append(RowData(key="" if not r or r[0] is None else str(r[0]), cells=cells))
    return SheetDiff(
        name=sheet_name, headers=headers, rows=body,
        stats={"total": len(body), "conflicts": 0, "changed": 0, "inserted": 0, "deleted": 0},
        structural_status=status, origin=origin,
    )


def build_display_group(group: str, file_path, status: str, origin: str):
    """为"仅一侧存在"的表构建只读展示 FileGroup：单侧文件全量内容，无三方比对。

    供前端直接走既有表格渲染（sheet 可选、表体可见）；apply 时由前端跳过（files 仅 1 个，
    无比对数据）。status/origin 透传到 FileGroup/SheetDiff 供前端着色徽标。
    """
    from engine.models import FileGroup
    from engine.parser import read_excel

    data = read_excel(str(file_path))
    fname = file_path.name if hasattr(file_path, "name") else str(file_path).replace("\\", "/").rsplit("/", 1)[-1]
    sheets = {
        sname: build_display_sheet(file_path, sname, rows, status, origin)
        for sname, rows in data.items()
    }
    return FileGroup(
        group_name=group, base_file=fname, files=[fname], sheets=sheets,
        structural_status=status, origin=origin,
    )

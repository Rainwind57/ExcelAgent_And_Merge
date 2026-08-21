"""SET 改主键的跨表级联影响推理与执行（P0 数据风险防护）。

设计动机：
    ca-overview.md §2.3.2 P0：改 item.id=10500，所有引用该 id 的 reward/drop/mail 行
    不会联动。ref_integrity.py 只做事后存在性校验（提交后才报警），_run_set 改 PK
    仅做 id_scope 越界检查后直接写盘，无影响面推理。

    本模块在 _run_set 改主键**写前**插入级联预览门：基于 table_relations.json 关系图
    + 语义列名匹配，推断受影响表/行集合，生成级联更新补丁。高置信（精确关系图命中）
    自动带改入事务，低置信（语义匹配）needs_confirm 交用户确认。

    质变：从"事后救火"（ref_integrity 报警）到"事前预防"（写前生成级联补丁）。
    对应 ca-overview.md §5 核心目标转变。

复用 xlsx_tool 工具：
    _find_related_files（同目录 + 索引前缀关联文件）
    _semantic_col_names（列名语义变体，跨 sheet 匹配含义相同列）
    _match_header（表头三级匹配：精确/startswith/换行截取）

边界：
    纯代码、零 LLM（快路径预览）。LLM 诊断在 verify-repair Level 2。
    仅处理"被引用主键"改动（to_column in table_relations 或首列主键）。
    改外键列（引用方）不触发级联（只影响自身行）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _load_table_relations() -> list[dict]:
    """加载 table_relations.json 关系图。失败返回空列表。"""
    try:
        import json
        p = Path(__file__).resolve().parent.parent / "table_relations.json"
        if not p.exists():
            return []
        data = json.loads(p.read_text(encoding="utf-8"))
        rels = data.get("relations", []) if isinstance(data, dict) else []
        return [r for r in rels if isinstance(r, dict)]
    except Exception:
        logger.debug("table_relations.json 加载失败", exc_info=True)
        return []


def _stem_matches_relation_to(stem: str, col_name: str, rel: dict) -> bool:
    """判断关系条的 to 端是否指向当前表+列（被引用方）。

    to_path 形如 "pet/pet.xlsx"，stem="pet" → endswith(stem + ".xlsx") 或含 stem。
    to_column 与 col_name 语义相等（去 _/空格/大小写）。
    """
    to_path = str(rel.get("to_path", ""))
    to_col = str(rel.get("to_column", "")).strip()
    if not to_path or not to_col:
        return False
    # stem 匹配（to_path 含 stem）
    if stem not in to_path:
        return False
    # 列名语义匹配（去 _/空格/大小写）
    def _norm(s: str) -> str:
        return s.replace("_", "").replace(" ", "").lower()
    return _norm(to_col) == _norm(col_name)


def _find_fk_columns_in_relation(stem: str, col_name: str) -> list[tuple[str, str, str]]:
    """从 table_relations.json 找引用当前表+列的所有外键列。

    返回 [(from_path, from_sheet, from_column), ...]。
    精确关系图命中=高置信；未命中回退语义匹配（在 _collect_affected_rows 里做）。
    """
    rels = _load_table_relations()
    out = []
    seen = set()
    for rel in rels:
        if _stem_matches_relation_to(stem, col_name, rel):
            key = (rel.get("from_path", ""), rel.get("from_sheet", ""), rel.get("from_column", ""))
            if key not in seen and key[0] and key[1] and key[2]:
                out.append(key)
                seen.add(key)
    return out


def _collect_affected_rows(cli, path: Path, sheet: str, row: int,
                           col_name: str, old_val: Any, new_val: Any,
                           stem: str) -> list[dict]:
    """收集级联更新目标（dry-run），不执行任何写。

    两策略：
    1. 精确关系图：_find_fk_columns_in_relation 给的 from_* 列，扫值==old_val 的行
    2. 语义匹配回退：_find_related_files + _semantic_col_names + _match_header
       （关系图未覆盖时，复用 _collect_cascade_deletes 同款语义匹配）

    返回 affected [{path, sheet, row, field, old_value, suggested_value}]
    """
    from ..cli.xlsx_tool import (
        _find_related_files, _semantic_col_names, _match_header,
    )
    affected: list[dict] = []
    seen_rows: set[tuple] = set()  # (path, sheet, row, col) 去重

    # 1. 精确关系图命中
    fk_cols = _find_fk_columns_in_relation(stem, col_name)
    for from_path_str, from_sheet, from_col in fk_cols:
        try:
            from_path = _resolve_workspace_path(from_path_str)
            if from_path is None:
                continue
            if not _sheet_exists(cli, from_path, from_sheet):
                continue
            other_header = cli.read_header(from_path, from_sheet)
            matched_ci = _match_header(from_col, other_header, stem, from_sheet)
            if matched_ci is None:
                continue
            for r in _scan_value_rows(cli, from_path, from_sheet, matched_ci, old_val):
                key = (str(from_path), from_sheet, r, matched_ci)
                if key in seen_rows:
                    continue
                seen_rows.add(key)
                affected.append({
                    "path": str(from_path), "sheet": from_sheet, "row": r,
                    "field": from_col, "old_value": old_val,
                    "suggested_value": new_val,
                })
        except Exception:
            logger.debug("关系图级联扫描失败 %s/%s/%s", from_path_str, from_sheet, from_col, exc_info=True)

    # 2. 语义匹配回退（关系图未覆盖的关联文件）
    #    仅当精确关系图零命中时走全量语义扫描（避免与精确命中重复）
    if not affected:
        variants = _semantic_col_names(col_name)
        # 同文件其他 sheet
        try:
            all_sheets = cli.get_sheets(path)
            for other_sheet in all_sheets:
                if other_sheet == sheet:
                    continue
                _scan_sheet_semantic(cli, path, other_sheet, variants, old_val, new_val,
                                    col_name, stem, affected, seen_rows)
        except Exception:
            pass
        # 关联文件
        for rel_path in _find_related_files(path, stem):
            try:
                for rel_sheet in cli.get_sheets(rel_path):
                    _scan_sheet_semantic(cli, rel_path, rel_sheet, variants, old_val, new_val,
                                        col_name, stem, affected, seen_rows)
            except Exception:
                pass
    return affected


def _resolve_workspace_path(rel_path_str: str) -> Optional[Path]:
    """把 table_relations.json 的相对路径（如 'pet/pet.xlsx'）解析为绝对路径。"""
    try:
        from ..cli.xlsx_tool import WORKSPACE
        p = WORKSPACE / rel_path_str
        return p
    except Exception:
        return None


def _sheet_exists(cli, path: Path, sheet: str) -> bool:
    try:
        return sheet in cli.get_sheets(path)
    except Exception:
        return False


def _scan_value_rows(cli, path: Path, sheet: str, col_idx: int, target_val: Any) -> list[int]:
    """扫某列，返回值==target_val 的行号列表（1-based）。"""
    rows = []
    try:
        ws = cli._load(path)[sheet]
        last_row = cli._last_data_row(ws, cli.data_start_row)
        target_str = str(target_val).strip()
        for r in range(cli.data_start_row, last_row + 1):
            cell_val = ws.cell(r, col_idx).value
            if cell_val is not None and str(cell_val).strip() == target_str:
                rows.append(r)
    except Exception:
        pass
    return rows


def _scan_sheet_semantic(cli, path: Path, sheet: str, variants: set[str],
                         old_val: Any, new_val: Any, col_name: str,
                         stem: str, affected: list[dict],
                         seen_rows: set[tuple]) -> None:
    """语义匹配扫一个 sheet：找引用列，扫值==old_val 的行。"""
    try:
        other_header = cli.read_header(path, sheet)
        matched_ci = None
        for variant in variants:
            matched_ci = _match_header(variant, other_header, stem, sheet)
            if matched_ci is not None:
                break
        if matched_ci is None:
            return
        for r in _scan_value_rows(cli, path, sheet, matched_ci, old_val):
            key = (str(path), sheet, r, matched_ci)
            if key in seen_rows:
                continue
            seen_rows.add(key)
            affected.append({
                "path": str(path), "sheet": sheet, "row": r,
                "field": other_header[matched_ci - 1] if matched_ci <= len(other_header) else col_name,
                "old_value": old_val, "suggested_value": new_val,
            })
    except Exception:
        pass


def preview_cascade_set_pk(cli, path: Path, sheet: str, row: int,
                           col_name: str, old_val: Any, new_val: Any,
                           stem: str) -> dict:
    """预览改主键的级联影响（dry-run，不执行写）。

    返回 {affected: [{path, sheet, row, field, old_value, suggested_value}],
          count, items, confidence}
    confidence="high"（精确关系图命中）/"low"（语义匹配回退）/"none"（无影响）
    """
    try:
        affected = _collect_affected_rows(cli, path, sheet, row, col_name, old_val, new_val, stem)
        fk_cols = _find_fk_columns_in_relation(stem, col_name)
        confidence = "high" if (fk_cols and affected) else ("low" if affected else "none")
        items = [
            f"{a['sheet']} 行{a['row']}: {a['field']}={a['old_value']} → {a['suggested_value']}"
            for a in affected
        ]
        return {
            "affected": affected, "count": len(affected),
            "items": items, "confidence": confidence,
        }
    except Exception as e:
        logger.warning("级联预览失败，降级按无影响处理: %s", e, exc_info=True)
        return {"affected": [], "count": 0, "items": [f"(级联预览失败: {e})"], "confidence": "none"}


def apply_cascade_set_pk(cli, affected: list[dict]) -> list[dict]:
    """执行级联更新：逐行 write_cell 改外键值。

    返回 results [{ok, path, sheet, row, field, error}]
    """
    results = []
    for a in affected:
        try:
            p = Path(a["path"])
            r = cli.write_cell(p, a["sheet"], a["row"], _col_idx(cli, p, a["sheet"], a["field"]),
                               a["suggested_value"])
            ok = getattr(r, "ok", True)
            results.append({
                "ok": ok, "path": a["path"], "sheet": a["sheet"],
                "row": a["row"], "field": a["field"],
                "error": getattr(r, "error", "") if not ok else "",
            })
        except Exception as e:
            results.append({
                "ok": False, "path": a.get("path", ""), "sheet": a.get("sheet", ""),
                "row": a.get("row", 0), "field": a.get("field", ""),
                "error": str(e),
            })
    return results


def _col_idx(cli, path: Path, sheet: str, col_name: str) -> int:
    """按列名查 1-based 列索引。"""
    try:
        header = cli.read_header(path, sheet)
        from ..cli.xlsx_tool import _match_header
        ci = _match_header(col_name, header, "", sheet)
        return ci or 1
    except Exception:
        return 1

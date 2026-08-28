"""大数据表 apply 快路径：纯数据大表（无公式/批注/合并单元格）用 zip+XML 直改。

10w 行表 openpyxl 全量 load+save 约 13s（外加公式校验快照约 12s），而合并通常只改
十几格/几行。本模块对不满足快路径条件的文件返回 None，由调用方回退原 openpyxl 路径。

编辑语义与 _apply_edits_to_workbook 对齐：
  - matched 行：写入 value≠base 的已解决/变更单元格（col!=0）
  - inserted / missing_row：按主键升序插入整行（样式取自落点参考行同列）
  - deleted：删除整行
  - 存在插入/删除时整表行号重排（保证 row/cell 的 r 连续有效）
"""
from __future__ import annotations

import bisect
import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

# 优先 lxml（C 实现，25MB sheet XML 序列化 ~3.4s → ~1s），不可用时回退标准库 ElementTree
try:
    import lxml.etree as _xet
    _HAS_LXML = True
except ImportError:
    import xml.etree.ElementTree as _xet
    _HAS_LXML = False

logger = logging.getLogger(__name__)

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_XML_SPACE = "{http://www.w3.org/XML/1998/namespace}space"
_REL_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
# 小于该大小的小表继续走 openpyxl（快路径收益小、回归风险不值当）
_FAST_MIN_SIZE = 512 * 1024


_col_letter_cache: Dict[int, str] = {}
_letter_col_cache: Dict[str, int] = {}


def _col_letter(col0: int) -> str:
    """0 基列号 → 列字母（A=0）。缓存列字母（表格列数有限）。"""
    cached = _col_letter_cache.get(col0)
    if cached is not None:
        return cached
    n = col0 + 1
    s = ""
    while n:
        n, r = divmod(n - 1, 26)
        s = chr(65 + r) + s
    _col_letter_cache[col0] = s
    return s


def _col_from_letter(r: str) -> int:
    """cell 引用（如 B2）→ 0 基列号。缓存列字母→列号。"""
    m = re.match(r"([A-Z]+)", r or "")
    if not m:
        return 0
    key = m.group(1)
    cached = _letter_col_cache.get(key)
    if cached is not None:
        return cached
    n = 0
    for ch in key:
        n = n * 26 + (ord(ch) - 64)
    n -= 1
    _letter_col_cache[key] = n
    return n


def _cell_text_value(cell: _xet.Element, shared: List[str]) -> Any:
    """读单元格值：shared string / inlineStr / 数值 / 布尔。"""
    t = cell.get("t")
    v = cell.find(f"{{{_NS}}}v")
    if t == "s" and v is not None:
        try:
            idx = int(v.text or 0)
        except (TypeError, ValueError):
            return None
        return shared[idx] if idx < len(shared) else None
    if t == "inlineStr":
        is_el = cell.find(f"{{{_NS}}}is")
        if is_el is None:
            return None
        t_el = is_el.find(f"{{{_NS}}}t")
        return t_el.text if t_el is not None else ""
    return v.text if v is not None else None


def _row_pk(row: _xet.Element, shared: List[str]) -> Optional[str]:
    """取行主键（第 0 列值，与前端 row.key 的字符串形式一致）。"""
    for c in list(row):
        if _col_from_letter(c.get("r")) == 0:
            val = _cell_text_value(c, shared)
            return None if val is None else str(val).strip()
    return None


def _make_cell(r: str, value: Any, style: Optional[str] = None) -> _xet.Element:
    """构造单元格元素。文本用 inlineStr（不触碰 sharedStrings），数值/布尔按类型写。"""
    attrs: Dict[str, str] = {"r": r}
    if style:
        attrs["s"] = style
    el = _xet.Element(f"{{{_NS}}}c", attrs)
    if value is None:
        return el
    if isinstance(value, bool):
        el.set("t", "b")
        v = _xet.SubElement(el, f"{{{_NS}}}v")
        v.text = "1" if value else "0"
        return el
    if isinstance(value, (int, float)):
        v = _xet.SubElement(el, f"{{{_NS}}}v")
        v.text = str(value)
        return el
    el.set("t", "inlineStr")
    is_el = _xet.SubElement(el, f"{{{_NS}}}is")
    t_el = _xet.SubElement(is_el, f"{{{_NS}}}t")
    t_el.set(_XML_SPACE, "preserve")
    t_el.text = str(value)
    return el


def _replace_cell(row: _xet.Element, target: _xet.Element, col0: int, value: Any) -> None:
    """替换单元格内容（保留原样式 s 与行号 r 前缀）。"""
    old_r = target.get("r") or ""
    m = re.match(r"[A-Z]+", old_r)
    letter = m.group() if m else _col_letter(col0)
    row_num = re.sub(r"^\D+", "", old_r)
    newc = _make_cell(letter + row_num, value, style=target.get("s"))
    cells = list(row)
    pos = cells.index(target)
    row.remove(target)
    row.insert(pos, newc)


def _find_cell(row: _xet.Element, col0: int) -> Optional[_xet.Element]:
    for c in list(row):
        if _col_from_letter(c.get("r")) == col0:
            return c
    return None


def _insert_cell_sorted(row: _xet.Element, col0: int, value: Any, style: Optional[str] = None) -> None:
    """按列号顺序插入新单元格（未找到现成格时）。"""
    row_num = re.sub(r"^\D+", "", row.get("r") or "")
    newc = _make_cell(_col_letter(col0) + row_num, value, style=style)
    cells = list(row)
    pos = len(cells)
    for i, c in enumerate(cells):
        if _col_from_letter(c.get("r")) > col0:
            pos = i
            break
    row.insert(pos, newc)


def _cell_style(row: _xet.Element, col0: int) -> Optional[str]:
    c = _find_cell(row, col0)
    return c.get("s") if c is not None else None


def _build_inserted_row(ins: Dict[str, Any], ref_row: Optional[_xet.Element]) -> _xet.Element:
    row = _xet.Element(f"{{{_NS}}}row")
    for col0, value in sorted(ins.get("cells", {}).items()):
        style = _cell_style(ref_row, col0) if ref_row is not None else None
        row.append(_make_cell(_col_letter(col0), value, style=style))
    return row


def _renumber_rows(rows: List[_xet.Element], root: _xet.Element) -> None:
    """插入/删除后重排行号（row/cell 的 r 连续有效），并更新 dimension。"""
    for idx, row in enumerate(rows, start=1):
        row.set("r", str(idx))
        for cell in list(row):
            col0 = _col_from_letter(cell.get("r"))
            cell.set("r", _col_letter(col0) + str(idx))
    dim = root.find(f"{{{_NS}}}dimension")
    if dim is not None and rows:
        max_col = 1
        for row in rows:
            for cell in list(row):
                c = _col_from_letter(cell.get("r")) + 1
                if c > max_col:
                    max_col = c
        dim.set("ref", f"A1:{_col_letter(max_col - 1)}{len(rows)}")


def _pk_sort_key(pk):
    """R18: 主键自然排序键——数值优先按数值比较，字符串按数字段零填充做自然排序（与 routers.diff 一致）。"""
    s = "" if pk is None else str(pk).strip()
    try:
        return (0, float(s), "")
    except (ValueError, TypeError):
        pass
    padded = re.sub(r"(\d+)", lambda m: m.group(1).zfill(10), s)
    return (1, 0.0, padded)


def _edit_xml(xml: bytes, edits: Dict[str, Any], shared: List[str]) -> Optional[bytes]:
    """应用单 sheet 编辑。失败返回 None（调用方回退 openpyxl）。"""
    try:
        root = _xet.fromstring(xml)
    except _xet.ParseError:
        return None
    sheet_data = root.find(f"{{{_NS}}}sheetData")
    if sheet_data is None:
        return None
    rows: List[_xet.Element] = list(sheet_data)

    # 主键索引：跳过表头/字段类型行（PK 含 ":" 的元数据行，不参与定位）
    pk_rows: Dict[str, _xet.Element] = {}
    max_pk_key: Optional[Tuple] = None
    for row in rows:
        pk = _row_pk(row, shared)
        if pk is not None and ":" not in pk and pk not in pk_rows:
            pk_rows[pk] = row
            k = _pk_sort_key(pk)
            if max_pk_key is None or k > max_pk_key:
                max_pk_key = k

    # 1) matched 行单元格更新（改值/清空；与文件现值一致则跳过，避免类型/格式扰动）
    for pk, colvals in (edits.get("updates") or {}).items():
        row = pk_rows.get(str(pk))
        if row is None:
            return None  # 目标行缺失 → 语义不符，回退
        for col0, value in (colvals or {}).items():
            target = _find_cell(row, int(col0))
            if target is not None:
                cur = _cell_text_value(target, shared)
                if cur is not None and str(cur).strip() == str(value).strip():
                    continue
                _replace_cell(row, target, int(col0), value)
            else:
                _insert_cell_sorted(row, int(col0), value, style=None)

    # 2) 删除行
    if edits.get("deleted"):
        del_pks = {str(p) for p in edits["deleted"]}
        rows = [r for r in rows if _row_pk(r, shared) not in del_pks]

    # 3) 插入行（按主键升序落位；跳过目标已存在的主键，避免重复）
    needs_renumber = bool(edits.get("deleted"))
    inserted_positions: List[int] = []
    if edits.get("inserts"):
        inserts = sorted(edits["inserts"], key=lambda x: _pk_sort_key(x.get("pk")))
        inserts = [i for i in inserts if str(i.get("pk", "")) not in pk_rows]
        if inserts:
            new_rows: List[_xet.Element] = []
            # 全部插入主键都大于现存最大主键 → 纯追加，免排序/二分（10w 行 ~0.8s）
            append_only = max_pk_key is None or all(
                _pk_sort_key(str(i.get("pk", ""))) > max_pk_key for i in inserts)
            if append_only:
                for ins in inserts:
                    ref = rows[-1] if rows else None
                    nr = _build_inserted_row(ins, ref)
                    rows.append(nr)
                    new_rows.append(nr)
                    inserted_positions.append(len(rows) - 1)
            else:
                # 预构建按主键排序的数据行索引，落点用二分定位（O(log n)）
                sorted_data = sorted(pk_rows.items(), key=lambda kv: _pk_sort_key(kv[0]))
                sort_keys = [_pk_sort_key(pk) for pk, _ in sorted_data]
                data_rows = [row for _, row in sorted_data]
                row_pos = {id(r): i for i, r in enumerate(rows)}
                for ins in inserts:
                    pk = str(ins.get("pk", ""))
                    ins_key = _pk_sort_key(pk)
                    idx = bisect.bisect_right(sort_keys, ins_key)
                    if idx < len(data_rows):
                        # 首个自然序主键更大的现存行：样式参考（将被下推行），落点在其文档位置
                        ref = data_rows[idx]
                        pos = row_pos[id(ref)]
                    else:
                        # 追加末尾：样式参考上一行（openpyxl 同样取 actual_row-1）
                        ref = rows[-1] if rows else None
                        pos = len(rows)
                    # 前序插入的主键更小 → 均已落在 pos 之前，位置顺延
                    pos += len(new_rows)
                    nr = _build_inserted_row(ins, ref)
                    rows.insert(pos, nr)
                    new_rows.append(nr)
                    inserted_positions.append(pos)
            # 仅末尾追加（无删除、无中间插入）→ 现有行号不变，免整表重排（10w 行 ~1s）
            if any(p < len(rows) - 1 for p in inserted_positions):
                needs_renumber = True

    # 4) 结构变化 → 整表行号重排；否则只给新增行补行号
    if needs_renumber:
        _renumber_rows(rows, root)
        sheet_data[:] = rows
    else:
        for pos in inserted_positions:
            nr = rows[pos]
            row_num = str(pos + 1)
            nr.set("r", row_num)
            for cell in list(nr):
                col0 = _col_from_letter(cell.get("r"))
                cell.set("r", _col_letter(col0) + row_num)
        # 仅末尾追加：把新行元素挂到 sheetData 末尾（避免整表重挂 10w 行元素）
        for pos in inserted_positions:
            sheet_data.append(rows[pos])
        # dimension 只扩展末行号（追加不改列数），O(1) 免全表扫描
        if inserted_positions:
            dim = root.find(f"{{{_NS}}}dimension")
            if dim is not None:
                m = re.match(r"^[^:]*:?([A-Z]+)", dim.get("ref") or "")
                col = m.group(1) if m else "A"
                dim.set("ref", f"A1:{col}{len(rows)}")
    return _xet.tostring(root, encoding="utf-8", xml_declaration=True)


def _sheet_map(zf: zipfile.ZipFile) -> Dict[str, str]:
    """sheet 名 → worksheet XML 路径（xl/worksheets/sheetN.xml）。"""
    rid_to_target: Dict[str, str] = {}
    try:
        rels = _xet.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
        for rel in rels:
            rid = rel.get("Id")
            tgt = rel.get("Target")
            if rid and tgt:
                rid_to_target[rid] = tgt.lstrip("/")
    except (KeyError, _xet.ParseError):
        pass
    out: Dict[str, str] = {}
    try:
        wb = _xet.fromstring(zf.read("xl/workbook.xml"))
        for sheet in wb.iter(f"{{{_NS}}}sheet"):
            name = sheet.get("name")
            rid = sheet.get(f"{{{_REL_NS}}}id") or sheet.get("r:id")
            tgt = rid_to_target.get(rid or "", "")
            if not tgt:
                continue
            if not tgt.startswith("xl/"):
                tgt = "xl/" + tgt
            out[name] = tgt
    except (KeyError, _xet.ParseError):
        pass
    return out


def _load_shared_strings(zf: zipfile.ZipFile) -> List[str]:
    out: List[str] = []
    try:
        ss = _xet.fromstring(zf.read("xl/sharedStrings.xml"))
        for si in ss:
            out.append("".join(t.text or "" for t in si.iter(f"{{{_NS}}}t")))
    except (KeyError, _xet.ParseError):
        pass
    return out


def _eligible(src: Path, edits: Optional[Dict[str, Dict[str, Any]]] = None) -> Optional[str]:
    """返回 None 走快路径，否则返回跳过原因。

    4.7: 含公式/批注的表在「仅更新(无插入/删除)」时放行快路径——_replace_cell 用
    _make_cell 重建单元格,更新公式格时正确以字面值替换(去 <f>),非目标公式格原样
    保留;批注在独立 xml,纯更新不改行号→引用稳定。含合并单元格仍回退(非 anchor
    写入静默失效,需 anchor 感知逻辑,保守跳过)。结构变更(插入/删除)与公式/批注
    共存时回退:行号位移致公式引用/批注引用漂移。
    """
    try:
        if src.stat().st_size < _FAST_MIN_SIZE:
            return "小表走 openpyxl"
        has_formula = has_merge = has_comment = False
        with zipfile.ZipFile(src) as zf:
            for n in zf.namelist():
                if n.startswith("xl/worksheets/") and n.endswith(".xml"):
                    data = zf.read(n)
                    if not has_formula and (b"<f>" in data or b"<f " in data or b"<f/>" in data):
                        has_formula = True
                    if not has_merge and b"<mergeCells" in data:
                        has_merge = True
                if n.startswith("xl/") and n.endswith(".xml") and "comments" in n:
                    has_comment = True
        if has_merge:
            return "含合并单元格"
        structural = bool(edits) and any(
            (e.get("deleted") or e.get("inserts")) for e in edits.values())
        if has_formula and structural:
            return "含公式+结构变更"
        if has_comment and structural:
            return "含批注+结构变更"
        # 含公式/批注但仅更新 → 放行快路径
    except Exception as e:
        return f"zip 读取失败: {e}"
    return None


def _extract_edits(mr) -> Dict[str, Dict[str, Any]]:
    """从合并请求提取每 sheet 的编辑：updates{pk:{col:value}} / deleted[pk] / inserts[{pk,cells}]。

    matched 行收集全部 col!=0 单元格（稀疏行只有主键格，天然只含差异行）——
    不依赖 changed/resolved 标志（pydantic 会丢弃前端 resolved 字段），
    是否真正写入在 _edit_xml 中与文件现值比对（str(value) != str(file_value)）。
    """
    out: Dict[str, Dict[str, Any]] = {}
    for sd in getattr(mr, "sheets", []):
        key: Dict[str, Any] = {"updates": {}, "deleted": [], "inserts": []}
        for row in sd.rows:
            rt = getattr(row, "row_type", "matched")
            if rt == "deleted":
                key["deleted"].append(str(row.key))
                continue
            if rt in ("inserted", "missing_row"):
                key["inserts"].append({
                    "pk": str(row.key),
                    "cells": {c.col: c.value for c in row.cells},
                })
                continue
            # matched：收集 col!=0 单元格（真正差异在 _edit_xml 与文件值比对后落笔）
            for c in row.cells:
                if c.col == 0:
                    continue
                key["updates"].setdefault(str(row.key), {})[c.col] = c.value
        if any(key.values()):
            out[sd.name] = key
    return out


def fast_apply_xml(src: Path, dest: Path, mr) -> Optional[dict]:
    """大数据表 XML 快路径。成功返回 cache_info；不满足条件返回 None（回退 openpyxl）。"""
    edits = _extract_edits(mr)
    reason = _eligible(src, edits)
    if reason:
        logger.info("[FastApply] 跳过 %s: %s", src.name, reason)
        return None
    try:
        if not edits:
            shutil.copy2(src, dest)
            return {"needs_manual_fix": False, "cache_message": "无改动，fast-path 复制通过"}
        with zipfile.ZipFile(src) as zf:
            sheet_map = _sheet_map(zf)
            shared = _load_shared_strings(zf)
            new_entries: Dict[str, bytes] = {}
            for sheet_name, key in edits.items():
                xml_path = sheet_map.get(sheet_name)
                if not xml_path:
                    return None
                new_xml = _edit_xml(zf.read(xml_path), key, shared)
                if new_xml is None:
                    return None
                new_entries[xml_path] = new_xml
        with zipfile.ZipFile(src) as zf, zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as out:
            for item in zf.infolist():
                data = new_entries.get(item.filename, zf.read(item.filename))
                out.writestr(item, data)
        return {"needs_manual_fix": False, "cache_message": "大数据表 fast-path 通过（XML 直改）"}
    except Exception as e:
        logger.warning("[FastApply] XML 快路径失败回退 %s: %s", src.name, e)
        return None


def collect_disk_sheet_pks(path: Path) -> Dict[str, Set[str]]:
    """用 calamine 读落盘表格各 sheet 主键集合（apply 引用校验：前端只传差异行）。"""
    out: Dict[str, Set[str]] = {}
    try:
        from python_calamine import CalamineWorkbook
        wb = CalamineWorkbook.from_path(str(path))
        for sn in wb.sheet_names:
            rows = wb.get_sheet_by_name(sn).to_python()
            pks: Set[str] = set()
            for r in rows[1:]:  # 跳过表头
                if r and r[0] is not None:
                    pks.add(str(r[0]).strip())
            out[sn] = pks
    except Exception as e:
        logger.warning("[FastApply] 读磁盘主键失败 %s: %s", path.name, e)
    return out

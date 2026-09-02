"""
多版本 Excel 比对引擎：以第一列为主键，对所有版本进行行匹配比对。

file_sheets 格式: {文件名: {sheet名: [[cell...], ...]}}
compare_sheet 从中提取当前 sheet 数据，以第一列为主键对齐所有版本的行。
"""

import math
import re
from functools import lru_cache
from pathlib import Path
from typing import List, Dict, Any, Optional


try:
    import yaml as _yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_VC_PATH = Path(__file__).resolve().parent.parent / "agent" / "excel" / "skills" / "L1_derived" / "value_constraints.yaml"
_vc_cache: dict = {}


def _deep_merge_tables(base: dict, extra: dict) -> dict:
    """递归深合并 extra 到 base（extra 优先，dict 级深合并，list 整值替换）。

    与 merge_engine._deep_merge_tables / agent.core.agent._deep_merge_tables 同语义，
    engine 层独立实现避免跨层 import。
    """
    for k, v in extra.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge_tables(base[k], v)
        else:
            base[k] = v
    return base


def _load_value_constraints() -> dict:
    """加载 value_constraints.yaml，并叠加 rules/validate 用户规则 overlay。

    返回 {stem: {sheet: {columns: {col: {type, min, max, unique, regex}}}}}。
    overlay 来自 rules_loader.get_value_constraints_overlay()，与 merge_engine /
    agent.core.agent 读的是同一份用户规则源，engine 层独立合并与缓存，不 import
    agent 层本体，避免跨层耦合。
    """
    try:
        cur_mtime = _VC_PATH.stat().st_mtime if _VC_PATH.exists() else 0.0
    except OSError:
        cur_mtime = 0.0
    if _vc_cache.get("mtime") == cur_mtime and "data" in _vc_cache:
        return _vc_cache["data"]
    if not _HAS_YAML or not cur_mtime:
        tables: dict = {}
    else:
        data = _yaml.safe_load(_VC_PATH.read_text(encoding="utf-8")) or {}
        tables = data.get("tables", {}) or {}
    try:
        from agent.excel.core.rules_loader import get_value_constraints_overlay
        overlay = get_value_constraints_overlay()
        if overlay:
            _deep_merge_tables(tables, overlay)
    except Exception:
        pass
    _vc_cache["data"] = tables
    _vc_cache["mtime"] = cur_mtime
    return _vc_cache["data"]


def _get_col_constraints(table_stem: str, sheet: str, col_header: str) -> dict:
    """查列完整约束配置（type/min/max/unique/regex）。未命中返回空 dict（不校验）。"""
    header = (col_header or "").split(":")[0].strip()
    if not header or not table_stem:
        return {}
    vc = _load_value_constraints()
    sheets = vc.get(table_stem, {})
    cols = sheets.get(sheet, {}).get("columns", {})
    info = cols.get(header) or cols.get(col_header)
    return info or {}


def _get_col_type(table_stem: str, sheet: str, col_header: str) -> str:
    """查列类型标注（int/float/bool/…）。未命中返回空串。"""
    return (_get_col_constraints(table_stem, sheet, col_header).get("type", "") or "").strip().lower()


def _check_formula_result_type(col_cfg, val) -> tuple[bool, str]:
    """公式重算结果约束校验：type（数值强校验）+ min/max（范围，来自 rules/validate）。

    col_cfg 可传纯类型字符串（旧调用方式，向后兼容）或完整列约束 dict。
    空/未命中不校验；regex 对公式数值结果意义不大，此处不做。
    """
    cfg = col_cfg if isinstance(col_cfg, dict) else {"type": col_cfg}
    t = (cfg.get("type", "") or "").strip().lower()
    if not t:
        return True, ""
    if val is None:
        return True, ""
    if t in ("int", "integer", "long", "float", "double", "number"):
        try:
            fv = float(str(val))
        except (ValueError, TypeError):
            return False, f"公式重算结果 {val!r} 与列类型 {t} 不符"
        vmin, vmax = cfg.get("min"), cfg.get("max")
        if vmin is not None and fv < float(vmin):
            return False, f"公式重算结果 {val!r} 小于规则允许的最小值 {vmin}"
        if vmax is not None and fv > float(vmax):
            return False, f"公式重算结果 {val!r} 大于规则允许的最大值 {vmax}"
        return True, ""
    return True, ""


# 将0-based列索引转为Excel列字母（0→A, 25→Z, 26→AA...）
@lru_cache(maxsize=512)
def _col_letter(n: int) -> str:
    """列号转字母：0→A, 1→B, ..., 25→Z, 26→AA。纯函数 + lru_cache 避免主循环 10w 行重复计算。"""
    s = ''
    n += 1
    while n > 0:
        n -= 1
        s = chr(65 + (n % 26)) + s
        n //= 26
    return s


# ------------------------------------------------------------------
# 语义相等归一（#24）：消除 "100" vs 100、"a " vs "a"、0.1 vs "0.10" 这类
# 表示差异导致的假冲突。三方 cell 判定与 recommend_version 多数表决共用同一把尺。
# ------------------------------------------------------------------
def _semantic_key(v: Any):
    """归一化值用于语义相等比较，返回可 == / 入 set 的 tuple。

    - None / 空串 → ('none', '')
    - bool → ('bool', v)  与数值分离，避免 True==1 误判
    - 数值（int/float/可 float() 的字符串）→ ('num', float(v))；inf/nan 退回字符串
    - 其他字符串 → trim 空白后 ('str', s)
    """
    if v is None:
        return ('none', '')
    if isinstance(v, bool):
        return ('bool', v)
    if isinstance(v, (int, float)):
        f = float(v)
        if math.isfinite(f):
            return ('num', f)
        return ('str', str(v))
    s = str(v).strip()
    if s == '':
        return ('none', '')
    try:
        f = float(s)
        if math.isfinite(f):
            return ('num', f)
    except (ValueError, TypeError):
        pass
    return ('str', s)


def _semantic_eq(a: Any, b: Any) -> bool:
    """两值语义相等（trim 空白 + 数值 float 归一 + bool/None 统一）。"""
    return _semantic_key(a) == _semantic_key(b)


# ------------------------------------------------------------------
# 轻量公式求值器：对行内聚合公式（SUM/AVERAGE/MAX/MIN/COUNT）按输入值预览重算
# ------------------------------------------------------------------
_FUNC_RE = re.compile(r"=(\w+)\((.*)\)\s*$", re.IGNORECASE)


def _eval_row_arithmetic(formula: str, row_values: List[Any]) -> Optional[Any]:
    """行内算术表达式求值（=B7+C7+D7+E7、=(B7+C7)*2 等，无函数调用）。

    将单元格引用（A1 / $A$1 等）替换为 row_values 对应数值，安全求值：
    替换后仅允许数字与四则运算符/括号/小数点/空白，否则返回 None。
    含函数调用（SUM/VLOOKUP 等）或跨行/跨表引用的表达式无法求值，返回 None
    （交由 libreoffice 重算兜底）。
    """
    expr = formula.lstrip("=")
    cell_ref_re = re.compile(r"\$?([A-Za-z]+)\$?(\d+)")

    def _replace(m):
        col_idx = _col_letter_to_index(m.group(1))
        v = row_values[col_idx] if 0 <= col_idx < len(row_values) else None
        try:
            return str(float(v)) if v is not None else "0"
        except (ValueError, TypeError):
            return "0"

    expr = cell_ref_re.sub(_replace, expr)
    if not re.match(r"^[\d+\-*/.()\s]+$", expr):
        return None
    try:
        return float(eval(expr, {"__builtins__": {}}, {}))
    except Exception:
        return None


def _eval_row_formula(formula: str, row_values: List[Any]) -> Optional[Any]:
    """按行内值预览聚合公式的重算结果（不依赖 libreoffice）。

    支持 SUM(A?:E?)/AVERAGE/MAX/MIN/COUNT 形式的行内计算公式（引用同行若干列）。
    解析公式引用的列字母，按 row_values（该行各列值，0-based）取数后聚合。
    范围引用 A?:E? → 列区间；单点引用 A? → 单值。
    非函数形式但为行内算术表达式（=B7+C7+D7+E7）由 _eval_row_arithmetic 求值。
    非行内公式（跨行/跨表/含函数与算术混合）返回 None，交由 libreoffice 重算。

    Args:
        formula: 公式文本（如 "=SUM(B3:E3)" 或 "=B3+C3+D3"）
        row_values: 该行所有单元格值（0-based 列索引 → 值）

    Returns:
        重算数值，无法求值返回 None。
    """
    if not formula or not formula.startswith("="):
        return None
    m = _FUNC_RE.match(formula)
    if not m:
        # 非函数形式 → 尝试行内算术表达式（=B7+C7+D7+E7 这种）
        return _eval_row_arithmetic(formula, row_values)
    func = m.group(1).upper()
    args = m.group(2).strip()
    if not args:
        return None

    # 解析参数里的列字母（仅行内范围/单点引用）
    nums: List[float] = []
    for part in args.split(","):
        part = part.strip()
        # 范围 A3:E3 → 列字母区间
        rng = re.match(r"^([A-Za-z]+)\d+:[A-Za-z]+\d+$", part)
        if rng:
            start_col = _col_letter_to_index(rng.group(1))
            end_match = re.search(r":([A-Za-z]+)\d+$", part)
            if not end_match:
                continue
            end_col = _col_letter_to_index(end_match.group(1))
            for ci in range(min(start_col, end_col), max(start_col, end_col) + 1):
                if 0 <= ci < len(row_values):
                    v = row_values[ci]
                    try:
                        nums.append(float(v) if v is not None else 0.0)
                    except (ValueError, TypeError):
                        pass
            continue
        # 单点 A3
        single = re.match(r"^([A-Za-z]+)\d+$", part)
        if single:
            ci = _col_letter_to_index(single.group(1))
            if 0 <= ci < len(row_values):
                v = row_values[ci]
                try:
                    nums.append(float(v) if v is not None else 0.0)
                except (ValueError, TypeError):
                    pass

    if not nums:
        return None
    if func == "SUM":
        return sum(nums)
    if func == "AVERAGE":
        return sum(nums) / len(nums)
    if func == "MAX":
        return max(nums)
    if func == "MIN":
        return min(nums)
    if func == "COUNT":
        return len(nums)
    return None


def _col_letter_to_index(letters: str) -> int:
    """列字母转 0-based 索引：A→0, Z→25, AA→26。"""
    n = 0
    for ch in letters.upper():
        n = n * 26 + (ord(ch) - ord("A") + 1)
    return n - 1


def _extract_version_num(fname: str) -> Optional[int]:
    """从文件名 `xxx_<数字>.xlsx` 后缀提取版本号（复用 id_resolver/R18 同款正则）。

    解析失败（无该后缀）返回 None，交调用方按 other_files 出现顺序兜底。
    """
    m = re.search(r'_(\d+)\.xlsx$', fname, re.IGNORECASE)
    return int(m.group(1)) if m else None


def _pick_author_representatives(
    changed_versions: Dict[str, Any],
    other_files: List[str],
    commit_authors: Dict[str, str],
) -> Dict[str, Any]:
    """将"有改动"的衍生版本按作者分组，每组取代表值。

    同作者若对应多个衍生文件（同一人多次提交），代表值取"最后一次修改"的那个文件：
    按文件名 `_<数字>.xlsx` 版本号取最大者；版本号无法解析时，按 other_files（调用方
    传入 paths 的顺序，通常即提交时间顺序）中的出现顺序取最后一个。
    没有作者信息的文件（commit_authors 未覆盖）各自单独成组（用文件名当分组 key），
    不与其他文件合并，避免误合并无归属的改动。

    返回: {代表分组key: 代表值}，供后续 distinct 去重判断使用。
    """
    groups: Dict[str, List[str]] = {}
    for fname in changed_versions:
        author = commit_authors.get(fname)
        group_key = author if author else f"__no_author__{fname}"
        groups.setdefault(group_key, []).append(fname)

    representatives: Dict[str, Any] = {}
    for group_key, fnames in groups.items():
        if len(fnames) == 1:
            rep_fname = fnames[0]
        else:
            # 同作者多文件：按版本号排序取最大；解析失败则按 other_files 出现顺序取最后一个
            versioned = [(f, _extract_version_num(f)) for f in fnames]
            if all(v is not None for _, v in versioned):
                rep_fname = max(versioned, key=lambda t: t[1])[0]
            else:
                pos = {f: i for i, f in enumerate(other_files)}
                rep_fname = max(fnames, key=lambda f: pos.get(f, -1))
        representatives[group_key] = changed_versions[rep_fname]
    return representatives


# ── 向量化快速路径：numpy 矩阵 diff，大表纯数据场景 10-50× 提速 ──
# 仅适用于无公式/无批注的纯数据 sheet；有公式/批注时回退逐格 Python 循环。
def _tag_inserted_source(
    result_rows: List[dict],
    other_files: List[str],
    version_meta: Optional[Dict[str, Dict[str, Any]]],
) -> None:
    """R18/R25-fix: 为 inserted 行标注来源文件/版本（供导出写批注 + 前端来源徽章）。

    抽成公共函数：慢速逐格路径与向量化快速路径都要用——之前只有慢速路径打了
    这个标记，向量化路径（无公式/无批注的表，覆盖大多数表）在 R25 排查"看不出
    新增/重编号是哪个分支"时发现完全没打，导致这些表的新增行/重编号行前端拿不到
    来源信息。就地修改 result_rows（原地打标记），不返回新对象。
    """
    for r in result_rows:
        if r.get('row_type') != 'inserted':
            continue
        cells = r.get('cells') or []
        src_file = ''
        for fname in other_files:
            for c in cells:
                vers = c.get('versions') or {}
                if vers.get(fname) is not None:
                    src_file = fname
                    break
            if src_file:
                break
        r['source_file'] = src_file
        r['source_version'] = ''
        if src_file:
            # SVN 模式优先取 version_meta 的 rev；demo 模式回退文件名 _N 后缀
            vm = (version_meta or {}).get(src_file) or {}
            if vm.get('rev'):
                r['source_version'] = str(vm['rev'])
            else:
                m = re.search(r'_(\d+)\.xlsx$', src_file, re.IGNORECASE)
                if m:
                    r['source_version'] = m.group(1)


def _compare_sheet_vectorized(
    file_rows: Dict[str, List[List[Any]]],
    base_name: str,
    other_files: List[str],
    all_files: List[str],
    headers: List[str],
    structure_diff: Optional[dict],
    sparse: bool,
    merge_base_file: Optional[str],
    commit_authors: Optional[Dict[str, str]],
    version_meta: Optional[Dict[str, Dict[str, Any]]] = None,
) -> Optional[dict]:
    """numpy 矩阵 diff：对齐 PK 后一次性矩阵比较，有差异才物化单元格。

    返回完整的 compare_sheet 同款 dict，或 None（降级走逐格循环）。
    """
    try:
        import numpy as np
    except ImportError:
        return None

    base_rows = file_rows.get(base_name, [])
    if not base_rows:
        return None

    # 按 PK 对齐：构建 base 的 PK→row 映射
    def _pk(r):
        return str(r[0]).strip() if r and r[0] is not None else ''
    base_pk_map = {_pk(r): r for r in base_rows}

    n_cols = len(headers)
    # 找出最大行数：取 base 行数 + 各衍生 inserted 行
    other_pk_rows: Dict[str, List[tuple]] = {}  # {pk: [(fname, row), ...]}
    for fname in other_files:
        for r in file_rows.get(fname, []):
            pk = _pk(r)
            if pk in base_pk_map or pk:
                other_pk_rows.setdefault(pk, []).append((fname, r))

    all_pks = list(base_pk_map.keys())
    seen = set(all_pks)
    for pk in other_pk_rows:
        if pk not in seen:
            all_pks.append(pk)
            seen.add(pk)

    if not all_pks:
        return None

    n_rows = len(all_pks)
    pk_to_idx = {pk: i for i, pk in enumerate(all_pks)}

    # 构建 numpy 矩阵：object dtype（兼容混合类型 None/int/str/float）
    # 4.1/4.2 真向量化:同时预计算 _semantic_key 矩阵(每格归一化 key 一次),
    # 后续 base↔衍生判等改 numpy != 广播(C 层 tuple 比较),替换三层 Python 循环
    # (_semantic_eq 每次重算两个 key + 函数调用开销)。未填充格默认 ('none','')
    # (_semantic_key(None));逐行赋 tuple 列表避免 np.full(tuple) 广播歧义。
    matrices: Dict[str, np.ndarray] = {}
    key_mats: Dict[str, np.ndarray] = {}
    none_row = [('none', '')] * n_cols
    for fname in all_files:
        mat = np.full((n_rows, n_cols), None, dtype=object)
        kmat = np.empty((n_rows, n_cols), dtype=object)
        for i, pk in enumerate(all_pks):
            kmat[i] = none_row  # 默认全 ('none','') = _semantic_key(None)
            row = None
            if pk in base_pk_map and fname == base_name:
                row = base_pk_map[pk]
            else:
                for of, orow in other_pk_rows.get(pk, []):
                    if of == fname:
                        row = orow
                        break
            if row:
                for j in range(min(n_cols, len(row))):
                    v = row[j]
                    mat[i, j] = v
                    kmat[i, j] = _semantic_key(v)
        matrices[fname] = mat
        key_mats[fname] = kmat

    base_mat = matrices[base_name]
    base_keys = key_mats[base_name]

    # 快速判等：base 与各衍生 key 矩阵 numpy 广播比较,替换三层逐格循环
    # 对每行每列，如果所有衍生值都与 base 一致 → 不变；否则标记差异
    has_diff = np.zeros((n_rows, n_cols), dtype=bool)
    for fname in other_files:
        has_diff |= (base_keys != key_mats[fname])
    if n_cols > 0:
        has_diff[:, 0] = False  # PK 列不判等

    # 构建结果行：有差异的行物化全行单元格，无差异行走 sparse
    result_rows: List[dict] = []
    conflict_count = changed_count = inserted_count = deleted_count = 0

    for ri, pk in enumerate(all_pks):
        has_base = pk in base_pk_map
        has_other = pk in other_pk_rows
        if not has_base and has_other:
            row_type = 'inserted'
            inserted_count += 1
        elif has_base and not has_other:
            row_type = 'deleted'
            deleted_count += 1
        else:
            row_type = 'matched'

        # 检查是否有差异列
        row_has_diff = has_diff[ri].any() if row_type == 'matched' else True
        if sparse and row_type == 'matched' and not row_has_diff:
            pk_val = base_mat[ri, 0]
            result_rows.append({
                'key': pk,
                'cells': [{
                    'col': 0, 'col_letter': _col_letter(0), 'value': pk_val,
                    'versions': {}, 'conflict': False, 'changed': False,
                    'diff_type': '', 'formula_changed': False, 'formula_source': '',
                    'formula_text': '', 'comments': {}, 'comment_conflict': False,
                    'author_resolved': False, 'formula_notice': '',
                }],
                'row_type': row_type,
                'presence': {base_name: has_base, **{fn: fn in (dict(other_pk_rows.get(pk, []))) for fn in other_files}},
            })
            continue

        # 物化整行单元格（有差异或非 matched）
        cells: List[dict] = []
        for ci in range(n_cols):
            versions: Dict[str, Any] = {}
            for fname in all_files:
                versions[fname] = matrices[fname][ri, ci]
            base_val = versions.get(base_name)
            display_val = base_val if base_val is not None else next(
                (versions[f] for f in all_files if versions.get(f) is not None), None)

            changed = False
            has_conflict = False
            diff_type = ''
            if ci != 0 and row_type == 'matched' and has_diff[ri, ci]:
                changed_versions = {}
                for fname in other_files:
                    ov = versions.get(fname)
                    if not _semantic_eq(ov, base_val):
                        changed_versions[fname] = ov
                if changed_versions:
                    distinct = set(_semantic_key(v) for v in changed_versions.values())
                    if len(distinct) == 1:
                        changed = True
                        if merge_base_file:
                            display_val = next(iter(changed_versions.values()))
                    else:
                        has_conflict = True
                        diff_type = 'content'
                        conflict_count += 1
                        changed_count += 1
                if changed and not has_conflict:
                    changed_count += 1

            cells.append({
                'col': ci, 'col_letter': _col_letter(ci), 'value': display_val,
                'versions': versions, 'conflict': has_conflict, 'changed': changed,
                'diff_type': diff_type, 'formula_changed': False, 'formula_source': '',
                'formula_text': '', 'comments': {}, 'comment_conflict': False,
                'author_resolved': False, 'formula_notice': '',
            })

        presence = {base_name: has_base}
        for fn in other_files:
            presence[fn] = any(of == fn for of, _ in other_pk_rows.get(pk, []))
        result_rows.append({'key': pk, 'cells': cells, 'row_type': row_type,
                           'presence': presence})

    # R25-fix: 向量化快速路径此前完全没调用 resolve_id_conflicts——多分支各自新增
    # 同一 PK 但内容不同的行（真实 ID 冲突，如 dev1/dev2 都插入 id=9999 但改成不同
    # 名字）在这条路径上会被直接合并成一行、双方 versions 都保留却从不触发冲突/
    # 重编号判定（旧代码硬编码返回空 id_resolution，且 inserted 行的 diff 分支写死
    # 只在 row_type=='matched' 时生效，inserted 行整体跳过判定）。凡是不含公式/
    # 批注的表都会走这条快速路径，等于这类表的多分支新增 ID 冲突检测完全失效。
    # 这里补上跟慢速逐格路径一致的 id 冲突解析，行为对齐。
    from .id_resolver import resolve_id_conflicts
    id_res = resolve_id_conflicts(result_rows, headers, base_name, all_files, mode="split")
    result_rows = id_res["resolved_rows"]
    # split 可能重建/替换行，按最终 result_rows 重新统计（分组前按 pk 循环的计数
    # 在 split 后可能不再准确，比如 1 个合并行拆成 2 个 inserted 行）
    inserted_count = sum(1 for r in result_rows if r.get('row_type') == 'inserted')
    deleted_count = sum(1 for r in result_rows if r.get('row_type') == 'deleted')

    # R25: 新增行标来源分支（之前只有慢速路径打了这个标记，向量化路径完全没打，
    # 导致这类表——即多数无公式/无批注的表——的"新增行是哪个分支加的"、
    # "重编号冲突是哪个分支的编号被改了"在前端完全看不出来）。
    _tag_inserted_source(result_rows, other_files, version_meta)

    return {
        'rows': result_rows,
        'headers': headers,
        'stats': {
            'total_rows': len(result_rows),
            'conflicts': conflict_count,
            'changed': changed_count,
            'inserted': inserted_count,
            'deleted': deleted_count,
            'missing_rows': 0,
            'formula': 0,
            'formula_changed': 0,
            'formula_conflicts': 0,
            'comment_conflicts': 0,
        },
        'missing_rows': [],
        'structure_diff': structure_diff,
        'id_resolution': {
            'id_mapping': id_res['id_mapping'],
            'conflicts': id_res['conflicts'],
            'pk_conflicts': id_res['pk_conflicts'],
            'stats': id_res['stats'],
        },
    }


# 核心比对函数：以第一列为主键，逐Sheet逐单元格比对多版本差异
def compare_sheet(
    file_sheets: Dict[str, Dict[str, List[List[Any]]]],
    base_name: str,
    sheet_name: str,
    file_formulas: Optional[Dict[str, Dict[str, List[List[Any]]]]] = None,
    detect_missing: bool = False,
    file_comments: Optional[Dict[str, Dict[str, List[List[Any]]]]] = None,
    merge_base_file: Optional[str] = None,
    commit_authors: Optional[Dict[str, str]] = None,
    version_meta: Optional[Dict[str, Dict[str, Any]]] = None,
    sparse: bool = True,
    table_stem: Optional[str] = None,
) -> dict:
    """
    以第一列为主键，对所有版本进行行匹配比对。

    参数:
        file_sheets: {文件名: {sheet名: [[cell...], ...]}}（read_excel 的 data_only 缓存值）
        base_name:   基准文件名
        sheet_name:  要比对的 sheet 名
        file_formulas: {文件名: {sheet名: [[公式文本...], ...]}}（read_formulas 的公式文本）。
            提供时启用公式列识别：公式文本一致的列标 diff_type='formula' 不算 changed/conflict，
            若引用的输入值在冲突选择后变化则标 formula_changed + 重算预览值。
        detect_missing: M3 漏行检测模式。True 时 base 有但所有衍生均缺的行标 row_type='missing_row'
            （P0 告警，区别于普通 'deleted'）。用于全量覆盖分支（testbranch 等）合入场景。
        file_comments: M7 批注三方 diff。{文件名: {sheet名: [[comment_or_None...], ...]}}（read_comments）。
            提供时收集各版本批注文本，各版本批注不同则标 comment_conflict=True。
        merge_base_file: 三方合并的公共祖先文件名（fork 快照）。提供时启用三方判定：base_name
            应为 merge_base_file，ours/theirs 为衍生；changed 单元格 display_val 取改了的那方
            采纳值（而非 base_val），避免单向改动静默丢失。未提供时退两方行为不变。
        commit_authors: {文件名: 作者名}（由调用方从 svn log 结果传入）。提供时非公式列冲突判定
            按"同作者取代表值、跨作者比较代表值"规则（见 D3 设计决策）；未提供时维持原规则
            （所有衍生值一视同仁）。注意：本函数是"多版本一次性比对"，没有逐次提交回放的时间轴，
            故"同作者取最后一次修改的值"在当前数据结构下具体化为——同作者若映射了多个衍生文件
            （如 devbranch1 的三次提交 item_1/2/3.xlsx 均属同一作者），按文件名 `_<数字>.xlsx` 后缀
            解析出的版本号取最大（最新）的那个文件代表该作者；若版本号无法解析，则按 other_files
            （即调用方传入 paths 的顺序，通常已是提交时间顺序）里的出现顺序取最后一个。
        table_stem: 表名词干（如 'match_stat'），用于从 value_constraints.yaml 查列类型。
            提供时启用公式列重算结果的类型校验：若某版本重算结果与列标注类型（如 平均场次:float）
            不符，在 formula_notice 附"公式重算结果类型不符"提示，供前端展示。未提供则不校验。

    返回: {'rows': [...], 'headers': [...], 'stats': {...}, 'missing_rows': [...], 'structure_diff': Optional[dict]}
         rows 中每个元素为 dict: {'key': str, 'cells': [...], 'row_type': str}
         missing_rows: 漏行摘要列表 [{key, pk, sheet}]
         structure_diff: 表头结构差异（M5），None 表示无差异
    """
    # ── 提取当前 sheet 的数据: {文件名: [[cell...]]} ──
    single_sheet: Dict[str, List[List[Any]]] = {}
    for fname, sheets in file_sheets.items():
        single_sheet[fname] = sheets.get(sheet_name, [])

    # M7: 提取当前 sheet 的批注矩阵
    single_comments: Dict[str, List[List[Any]]] = {}
    if file_comments:
        for fname, sheets in file_comments.items():
            single_comments[fname] = sheets.get(sheet_name, [])

    # 公式文本（若调用方提供 file_formulas）
    single_formulas: Dict[str, List[List[Any]]] = {}
    if file_formulas:
        for fname, sheets in file_formulas.items():
            single_formulas[fname] = sheets.get(sheet_name, [])

    all_files = list(single_sheet.keys())
    # 衍生文件列表（排除基准文件）
    other_files = [f for f in all_files if f != base_name]

    # 分离表头与数据行：所有文件共用一个表头（取自第一个有数据的文件）
    file_rows: Dict[str, List[List[Any]]] = {}
    headers: List[str] = []
    file_headers: Dict[str, List[str]] = {}  # M5: 各文件表头（按列名），用于结构差异检测
    for fname in all_files:
        data = single_sheet[fname]
        # 空Sheet：跳过
        if not data or len(data) == 0:
            file_rows[fname] = []
            continue
        # 取第一个非空文件的第1行作为表头
        if not headers:
            headers = [str(c) if c is not None else '' for c in data[0]]
        file_headers[fname] = [str(c) if c is not None else '' for c in data[0]]
        # 数据行从第2行开始
        file_rows[fname] = data[1:] if len(data) > 1 else []

    # ── M5: 表头结构差异检测（轻量告警，不改列对齐逻辑）──
    # 各文件表头按列名集合比较：增/删/重排均报告，比对仍按列号进行（保持现有行为）
    structure_diff: Optional[dict] = None
    if len(file_headers) > 1:
        base_hdrs = file_headers.get(base_name) or list(file_headers.values())[0]
        base_set = set(base_hdrs)
        diffs: dict = {"base_headers": base_hdrs, "files": {}}
        has_diff = False
        for fname, hdrs in file_headers.items():
            if fname == base_name:
                continue
            f_set = set(hdrs)
            added = sorted(f_set - base_set)
            removed = sorted(base_set - f_set)
            reordered = hdrs != base_hdrs and not added and not removed
            if added or removed or reordered:
                diffs["files"][fname] = {
                    "headers": hdrs,
                    "added_cols": added,
                    "removed_cols": removed,
                    "reordered": reordered,
                }
                has_diff = True
        if has_diff:
            structure_diff = diffs

    # ── 以第一列为主键收集所有行 ──
    # key_map: {主键值: [group, ...]}，每个 group = {文件名: 行数据(list)}
    # 多个分支同主键的 inserted 行不合并为一条（避免误判成"同一行内容冲突"），
    # 而是各自独立成 group，交 id_resolver 检测并重映射后到者主键到未占用值。
    key_map: Dict[str, List[Dict[str, list]]] = {}
    key_order: List[tuple] = []  # [(主键值, group索引)]，保持行顺序（基准优先）

    def _extract_pk(row: list) -> str:
        if len(row) > 0 and row[0] is not None:
            return str(row[0]).strip()
        return ''

    # 先遍历基准文件确定行的展示顺序（基准行始终进该 pk 的 group 0）
    if base_name in file_rows:
        for row in file_rows[base_name]:
            pk = _extract_pk(row)
            groups = key_map.get(pk)
            if not groups:
                groups = [{}]
                key_map[pk] = groups
                key_order.append((pk, 0))
            groups[0][base_name] = row

    # 再遍历衍生文件：基准有此 pk → 并入 group 0（matched）；
    # 基准无此 pk（inserted）→ 新建独立 group（不同分支同 pk 不合并）
    for fname in other_files:
        for row in file_rows.get(fname, []):
            pk = _extract_pk(row)
            groups = key_map.get(pk)
            if not groups:
                groups = []
                key_map[pk] = groups
            if groups and base_name in groups[0]:
                groups[0][fname] = row
            else:
                # 已有同文件 group 则并入（同文件内重复 pk 取最后一条），否则新建
                g = next((gg for gg in groups if fname in gg), None)
                if g is None:
                    g = {}
                    groups.append(g)
                    key_order.append((pk, len(groups) - 1))
                g[fname] = row

    # ── 逐行逐列构建比对结果 ──
    result_rows: List[dict] = []
    conflict_count = 0
    changed_count = 0
    inserted_count = 0
    deleted_count = 0
    formula_count = 0          # 公式列单元格数（diff_type='formula'）
    formula_changed_count = 0  # 公式列因引用值变化需重算的单元格数
    formula_conflict_count = 0  # 公式文本各版本不一致（diff_type='formula_conflict'）
    formula_row_drift_count = 0  # R25: 公式文本未变但行物理位置漂移（非冲突，独立统计，不计入 conflicts）
    comment_conflict_count = 0  # 批注文本各版本不一致

    # M7 性能优化：预建 {fname: {id(row): index_in_single_sheet}} 哈希映射。
    # 行循环内原有多处 list.index(row_obj) 线性扫描，10w 行 × 100 列 ≈ 500 亿次 → 卡死。
    # single_sheet[fname] 含表头(index 0)，file_rows[fname]=data[1:] 与之共享内层 list 引用，
    # 故 id() 可 O(1) 定位 row_obj 在 single_sheet 中的下标（批注/公式矩阵按此下标取行）。
    row_idx_maps: Dict[str, Dict[int, int]] = {
        fname: {id(r): i for i, r in enumerate(single_sheet.get(fname, []))}
        for fname in all_files
    }

    # ── 全 sheet 公式/批注是否存在的预检（零则 Phase A 稀疏判定跳过对应检查，避免逐格扫描）──
    formulas_active = False
    if single_formulas:
        for frows in single_formulas.values():
            for r in frows:
                for c in r:
                    if isinstance(c, str) and c.startswith("="):
                        formulas_active = True
                        break
                if formulas_active:
                    break
            if formulas_active:
                break
    comments_active = False
    if single_comments:
        for crows in single_comments.values():
            for r in crows:
                if any(c is not None for c in r):
                    comments_active = True
                    break
            if comments_active:
                break
    # ── 向量化快速路径：无公式无批注的纯数据 sheet 走 numpy 矩阵 diff ──
    if not formulas_active and not comments_active and not detect_missing:
        vec = _compare_sheet_vectorized(
            file_rows, base_name, other_files, all_files, headers,
            structure_diff, sparse, merge_base_file, commit_authors,
            version_meta,
        )
        if vec is not None:
            return vec
    for pk, gi in key_order:
        file_versions = key_map[pk][gi]

        # M7: 预计算各文件该行在数据矩阵中的行索引（供取批注；公式判定也复用）
        file_row_idx: Dict[str, int] = {}
        if single_comments or formulas_active:
            for fname, row_obj in file_versions.items():
                ri = row_idx_maps.get(fname, {}).get(id(row_obj), -1)
                if ri >= 0:
                    file_row_idx[fname] = ri

        # 判断行类型：
        #   inserted - 基准没有，衍生有 → 新增行
        #   deleted  - 基准有，所有衍生都没有 → 删除行
        #   missing_row - 同 deleted 但 detect_missing=True（全量覆盖分支场景）→ P0 漏行告警
        #   matched  - 基准与至少一个衍生都有 → 正常匹配行
        has_base = base_name in file_versions
        has_any_other = any(fname in file_versions for fname in other_files)
        if not has_base and has_any_other:
            row_type = 'inserted'
            inserted_count += 1
        elif has_base and not has_any_other:
            if detect_missing:
                row_type = 'missing_row'
            else:
                row_type = 'deleted'
                deleted_count += 1
        else:
            row_type = 'matched'

        # 找到该行在所有文件中的最大列数
        # R25-fix: 若只看 value 矩阵(file_versions)算列数，会漏掉"只有公式、没有
        # 缓存计算值"的尾部列——value 矩阵读取器(calamine/openpyxl data_only=True)
        # 按每行末尾非空值裁剪，公式格如果从未在 Excel/LibreOffice 里重算过缓存值
        # 为 None，会被当成"空尾列"裁掉，导致该列在 value 矩阵里长度不够，从而
        # range(max_cols) 永远到不了那一列，公式冲突/漂移检测全部失效（哪怕
        # formulas_active=True，逐列扫描也扫不到超出 max_cols 的列）。这里额外把
        # 公式矩阵同一行的长度一起纳入 max，保证公式列不会因为没有缓存值被裁没。
        max_cols = 0
        for row in file_versions.values():
            max_cols = max(max_cols, len(row))
        if formulas_active:
            for fname in file_versions.keys():
                _ri = file_row_idx.get(fname, -1)
                if _ri < 0:
                    continue
                _frows = single_formulas.get(fname, [])
                if _ri < len(_frows):
                    max_cols = max(max_cols, len(_frows[_ri]))

        # ── Phase A：稀疏化预判定（仅 sparse=True 启用）──
        # matched 行若所有版本所有非主键列值一致、无公式单元格、无批注 → 不展开稠密 cells，
        # 仅保留主键单元格（cells=[pk_cell]）。把"全表物化 10w×100 个 15 字段 dict"降为
        # "仅变动行物化"，是 10w 行可达秒级的关键；JSON/内存从 GB 级降到 MB 级。
        # 正确性：① 合并导出走基准克隆，未变动行就地保留，pk_cell 即足够；
        # ② id_resolver/collect_cross_sheet_pks 的主键均取 cells[0]=pk_cell，pk_cell 保留；
        # ③ ref_integrity 对未变动行 FK 列(len(cells)==1 → ci>=len 跳过)由基准克隆兜底，
        #    其值本就等于基准（行被判定为不变），语义不变。
        # 启用范围：仅合并引导路径 _build_group(sparse=True)；上传/文件夹/历史恢复路径保持
        # dense（默认 False）以兼容"显示全部行"视图与既有断言。
        if sparse and row_type == 'matched':
            # M20-perf: 预提取 base/other 行对象 + 预 str 化 base 列，避免逐列重复 get/索引/str。
            _base_row = file_versions.get(base_name, [])
            _other_rows = [file_versions.get(_fn, []) for _fn in other_files]
            _base_strs = ['' if _v is None else str(_v) for _v in _base_row]
            # M20-perf: 无公式无批注时，用 C 层 list== 快速判等（短路，10x 快于逐列 Python 循环）。
            # 全等行直接 sparse 短路；存在差异时走稠密物化（无需逐列定位差异列，公式/批注才需精确定位）。
            if not formulas_active and not comments_active:
                _sparse_ok = all(_base_row == _r for _r in _other_rows)
            else:
                # 有公式/批注时仍需逐列精扫（任一公式格/批注格需保留以展示）
                _sparse_ok = True
                for _ci in range(max_cols):
                    # 公式单元格（任一文件该格为公式 → 需标 formula/formula_conflict，保留）
                    if formulas_active:
                        for _fn in all_files:
                            _ri = file_row_idx.get(_fn, -1)
                            if _ri < 0:
                                continue
                            _frows = single_formulas.get(_fn, [])
                            if _ri < len(_frows) and _ci < len(_frows[_ri]):
                                _fv = _frows[_ri][_ci]
                                if isinstance(_fv, str) and _fv.startswith("="):
                                    _sparse_ok = False
                                    break
                        if not _sparse_ok:
                            break
                    # 批注（任一格有批注 → 保留以展示；comments_active 为假时整 sheet 无批注，跳过）
                    if comments_active:
                        for _fn in all_files:
                            _ri = file_row_idx.get(_fn, -1)
                            if _ri < 0:
                                continue
                            _crows = single_comments.get(_fn, [])
                            if _ri < len(_crows) and _ci < len(_crows[_ri]):
                                if _crows[_ri][_ci] is not None:
                                    _sparse_ok = False
                                    break
                        if not _sparse_ok:
                            break
                    # 值差（非主键列；主键因按 pk 分组天然一致）
                    if _ci != 0:
                        _bstr = _base_strs[_ci] if _ci < len(_base_strs) else ''
                        for _r in _other_rows:
                            _v = _r[_ci] if _ci < len(_r) else None
                            _vstr = '' if _v is None else str(_v)
                            if _vstr != _bstr:
                                _sparse_ok = False
                                break
                        if not _sparse_ok:
                            break
            if _sparse_ok:
                # 仅保留主键单元格；全等行 versions 置空（各版本值一致，前端只读 value，
                # 省 10w 行 × N 版本的 JSON/代理开销，payload 与内存显著下降）。
                # apply 对 matched 行仅写 col!=0 单元格，sparse 行只有主键格 → 天然跳过，无副作用。
                _pk_val = None
                for _fn in all_files:
                    _r = file_versions.get(_fn, [])
                    _v = _r[0] if _r else None
                    if _pk_val is None and _v is not None and _fn == base_name:
                        _pk_val = _v
                if _pk_val is None:
                    for _fn in all_files:
                        _r = file_versions.get(_fn, [])
                        if _r:
                            _pk_val = _r[0]
                            break
                result_rows.append({
                    'key': pk,
                    'cells': [{
                        'col': 0,
                        'col_letter': _col_letter(0),
                        'value': _pk_val,
                        'versions': {},
                        'conflict': False,
                        'changed': False,
                        'diff_type': '',
                        'formula_changed': False,
                        'formula_source': '',
                        'formula_text': '',
                        'comments': {},
                        'comment_conflict': False,
                        'author_resolved': False,
                        'formula_notice': '',
                        'formula_row_drift': False,
                    }],
                    'row_type': row_type,
                    'presence': {
                        base_name: base_name in file_versions,
                        **{fn: fn in file_versions for fn in other_files},
                    },
                })
                continue

        cells: List[dict] = []
        for col_idx in range(max_cols):
            # 收集各文件在该列的值
            versions: Dict[str, Any] = {}
            for fname in all_files:
                row = file_versions.get(fname, [])
                versions[fname] = row[col_idx] if col_idx < len(row) else None

            # M7: 收集各文件该单元格的批注文本 + 冲突检测
            comments: Dict[str, Optional[str]] = {}
            if single_comments:
                for fname in all_files:
                    ri = file_row_idx.get(fname, -1)
                    if ri < 0:
                        continue
                    cm_rows = single_comments.get(fname, [])
                    if ri < len(cm_rows) and col_idx < len(cm_rows[ri]):
                        cm = cm_rows[ri][col_idx]
                        if cm is not None:
                            comments[fname] = cm
            distinct_comments = set(str(c) for c in comments.values() if c is not None)
            comment_conflict = len(distinct_comments) > 1

            base_val = versions.get(base_name)
            # 显示值优先取基准文件的值，基准为None则取第一个非None值
            display_val = base_val
            if display_val is None:
                for fname in all_files:
                    if versions[fname] is not None:
                        display_val = versions[fname]
                        break

            # ── 公式列识别（若提供 file_formulas）──
            # 定位基准行在公式矩阵的行号，取该列公式文本判断是否公式列
            # M7-3: 用 formulas_active 守卫（大表跳过公式读取时 single_formulas 为空 list，
            # file_formulas 虽有 key 但无数据，访问空 list 会越界）。
            is_formula_col = False
            base_formula_text = ""
            cell_formulas: Dict[str, Any] = {}
            if formulas_active:
                # 优先取基准公式文本；基准无此行（inserted）则取第一个有公式的衍生版本
                if base_name in single_formulas:
                    base_row_obj = file_versions.get(base_name, [])
                    base_row_idx_in_data = row_idx_maps.get(base_name, {}).get(id(base_row_obj), -1)
                    if base_row_idx_in_data >= 0:
                        base_formula_row = single_formulas[base_name][base_row_idx_in_data]
                        if col_idx < len(base_formula_row):
                            fv = base_formula_row[col_idx]
                            if isinstance(fv, str) and fv.startswith("="):
                                is_formula_col = True
                                base_formula_text = fv
                                cell_formulas[base_name] = fv
                # 基准无公式（inserted 行）→ 从衍生版本取公式文本作为重算模板
                if not is_formula_col:
                    for fname in other_files:
                        f_row_obj = file_versions.get(fname, [])
                        f_idx = row_idx_maps.get(fname, {}).get(id(f_row_obj), -1)
                        if f_idx < 0:
                            continue
                        f_formula_rows = single_formulas.get(fname, [])
                        if f_idx >= len(f_formula_rows):
                            continue
                        f_formula_row = f_formula_rows[f_idx]
                        if col_idx < len(f_formula_row):
                            fv = f_formula_row[col_idx]
                            if isinstance(fv, str) and fv.startswith("="):
                                is_formula_col = True
                                base_formula_text = fv
                                cell_formulas[fname] = fv
                                break

            # 收集各文件公式文本（仅在公式列时，补全未收集的版本）
            if is_formula_col:
                for fname in all_files:
                    if fname in cell_formulas:
                        continue
                    f_row_obj = file_versions.get(fname, [])
                    f_idx = row_idx_maps.get(fname, {}).get(id(f_row_obj), -1)
                    if f_idx < 0:
                        continue
                    f_formula_rows = single_formulas.get(fname, [])
                    if f_idx >= len(f_formula_rows):
                        continue
                    f_formula_row = f_formula_rows[f_idx]
                    if col_idx < len(f_formula_row):
                        fv = f_formula_row[col_idx]
                        if isinstance(fv, str) and fv.startswith("="):
                            cell_formulas[fname] = fv

            # ── 类 Git 三方合并判定 ──
            # 公式列：公式文本一致 → 引用值变化触发重算(formula_changed)；
            #         公式文本不一致（各版本公式写法不同）→ 作为冲突供用户选择公式版本
            if is_formula_col:
                # 公式文本是否各版本一致
                formula_texts = set(
                    str(v) for v in cell_formulas.values() if v is not None
                )
                author_resolved = False  # 公式列不涉及作者维度自动合并规则

                # 4.1: 公式文本本身不一致（各版本公式写法不同）→ 作为冲突供用户选择公式版本
                if len(formula_texts) > 1:
                    has_conflict = True
                    changed = False
                    diff_type = "formula_conflict"
                    formula_notice = "公式文本在各版本间不一致，请选择采纳的公式版本"
                    # 公式文本冲突时也校验：各版本公式若可重算，结果类型/范围与列约束不符则提示
                    if table_stem:
                        _col_header = headers[col_idx] if col_idx < len(headers) else ""
                        _col_cfg = _get_col_constraints(table_stem, sheet_name, _col_header)
                        if _col_cfg:
                            bad = []
                            for fn in all_files:
                                ft = cell_formulas.get(fn)
                                if not ft:
                                    continue
                                ev = _eval_row_formula(ft, file_versions.get(fn, []))
                                ok_t, _ = _check_formula_result_type(_col_cfg, ev)
                                if not ok_t:
                                    bad.append(fn)
                            if bad:
                                formula_notice += f"；{','.join(bad)} 公式重算结果与列约束（{_col_cfg.get('type','')}）不符"
                    # versions 改填各文件公式文本，供前端冲突选择 UI 按公式文本展示与对比
                    versions = {fn: cell_formulas.get(fn) for fn in all_files}
                    # 显示值取基准公式文本（基准无公式时取第一个有公式的版本）
                    display_val = base_formula_text or next(
                        (v for v in cell_formulas.values() if v), display_val
                    )
                    formula_changed = False
                    formula_source = ""
                    preview_val = None
                    formula_text = base_formula_text
                    formula_row_drift = False  # 已经是文本冲突，漂移提示无意义，不重复标
                else:
                    # 公式文本一致 → 不算修改；引用值变化 → 标 formula_changed（原行为不变）
                    has_conflict = False
                    changed = False
                    diff_type = "formula"
                    formula_notice = ""
                    formula_changed = False
                    formula_source = ""
                    preview_val = None
                    # 列约束（value_constraints.yaml + rules/validate 深合并）：
                    # 用于校验重算结果类型/取值范围
                    _col_header = headers[col_idx] if col_idx < len(headers) else ""
                    _col_cfg = _get_col_constraints(table_stem or "", sheet_name, _col_header) if table_stem else {}
                    if base_formula_text:
                        # 公式文本一致 → 用各版本输入值重算，看是否与基准缓存不同
                        base_eval = _eval_row_formula(
                            base_formula_text,
                            file_versions.get(base_name, []),
                        )
                        for fname in other_files:
                            other_eval = _eval_row_formula(
                                base_formula_text,
                                file_versions.get(fname, []),
                            )
                            if other_eval is not None and other_eval != base_eval:
                                formula_changed = True
                                formula_source = fname
                                preview_val = other_eval
                                # 公式重算结果约束校验：衍生输入导致重算结果与列类型/范围不符时提示
                                if _col_cfg:
                                    ok_t, why_t = _check_formula_result_type(_col_cfg, other_eval)
                                    if not ok_t:
                                        formula_notice = f"公式重算结果类型不符（列类型 {_col_type}）：{why_t}"
                                break
                    # 公式列显示值：优先取引用值变化后的重算预览值（反映采纳衍生输入后的结果），
                    # 否则取基准缓存值；基准无缓存值时用基准输入重算兜底
                    if formula_changed and preview_val is not None:
                        display_val = preview_val
                    elif display_val is None and base_formula_text:
                        display_val = _eval_row_formula(
                            base_formula_text,
                            file_versions.get(base_name, []),
                        )
                    # 公式文本（供 inserted 行导出写入公式；matched 行由导出跳过 formula 列保留）
                    formula_text = base_formula_text

                    # ── R25: 公式行漂移风险提示（非冲突）──
                    # 场景：公式文本各版本完全一致（未落入 formula_conflict，没有"多个不同
                    # 版本供选择"的场景），但该 PK 行在各版本 sheet 中的物理行位置不同（因
                    # 其他行被增删导致位移）。若公式内嵌了绝对行号引用（如 =B5*100+C5），
                    # 引用目标很可能仍指向"位移前"的旧行号——但这属于"潜在风险提示"，
                    # 不是"合并冲突"：各分支公式文本完全一样，没有版本分歧可供用户选择，
                    # 不应占用 conflict 状态、不应弹出选版本框。仅作非阻塞标记供人工核实，
                    # 不计入 conflict_count/stats.conflicts，也不改变 diff_type（仍是
                    # 'formula'，行为与既有公式列一致，只是附加一个风险位 + 提示文案）。
                    formula_row_drift = False
                    row_positions: Dict[str, int] = {}
                    for _fn in all_files:
                        _fro = file_versions.get(_fn, [])
                        _ridx = row_idx_maps.get(_fn, {}).get(id(_fro), -1)
                        if _ridx >= 0:
                            row_positions[_fn] = _ridx
                    if (
                        len(set(row_positions.values())) > 1
                        and re.search(r'[A-Za-z]{1,3}\d+', base_formula_text)
                    ):
                        formula_row_drift = True
                        formula_notice = (
                            f"提示（非冲突）：公式文本未变，但该行在各版本中的物理位置不同"
                            f"（行号: {row_positions}），很可能是其他行被增删导致位移，"
                            f"公式里的绝对行号引用未必仍指向本行数据，建议人工核实"
                        )
            else:
                # 非公式列：原有类 Git 三方合并判定
                # #24: 基准值用语义归一 key（"100"==100=="1e2"、"a "=="a"），
                # 避免表示差异被判真冲突。base_key 仅用于等值比较，display 取原值。
                base_key = _semantic_key(base_val)

                # 收集衍生版本中"与基准不同"的版本及其值
                changed_versions: Dict[str, Any] = {}
                for fname in other_files:
                    ov = versions.get(fname)
                    if _semantic_key(ov) != base_key:
                        changed_versions[fname] = ov

                # 3.2: 若提供 commit_authors，先按作者分组取代表值（同作者取"最后一次修改"
                # 的代表值，见 _pick_author_representatives 注释），再对代表值做 distinct 判断；
                # 未提供 commit_authors 时代表值集合＝原始改动集合，规则与现状完全一致。
                author_resolved = False
                if commit_authors and len(changed_versions) > 1:
                    representative_values = _pick_author_representatives(
                        changed_versions, other_files, commit_authors
                    )
                else:
                    representative_values = changed_versions

                # 衍生改动值去重集合（基于代表值）；#24 用语义归一 key 去重，
                # 使 "100.0" 与 "1e2" 归并为同一代表值 → 单向变更而非真冲突。
                distinct_changed = set(
                    _semantic_key(v) for v in representative_values.values()
                )

                if not changed_versions:
                    # 无衍生版本改动：完全一致
                    has_conflict = False
                    changed = False
                elif len(distinct_changed) == 1:
                    # 所有改动版本（或同作者归并后的代表值）改成同一值 → 单向变更，可自动采纳
                    has_conflict = False
                    changed = True
                    # 仅当"归并前存在多个原始改动、归并后代表值只剩1个"才算因同作者规则自动合并；
                    # 只有单个改动、或未提供 commit_authors 时不打此标记（保持语义精确，便于审计）
                    if (
                        commit_authors
                        and len(changed_versions) > 1
                        and len(representative_values) < len(changed_versions)
                    ):
                        author_resolved = True
                else:
                    # 多个（跨作者）代表值不同 → 真冲突，需人工解决
                    has_conflict = True
                    changed = False

                # 标记差异类型并累计统计
                diff_type = ''
                formula_changed = False
                formula_source = ""
                formula_text = ""
                formula_notice = ""
                formula_row_drift = False  # 非公式列，恒为 False
                if has_conflict:
                    diff_type = 'content'

                # 三方合并：changed（单向变更）display_val 取改了的那方采纳值，不取 base_val。
                # 修复 base=merge-base 下 display_val 取 base_val 导致导出跳过、生产者单向改动静默丢失。
                # 两方模式（无 merge_base_file）保持 display_val=base_val 行为不变。
                if merge_base_file and changed and not has_conflict and changed_versions:
                    display_val = next(iter(changed_versions.values()))

            # 统计累计（公式文本冲突计入 conflict；公式列单独计入 formula，不进 conflict/changed）
            # M7-2: 主循环一次累加完，删掉末尾 5 次全量 cell 遍历重算（原 675-688/709）。
            if diff_type == 'content':
                conflict_count += 1
                changed_count += 1
            elif diff_type == 'formula_conflict':
                conflict_count += 1
                formula_conflict_count += 1
            elif diff_type == 'formula':
                formula_count += 1
                if formula_changed:
                    formula_changed_count += 1
                if formula_row_drift:
                    # R25: 非冲突风险提示，不计入 conflict_count/stats.conflicts，
                    # 只累计一个独立计数供前端后续可选展示（如"N 处公式行位置漂移，建议核实"）。
                    formula_row_drift_count += 1
            if comment_conflict:
                comment_conflict_count += 1

            cells.append({
                'col': col_idx,
                'col_letter': _col_letter(col_idx),
                'value': display_val,
                'versions': versions,
                'conflict': has_conflict,
                'changed': changed,
                'diff_type': diff_type,
                'formula_changed': formula_changed,
                'formula_source': formula_source,
                'formula_text': formula_text,
                'comments': comments,
                'comment_conflict': comment_conflict,
                'author_resolved': author_resolved,
                'formula_notice': formula_notice,
                'formula_row_drift': formula_row_drift,
            })

        result_rows.append({
            'key': pk,
            'cells': cells,
            'row_type': row_type,
            # 非破坏字段：该行在各文件中是否存在，供前端并排两表推导左右行态
            'presence': {
                base_name: base_name in file_versions,
                **{fn: fn in file_versions for fn in other_files},
            },
        })

    # ── 多分支同主键 inserted 行：重映射后到者主键到未占用值 ──
    # compare 阶段已让同 pk 的 inserted 行各自独立成行，此处检测重复 pk 并自增，
    # 在被重映射行上打 id_remapped/original_pk 供前端展示与导出写批注。
    # M6: 此处 mode=split 与下游 merge_engine.resolve_and_validate_sheet 的 split 是
    # 独立路径（前者属 /compare 端点，后者属 /merge 端点），非串联双重调用。
    # resolve_id_conflicts 幂等：已 split 的行 versions 仅单一文件，
    # _inserted_source_files 返回单文件 → 步骤1 直接跳过不再重映射，二次调用安全。
    from .id_resolver import resolve_id_conflicts
    id_res = resolve_id_conflicts(result_rows, headers, base_name, all_files, mode="split")
    result_rows = id_res["resolved_rows"]

    # R18: 为 inserted 行标注来源文件/版本（供导出写批注 + 前端来源徽章）
    # 放在 id_resolver 之后：split 会重建行丢弃额外字段，故此处按 versions 重新推导
    _tag_inserted_source(result_rows, other_files, version_meta)

    # 重算统计：仅行级（split 可能增删 inserted 行数）；cell 级统计已在主循环累加，
    # id_resolver split 重建 cells 时 dict(c) 复制保留全部标志位，cell 级计数不变。
    inserted_count = sum(1 for r in result_rows if r.get('row_type') == 'inserted')
    deleted_count = sum(1 for r in result_rows if r.get('row_type') == 'deleted')
    missing_rows_count = sum(1 for r in result_rows if r.get('row_type') == 'missing_row')

    # M3: 漏行摘要（供前端快速定位 + 补回接口消费）
    missing_rows_summary = [
        {'key': r.get('key', ''), 'pk': r.get('key', ''), 'sheet': sheet_name}
        for r in result_rows if r.get('row_type') == 'missing_row'
    ]

    return {
        'rows': result_rows,
        'headers': headers,
        'stats': {
            'total_rows': len(result_rows),
            'conflicts': conflict_count,
            'changed': changed_count,
            'inserted': inserted_count,
            'deleted': deleted_count,
            'missing_rows': missing_rows_count,
            'formula': formula_count,
            'formula_changed': formula_changed_count,
            'formula_conflicts': formula_conflict_count,
            'formula_row_drift': formula_row_drift_count,
            'comment_conflicts': comment_conflict_count,
        },
        'missing_rows': missing_rows_summary,
        'structure_diff': structure_diff,
        'id_resolution': {
            'id_mapping': id_res['id_mapping'],
            'conflicts': id_res['conflicts'],
            'pk_conflicts': id_res['pk_conflicts'],
            'stats': id_res['stats'],
        },
    }

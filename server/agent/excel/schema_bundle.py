"""Schema Bundle 模块（§2.2 lazy schema 拉取 + schema_bundle 精细化）。

任务文档假设 HTTP `/api/tables/{stem}/sheets/{sheet}?include_columns=1` + ThreadPool。
R21 HTTP API 已落地（`routers/tables.py:68`），但 excel-agent 同进程,cli 直读本地
文件更快（HTTP 自调用浪费）。本模块基于 cli + read_sheet 构造 schema_bundle,
供 ValidateAgent `data_getter` 用（§4 ④⑤⑥ 数据注入）。

§2.2 现状：`DecomposeAgent._build_schema_block` 用 `cli.read_header`（row1+row2）。
本模块扩展：加表数据（`existing_values`/`result_rows`）+ path/stem/sheet/cli 注入。

`build_data_getter(agent, intents)` 返回 `data_getter(intent) -> dict`:
  {path, stem, sheet, cli, existing_values, result_rows}
  （`vc`/`enum_set` 由 `validate_field_layer` 内部 `_load_value_constraints`/
   `_check_enum_whitelist` 纯函数读,或调用方额外注入）

§2.2 HTTP 化（独立服务调远程 API）待 excel-agent 部署为独立服务时做,
现状同进程 cli 直读。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)


# §非业务 sheet 过滤：说明/CONFIG 等辅助 sheet 在 schema 读取时直接跳过，
# 连读都不读。原因：(1) 它们是给策划看的说明/枚举页，不是业务数据；
# (2) 空说明 sheet（height=0）会触发 python-calamine 的 Rust panic
# （pet_evolve.xlsx「灵兽进化表说明」实测），跳过可根治该类崩溃。
def _is_business_sheet_name(name) -> bool:
    if not name:
        return False
    s = str(name).strip()
    if s.upper() == "CONFIG":
        return False
    return not any(m in s for m in ("说明", "备注", "Sheet1", "程序用勿删", "勿删"))


def _business_sheets(sheets) -> list:
    return [s for s in (sheets or []) if _is_business_sheet_name(s)]


def _resolver_of(agent):
    """取 agent 上的 TableResolver 实例（属性名兼容多写法）。"""
    for attr in ("_table_resolver", "resolver", "table_resolver"):
        r = getattr(agent, attr, None)
        if r is not None:
            return r
    return None


def _resolve_sheet(agent, stem: str) -> str:
    """stem -> sheet 名（sheet_hint 空时回退，供 data_getter/schema_getter 用）。

    优先 TableResolver；无 resolver 时经 cli 解析 path + get_sheets 取首个 sheet。
    """
    if not stem:
        return ""
    r = _resolver_of(agent)
    if r is not None:
        try:
            for m in ("resolve", "find_path", "resolve_path"):
                resolve = getattr(r, m, None)
                if callable(resolve):
                    try:
                        p = resolve(stem)
                    except Exception:
                        continue
                    if p is None:
                        continue
                    sheet = getattr(p, "sheet", "") or getattr(p, "sheet_name", "")
                    if sheet:
                        return sheet
                    break
        except Exception:
            pass
    # cli 兜底：经 _resolve_path 拿 path，再 get_sheets 取首个
    cli = getattr(agent, "cli", None)
    if cli is not None:
        path = _resolve_path(agent, stem)
        if path is not None:
            get_sheets = getattr(cli, "get_sheets", None)
            if callable(get_sheets):
                try:
                    sheets = get_sheets(path)
                    if sheets:
                        return str(sheets[0])
                except Exception:
                    pass
    return ""


def _resolve_path(agent, stem: str) -> Optional[Path]:
    """stem -> Path（_stem_to_path 漏时的回退，复用 resolver.resolve 的 path 字段 +
    cli.list_tables Path-aware 匹配，大小写不敏感）。"""
    if not stem:
        return None
    _stem_l = stem.lower()
    r = _resolver_of(agent)
    if r is not None:
        try:
            for m in ("resolve", "find_path", "resolve_path"):
                resolve = getattr(r, m, None)
                if callable(resolve):
                    try:
                        p = resolve(stem)
                        if p is None and stem != _stem_l:
                            p = resolve(_stem_l)  # 大小写回退
                    except Exception:
                        continue
                    if p is None:
                        continue
                    path = getattr(p, "path", None) or getattr(p, "file", None)
                    if path is not None:
                        return Path(path) if not isinstance(path, Path) else path
                    break
        except Exception:
            pass
    # cli.list_tables Path-aware 兜底（agent 无 resolver 时，大小写不敏感）
    cli = getattr(agent, "cli", None)
    if cli is not None:
        _stem_l = stem.lower()
        list_tables = getattr(cli, "list_tables", None)
        if callable(list_tables):
            for t in list_tables():
                if (getattr(t, "stem", "") or "").lower() != _stem_l:
                    continue
                if isinstance(t, Path):
                    return t
                p = getattr(t, "path", None) or getattr(t, "file", None)
                if p is not None:
                    return Path(p) if not isinstance(p, Path) else p
    return None


def _stem_to_path(agent, stem: str) -> Optional[Path]:
    """stem -> Path（经 agent TableResolver 或 cli.list_tables）。

    stem 匹配大小写不敏感：LLM(parse_multi) 偶把 sheet 名当 table_hint
    （如 'Reward' 而非 'reward'）→ 旧大小写敏感匹配 path=None →
    existing_values 空 → Core4 漏检 PK 冲突 → 落 Step3。
    """
    if not stem:
        return None
    _stem_l = stem.lower()
    try:
        # 优先用 agent 现有 path 解析（TableResolver）
        for attr in ("_table_resolver", "resolver", "table_resolver"):
            resolver = getattr(agent, attr, None)
            if resolver is not None:
                for m in ("resolve", "find_path", "resolve_path"):
                    resolve = getattr(resolver, m, None)
                    if callable(resolve):
                        try:
                            p = resolve(stem)
                            if p is None and stem != _stem_l:
                                p = resolve(_stem_l)  # 大小写回退
                            if p is not None:
                                return Path(p) if not isinstance(p, Path) else p
                        except Exception:
                            pass
                break
        # fallback: cli.list_tables 匹配 stem（大小写不敏感）
        cli = getattr(agent, "cli", None)
        if cli is not None:
            list_tables = getattr(cli, "list_tables", None)
            if callable(list_tables):
                for t in list_tables():
                    if (getattr(t, "stem", "") or "").lower() != _stem_l:
                        continue
                    # list_tables 返回 Path（StubCodeMakerCLI）或 table 对象
                    # （带 .path/.file）。Path 本身即路径，直接用；否则取属性。
                    if isinstance(t, Path):
                        return t
                    p = getattr(t, "path", None) or getattr(t, "file", None)
                    if p is not None:
                        return Path(p) if not isinstance(p, Path) else p
    except Exception:
        logger.debug("_stem_to_path 失败 stem=%s", stem, exc_info=True)
    return None


def _existing_values_from_rows(headers, rows) -> dict:
    """从 rows 算每列已有值集合（纯函数,不读表,§4 ④唯一性用）。

    Returns:
        {col_lower: set(values)}。仅含非空值。
    """
    if not rows or not headers:
        return {}
    col_idx = {}
    for i, h in enumerate(headers):
        if h is not None and i < (len(rows[0]) if rows else 0):
            col_idx.setdefault((str(h) or "").split(":")[0].strip().lower(), i)
    existing = {}
    for col_lower, idx in col_idx.items():
        vals = set()
        for row in rows:
            if idx < len(row):
                v = row[idx]
                if v is not None and str(v).strip():
                    vals.add(v)
        if vals:
            existing[col_lower] = vals
    return existing


def _composite_existing_from_rows(headers, rows, pk_norm_cols: list) -> set:
    """从 rows 算复合主键组合值集合（§复合键 ④ 组合唯一性用）。

    pk_norm_cols: 复合键列名的小写归一名列表（如 ['法宝id', '法宝等级']）。
    返回 { (v1, v2, ...) } 每行 PK 列值的 str 元组集合（仅含行内 PK 列全非空者）。
    单元素列集合时仍返回（语义即单列，供统一接口）。
    """
    if not rows or not headers or not pk_norm_cols:
        return set()
    # 列名归一小写 -> 列索引
    idx_of = {}
    for i, h in enumerate(headers):
        if h is None:
            continue
        nl = (str(h) or "").split(":")[0].strip().lower()
        if nl and nl not in idx_of:
            idx_of[nl] = i
    pick_idx = []
    for pc in pk_norm_cols:
        if not pc:
            continue
        idx = idx_of.get(pc.lower()) if isinstance(pc, str) else idx_of.get(pc)
        if idx is None:
            return set()  # 任一 PK 列不在表头 → 无法算组合，返空让上层放行
        pick_idx.append(idx)
    if len(pick_idx) < 2:
        return set()
    out = set()
    for row in rows:
        combo = []
        ok = True
        for idx in pick_idx:
            v = row[idx] if idx < len(row) else None
            sv = str(v).strip() if v is not None else ""
            if not sv:
                ok = False
                break
            combo.append(sv)
        if ok:
            out.add(tuple(combo))
    return out


def _read_existing_values(cli, path, sheet, headers) -> dict:
    """读表数据 + 算每列已有值集合（§4 ④唯一性用）。

    内部调 _existing_values_from_rows（read_sheet 一次）。
    """
    if cli is None or path is None or not sheet or not headers:
        return {}
    read_sheet = getattr(cli, "read_sheet", None)
    if not callable(read_sheet):
        return {}
    try:
        rows = read_sheet(path, sheet)
    except Exception:
        return {}
    return _existing_values_from_rows(headers, rows)


def _rows_to_dicts(headers, rows) -> list:
    """list[list] rows -> list[dict]（按 headers 列名,§4 ⑥result_rows 用）。"""
    if not rows or not headers:
        return []
    out = []
    for row in rows:
        d = {}
        for i, h in enumerate(headers):
            if h is not None and i < len(row):
                d[(str(h) or "").split(":")[0].strip()] = row[i]
        out.append(d)
    return out


def build_data_getter(agent, intents: list = None, sheet_resolver=None):
    """构造 data_getter 供 validate_field_layer 用（§2.2 schema_bundle 精细化）。

    data_getter(intent) -> dict {
        path, stem, sheet, cli, existing_values, result_rows,
        pk_cols, composite_existing, vc, enum_set
    }

    sheet_resolver: 可选 callable(path, intent) -> str，供 sheet_hint 空时统一
        解析目标 sheet。不传则回退 _resolve_sheet(agent, stem)（旧路径，与 Step3
        不一致——多 sheet 表会落到首 sheet）。传入与 Step3 同源的 _resolve_sheet
        可保 Step2/Step3 sheet 判定一致，根治"校验读错 sheet → PK 漏检"。

    pk_cols/composite_existing: §复合主键注入。data_getter 读 rules overlay 的
        primary_key，按列名算目标 sheet 的组合值集合，供 validator 组合唯一性检测。

    复用 agent.cli + _stem_to_path + read_sheet + read_header。
    vc/enum_set 由 validate_field_layer 内部纯函数读,或调用方额外注入。

    Args:
        agent: TableAgent 实例（提供 cli + path 解析）
        intents: 预留（批量预取优化,现状按需 lazy）
        sheet_resolver: 见上，统一 sheet 解析入口

    Returns:
        data_getter callable
    """
    cli = getattr(agent, "cli", None)
    # 预载 rules 的 primary_key overlay（{stem_lower: {sheet: [cols]}}）
    try:
        from .core.rules_loader import get_primary_key_overlay, _norm_col
        _pk_overlay = get_primary_key_overlay() or {}
    except Exception:
        _pk_overlay = {}
        _norm_col = lambda c: (str(c or "").split(":")[0].strip().lower())  # noqa: E731

    def _data_getter(intent):
        stem = getattr(intent, "table_hint", "") or ""
        sheet = getattr(intent, "sheet_hint", "") or ""
        # sheet 回退：sheet_hint 空时统一走与 Step3 同源的 _resolve_sheet(path,intent)
        # （若调用方传 sheet_resolver），否则回退 resolver 首业务 sheet（旧路径，
        # 与 Step3 不一致——多 sheet 表会错读导致 PK 漏检）。
        if not sheet and stem:
            sheet = _resolve_sheet(agent, stem)
        path = _stem_to_path(agent, stem)
        # path 回退：_stem_to_path 漏时也经 resolver 取 path
        if path is None and stem:
            path = _resolve_path(agent, stem)
        # §sheet 一致性：sheet_hint 空且调用方注入 sheet_resolver → 走与 Step3 同源
        # 的 _resolve_sheet(path, intent)，保证校验与执行判同一 sheet。
        if (not getattr(intent, "sheet_hint", None)) and path is not None \
                and callable(sheet_resolver):
            try:
                _rs = sheet_resolver(path, intent)
                if _rs:
                    sheet = _rs
            except Exception:
                logger.debug("sheet_resolver 失败 stem=%s", stem, exc_info=True)
        existing_values = {}
        result_rows = []
        headers = []
        rows = []
        if cli is not None and path is not None and sheet:
            try:
                read_header = getattr(cli, "read_header", None)
                headers = read_header(path, sheet) if callable(read_header) else []
                read_sheet_fn = getattr(cli, "read_sheet", None)
                rows = read_sheet_fn(path, sheet) if callable(read_sheet_fn) else []
                if headers:
                    existing_values = _existing_values_from_rows(headers, rows)
                    result_rows = _rows_to_dicts(headers, rows)
            except Exception:
                logger.debug("build_data_getter 读表失败 stem=%s sheet=%s",
                             stem, sheet, exc_info=True)
        # §跨 sheet 合并：同表多 sheet 时，PK 冲突检测只扫目标 sheet 会漏
        # （如 29004 在 sheet A 占用、新数据写 sheet B）。扫该 path 全部 sheet，
        # 把各 sheet 的 existing_values 并入。result_rows 仅留目标 sheet（供
        # 语义校验，不跨 sheet）。全表扫首次慢，_load 有缓存故可接受。
        if cli is not None and path is not None:
            try:
                get_sheets_fn = getattr(cli, "get_sheets", None)
                if callable(get_sheets_fn):
                    _all_sheets = get_sheets_fn(path) or []
                    # 跳过说明/CONFIG 等非业务 sheet（不读、不参与 PK 冲突检测）
                    _others = [s for s in _all_sheets
                               if s != sheet and _is_business_sheet_name(s)]
                    for _sh in _others:
                        try:
                            _hdrs = (read_header(path, _sh)
                                     if callable(read_header) else [])
                            _rows = (read_sheet_fn(path, _sh)
                                     if callable(read_sheet_fn) else [])
                            if _hdrs:
                                _ev = _existing_values_from_rows(_hdrs, _rows)
                                for _k, _set in _ev.items():
                                    existing_values.setdefault(_k, set()).update(_set)
                        except Exception:
                            continue
            except Exception:
                logger.debug("build_data_getter 跨 sheet 扫描失败 path=%s",
                             path, exc_info=True)
        # 值约束（type/min/max/unique）注入，供 validate_field_layer 范围检查
        # run_semantic_gate 用。合并通配表/sheet（"*"）与 rules overlay。
        vc = {}
        try:
            from .core.agent import _load_value_constraints
            _vc_all = _load_value_constraints()
            for _stem_key in ("*", stem):
                _t = _vc_all.get(_stem_key, {}) or {}
                if not isinstance(_t, dict):
                    continue
                for _sheet_key in ("*", sheet):
                    _s = _t.get(_sheet_key, {}) or {}
                    if not isinstance(_s, dict):
                        continue
                    _cols = _s.get("columns", {})
                    if isinstance(_cols, dict):
                        for _k, _v in _cols.items():
                            if isinstance(_v, dict):
                                vc.setdefault(_k, {}).update(_v)
                            else:
                                vc.setdefault(_k, _v)
        except Exception:
            logger.debug("value_constraints 注入失败 stem=%s sheet=%s",
                         stem, sheet, exc_info=True)
        # 用户规则枚举白名单（rules/validate/*.md 的 enum 字段），
        # 合并通配表/sheet（"*"）后注入 enum_set，供 validate_field_layer 强校验。
        enum_set = {}
        try:
            from .core.rules_loader import get_enum_overlay
            _eo = get_enum_overlay()
            for _stem_key in ("*", stem):
                _t = _eo.get(_stem_key, {}) or {}
                if not isinstance(_t, dict):
                    continue
                for _sheet_key in ("*", sheet):
                    _s = _t.get(_sheet_key, {}) or {}
                    if not isinstance(_s, dict):
                        continue
                    for _k, _v in _s.items():
                        if isinstance(_v, (set, list, tuple)):
                            enum_set.setdefault(_k, set()).update(_v)
        except Exception:
            logger.debug("rules enum overlay 加载失败 stem=%s sheet=%s",
                         stem, sheet, exc_info=True)
        # §复合主键：取本表本 sheet 的 PK 列（rules primary_key 声明优先），
        # 算目标 sheet 的组合值集合，供 validator 组合唯一性检测。
        pk_cols: list = []
        if stem and sheet:
            _stem_l = stem.lower()
            if _stem_l in _pk_overlay:
                sheets_map = _pk_overlay[_stem_l]
                if isinstance(sheets_map, dict):
                    pk_cols = sheets_map.get(sheet) or sheets_map.get(
                        next((s for s in sheets_map
                              if s and s.lower() == sheet.lower()), None), []) or []
                    if not pk_cols and "*" in sheets_map:
                        pk_cols = sheets_map.get("*") or []
        composite_existing = set()
        if pk_cols and len(pk_cols) >= 2 and headers and result_rows:
            try:
                composite_existing = _composite_existing_from_rows(
                    headers, rows, [_norm_col(c) for c in pk_cols if c])
            except Exception:
                logger.debug("composite_existing 算失败 stem=%s sheet=%s",
                             stem, sheet, exc_info=True)
        return {
            "path": path, "stem": stem, "sheet": sheet, "cli": cli,
            "existing_values": existing_values,
            "result_rows": result_rows,
            "pk_cols": pk_cols,
            "composite_existing": composite_existing,
            "vc": vc,
            "enum_set": enum_set,
        }

    return _data_getter


__all__ = ["build_data_getter", "_stem_to_path",
           "_resolve_sheet", "_resolve_path",
           "_read_existing_values", "_rows_to_dicts",
           "_existing_values_from_rows", "_composite_existing_from_rows"]

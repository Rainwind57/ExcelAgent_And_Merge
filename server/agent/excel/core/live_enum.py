"""运行时枚举发现：从当前工作区的 xlsx 现场解析「中文标签 → 数字码」映射。

背景：label→code 的解析不应依赖预生成到代码目录的 enum_mappings.yaml——
换工作区（另一套 resources/）时预生成文件可能不存在或内容不匹配。本模块提供
**零配置**的第 2 层：给定 cli（含 workspace），现场打开同工作区的 xlsx 提取映射。

分层解析优先级（label→code，高→低）：
  1. 用户业务规则 rules/validate/*.md 的 ``enum_map``（业务手填，最高）
  2. 工作区现场发现（本模块，零配置，换工作区自适应）
  3. L1 预生成缓存 enum_mappings.yaml（启动时 regenerate_skills 产出）
  4. LLM 推断 + pending（最后手段，EnumResolver.register_label）

本模块只做第 2 层，纯读、不写任何文件，天然支持多工作区/热切换。

识别来源（通用判据，不绑业务表名/测例）：
  a) 类型表：sheet 标题以 Type/类型 结尾，含一个 int 码列 + 一个 string/名称列
     （如 ItemType 的 道具类型:int + 类型名称:string → 资源=1/礼包=2…）。
  b) 说明表：块状排布「标签行 + 配置数字行」或横排 A1=列名 B1=标签 B2=值
     （如「道具表说明」的 道具品质: 凡品/良品/… + 配置数字: 1/2/…）。
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_TYPE_SHEET_RE = re.compile(r"(type|类型)$", re.IGNORECASE)
_VALUE_MARKERS = ("配置数字", "值", "value", "数值", "数字", "码", "code", "编码")


def _s(v: Any) -> str:
    return "" if v is None else str(v).strip()


def _int(v: Any) -> Optional[int]:
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def _norm_col(c: str) -> str:
    """列名归一：去类型后缀/空白/括号注释，小写。

    保留下划线——与 rules_loader._norm_col（split ':' 后 strip+lower）口径一致，
    避免英文规范键 item_type 被压成 itemtype 后与规则键/类型表键失配。
    """
    if not c:
        return ""
    c = str(c).split(":")[0].strip()
    c = re.sub(r"[（(].*?[）)]", "", c)
    return re.sub(r"[\s\-./\\]+", "", c).lower()


class LiveEnumSource:
    """工作区现场枚举发现（只读，缓存按文件 mtime 失效）。"""

    def __init__(self, cli: Any = None):
        self._cli = cli
        self._path_cache: dict[str, Optional[Path]] = {}
        self._wb_cache: dict[Path, tuple[float, dict[str, dict[str, int]]]] = {}

    def _path_for(self, stem: str) -> Optional[Path]:
        if stem in self._path_cache:
            return self._path_cache[stem]
        path = None
        if self._cli is not None:
            try:
                for p in self._cli.list_tables() or []:
                    if getattr(p, "stem", None) == stem or p.stem == stem:
                        path = p
                        break
            except Exception:
                path = None
        self._path_cache[stem] = path
        return path

    def _scan_workbook(self, path: Path) -> dict[str, dict[str, int]]:
        """扫一个 xlsx，返回 {col_norm: {label: code}}（类型表 + 说明表合并）。"""
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return {}
        cached = self._wb_cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        out: dict[str, dict[str, int]] = {}
        try:
            import openpyxl
            wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
            try:
                for ws in wb.worksheets:
                    title = ws.title or ""
                    if title.upper() == "CONFIG":
                        continue
                    # 只扫类型表/说明表，跳过大数据业务表（避免整表逐行遍历）
                    _is_type = bool(_TYPE_SHEET_RE.search(title))
                    _is_explain = any(k in title for k in ("说明", "枚举", "字典", "desc", "备注"))
                    if not _is_type and not _is_explain:
                        continue
                    rows = list(ws.iter_rows(values_only=True))
                    if not rows:
                        continue
                    if _is_type:
                        self._scan_type_sheet(rows, out)
                    if _is_explain:
                        self._scan_explain_sheet(rows, out)
            finally:
                wb.close()
        except Exception:
            logger.debug("LiveEnumSource 扫工作簿失败 %s", path, exc_info=True)
        self._wb_cache[path] = (mtime, out)
        return out

    @staticmethod
    def _scan_type_sheet(rows: list, out: dict[str, dict[str, int]]) -> None:
        """类型表：row0 表头 / row1 类型标注 / row2+ 数据，码列+名称列 → label→code。"""
        if len(rows) < 3 or not rows[0] or not rows[1]:
            return
        headers = [_s(h).split(":")[0].strip() for h in rows[0]]
        type_annot = [_s(t).lower() for t in rows[1]]
        code_idx = name_idx = None
        for i, ta in enumerate(type_annot):
            if not ta:
                continue
            if code_idx is None and (ta.endswith(":int") or ta.endswith(":integer")):
                code_idx = i
            if name_idx is None and i != code_idx and (
                    ta.endswith(":string") or ta.endswith(":str")):
                name_idx = i
            if code_idx is not None and name_idx is not None:
                break
        if code_idx is None or name_idx is None:
            return
        code_norm = _norm_col(headers[code_idx]) if code_idx < len(headers) else ""
        if not code_norm:
            return
        entries: dict[str, int] = {}
        for r in rows[2:]:
            if not r:
                continue
            code = _int(r[code_idx]) if code_idx < len(r) else None
            label = _s(r[name_idx]) if name_idx < len(r) else ""
            if code is not None and label:
                entries[label] = code
        if len(entries) >= 2:
            out[code_norm] = entries

    @staticmethod
    def _scan_explain_sheet(rows: list, out: dict[str, dict[str, int]]) -> None:
        """说明表：块状「标签行 + 配置数字行」/ 横排 A1=列名 B1=标签 B2=值。"""
        n = len(rows)
        # 块状 / 横排：第 i 行是 [列名, label...]，下一行是 [配置数字/值, value...]
        for i in range(n - 1):
            r = rows[i]
            if not r or r[0] is None:
                continue
            col_name = _s(r[0])
            if not col_name:
                continue
            nxt = rows[i + 1]
            if not nxt:
                continue
            if _s(nxt[0]).lower() not in _VALUE_MARKERS:
                continue
            labels = [_s(x) for x in r[1:] if _s(x)]
            vals = [_int(x) for x in nxt[1:]]
            if len(labels) < 2:
                continue
            entries = {lbl: val for lbl, val in zip(labels, vals) if lbl and val is not None}
            if len(entries) >= 2:
                col_norm = _norm_col(col_name)
                if col_norm:
                    out[col_norm] = entries

    def mapping(self, stem: str, sheet: str, col: str) -> dict[str, int]:
        """取某数据列的 label→code 全量映射（按列名归一匹配）。"""
        path = self._path_for(stem)
        if path is None:
            return {}
        col_maps = self._scan_workbook(path)
        query = _norm_col(col)
        if not query:
            return {}
        # 精确优先，contains 兜底
        best: Optional[dict] = None
        best_rank = 99
        for c_norm, entries in col_maps.items():
            rank = None
            if c_norm == query:
                rank = 0
            elif query in c_norm or c_norm in query:
                rank = 1
            if rank is not None and rank < best_rank:
                best_rank = rank
                best = entries
        return dict(best) if best else {}

    def lookup(self, stem: str, sheet: str, col: str, label: str) -> Optional[int]:
        """中文标签 → code。精确/去空格匹配，近似多义不臆测（交上层）。"""
        mapping = self.mapping(stem, sheet, col)
        if not mapping or not label:
            return None
        nl = label.strip()
        if nl in mapping:
            return mapping[nl]
        for k, v in mapping.items():
            if k.strip() == nl:
                return v
        # 子串包含唯一才采用（保守，多义放弃）
        contains = [k for k in mapping if nl in k or k in nl]
        if len(contains) == 1:
            return mapping[contains[0]]
        return None


def resolve_enum_label(stem: str, sheet: str, col: str, label: str,
                       resolver: Any = None, live: "LiveEnumSource" = None,
                       rules: dict = None) -> Optional[int]:
    """统一 label→code 解析链（规则 > 现场发现 > L1/pending 缓存）。"""
    # 1) 用户规则 enum_map（最高）
    if rules:
        m = _rules_lookup(rules, stem, sheet, col)
        if m:
            v = _label_hit(m, label)
            if v is not None:
                return v
    # 2) 现场发现
    if live is not None:
        v = live.lookup(stem, sheet, col, label)
        if v is not None:
            return v
    # 3) EnumResolver（L1 + pending + 近似）
    if resolver is not None:
        try:
            return resolver.resolve_label(stem, sheet, col, label)
        except Exception:
            return None
    return None


def _rules_lookup(rules: dict, stem: str, sheet: str, col: str) -> Optional[dict]:
    if not rules:
        return None
    sheets = rules.get(stem) or rules.get(str(stem).lower())
    if not isinstance(sheets, dict):
        return None
    cols = sheets.get(sheet)
    if cols is None:
        for sn, c in sheets.items():
            if sn and str(sn).lower() == str(sheet).lower():
                cols = c
                break
    if not isinstance(cols, dict):
        return None
    q = _norm_col(col)
    for c_norm, m in cols.items():
        # 规则键由 rules_loader._norm_col 存（split:strip:lower），这里统一重归一比较
        if _norm_col(c_norm) == q:
            return m if isinstance(m, dict) else None
    return None


def _label_hit(mapping: dict, label: str) -> Optional[int]:
    if not mapping or not label:
        return None
    nl = label.strip()
    if nl in mapping:
        return mapping[nl]
    for k, v in mapping.items():
        if str(k).strip() == nl:
            return v
    contains = [k for k in mapping if nl in str(k) or str(k) in nl]
    if len(contains) == 1:
        return mapping[contains[0]]
    return None


# ── 模块级共享（按 cli 缓存，跨调用复用现场发现结果；不写文件）──

_live_sources: dict[int, "LiveEnumSource"] = {}


def get_live_enum_source(cli: Any = None) -> Optional["LiveEnumSource"]:
    """取（并缓存）某 cli 对应的现场枚举发现器。cli=None 返回 None。"""
    if cli is None:
        return None
    key = id(cli)
    src = _live_sources.get(key)
    if src is None:
        src = LiveEnumSource(cli)
        _live_sources[key] = src
    return src


def resolve_label_full(cli: Any, stem: str, sheet: str, col: str, label: str,
                       resolver: Any = None) -> Optional[int]:
    """统一 label→code 解析链入口（供各调用点复用，避免四处拼装）。

    优先级：用户规则 enum_map > 工作区现场发现 > L1/pending 缓存（EnumResolver）。
    rules 覆盖层每次现取（rules_loader 已缓存底层 rules，开销小且支持热更新语义）。
    """
    if not stem or not label:
        return None
    try:
        from .rules_loader import get_enum_map_overlay
        _rules = get_enum_map_overlay()
    except Exception:
        _rules = None
    _live = get_live_enum_source(cli)
    return resolve_enum_label(stem, sheet, col, label,
                              resolver=resolver, live=_live, rules=_rules)

"""用户规则加载器：项目根 rules/ 目录（与 skills/ 隔离）。

规则分两类，均为用户手打的 Markdown 文档：

  1. 填表规则  rules/fill/*.md
     Step1 parse 阶段注入 LLM prompt 的知识（自然语言给 AI 读），
     帮助 AI 理解输入、选列、填值、归一化枚举、按 ID 分段取值。
     加载后拼成「填表规则（强约束）」文本块，附在解析 prompt 前缀。

  2. 校验规则  rules/validate/*.md
     Step2 validate 阶段的强约束（机器执行）。
     md 文档内嵌 yaml 代码块，加载器提取解析为结构化约束：
       type / required / enum / min / max / unique / regex。
     下游合并进 value_constraints / required_fields / enum_set 校验管线。

加载失败或目录缺失不抛异常，返回空结果，保证 agent 降级运行。
与 skills/ 的关系：本模块只读 rules/，不触碰 skills/*.yaml；
规则优先级高于 L1_derived 自动派生配置（用户显式声明 > 自动派生）。
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# 项目根/rules 目录：从 server/agent/excel/core 上溯 4 级到项目根。
# 支持 env 覆盖（CODEMAKER_RULES_DIR），指向自定义规则目录。
_DEFAULT_RULES_DIR = Path(__file__).resolve().parents[4] / "rules"
_RULES_DIR = Path(os.environ.get("CODEMAKER_RULES_DIR", "") or _DEFAULT_RULES_DIR)

_FILL_DIR = _RULES_DIR / "fill"
_VALIDATE_DIR = _RULES_DIR / "validate"

# yaml 代码块提取：```yaml ... ```
_FENCE_RE = re.compile(r"```\s*(?:yaml|yml)\s*\n(.*?)```", re.DOTALL)

# 校验规则缓存
_VALIDATE_CACHE: Optional[dict] = None
_FILL_CACHE: Optional[dict] = None  # {stem: text}，_global.md 单独取


def _read_md(path: Path) -> str:
    """读 md 全文，失败返回空串。"""
    if not path.exists():
        return ""
    try:
        return path.read_text(encoding="utf-8") or ""
    except Exception:
        logger.warning("规则文件读取失败 %s", path, exc_info=True)
        return ""


def _extract_yaml_blocks(text: str) -> list[dict]:
    """提取 md 里全部 yaml 代码块并 safe_load，失败块跳过。"""
    out: list[dict] = []
    if not text or not _HAS_YAML:
        return out
    for m in _FENCE_RE.finditer(text):
        try:
            data = yaml.safe_load(m.group(1))
            if isinstance(data, dict):
                out.append(data)
        except Exception:
            logger.warning("校验规则 yaml 块解析失败，跳过", exc_info=True)
    return out


def _deep_merge(base: dict, extra: dict) -> dict:
    """递归合并 extra 到 base（extra 优先，dict 级深合并）。"""
    for k, v in extra.items():
        if k in base and isinstance(base[k], dict) and isinstance(v, dict):
            _deep_merge(base[k], v)
        else:
            base[k] = v
    return base


def _norm_col(c: str) -> str:
    """列名归一：去类型后缀(:int 等)/空白/小写。供 primary_key 列名比对。"""
    if not c:
        return ""
    return str(c).split(":")[0].strip().lower()


# ── 填表规则（Step1 prompt 注入）────────────────────────────────

def _load_fill_files() -> dict[str, str]:
    """扫描 rules/fill/*.md，返回 {stem: 文本}（"_global" 键存全局规则）。"""
    global _FILL_CACHE
    if _FILL_CACHE is not None:
        return _FILL_CACHE
    files: dict[str, str] = {}
    if not _FILL_DIR.exists():
        _FILL_CACHE = files
        return files
    try:
        for p in sorted(_FILL_DIR.glob("*.md")):
            text = _read_md(p)
            if text:
                files[p.stem] = text
    except Exception:
        logger.warning("扫描 fill 规则目录失败", exc_info=True)
    _FILL_CACHE = files
    return files


def load_fill_rules(stems: Optional[list[str]] = None) -> str:
    """构建填表规则文本块，注入 Step1 解析 prompt。

    stems: 命中候选表 stem 列表（如 ["item", "interaction"]），
           只加载 _global.md + 这些 stem 对应的 <stem>.md；
           空/None 时只加载 _global.md（避免无关表规则撑爆 prompt）。
    """
    files = _load_fill_files()
    if not files:
        return ""
    wanted = set(stems or [])
    blocks: list[str] = []
    g = files.get("_global")
    if g:
        blocks.append(g)
    for stem in sorted(wanted):
        if stem in files:
            blocks.append(files[stem])
    if not blocks:
        return ""
    body = "\n\n".join(blocks)
    return f"## 填表规则（强约束，必须遵守）\n{body}"


# ── 校验规则（Step2 强约束）─────────────────────────────────────

def load_validate_rules() -> dict:
    """加载全部校验规则，返回合并后的结构化约束：

        {table_stem: {sheet: {"columns": {col: {type, required, enum,
         min, max, unique, regex}}}}}

    md 内嵌 yaml 块统一为 `tables:` 根节点（与 value_constraints.yaml 同构）。
    多文件/多块按 (stem, sheet, col) 深合并，后加载覆盖先加载。
    """
    global _VALIDATE_CACHE
    if _VALIDATE_CACHE is not None:
        return _VALIDATE_CACHE
    merged: dict = {}
    if not _VALIDATE_DIR.exists() or not _HAS_YAML:
        _VALIDATE_CACHE = merged
        return merged
    try:
        for p in sorted(_VALIDATE_DIR.glob("*.md")):
            text = _read_md(p)
            if not text:
                continue
            for block in _extract_yaml_blocks(text):
                tables = block.get("tables")
                if isinstance(tables, dict):
                    _deep_merge(merged, tables)
    except Exception:
        logger.warning("加载 validate 规则失败", exc_info=True)
    _VALIDATE_CACHE = merged
    return merged


def get_value_constraints_overlay() -> dict:
    """校验规则中可并入 value_constraints 的列约束（type/min/max/unique/regex）。

    返回结构同 _load_value_constraints：{stem: {sheet: {"columns": {col: {...}}}}}。
    剔除 required/enum 两个字段（它们走独立校验通道），保留纯值约束。
    """
    rules = load_validate_rules()
    out: dict = {}
    for stem, sheets in rules.items():
        if not isinstance(sheets, dict):
            continue
        for sheet, cfg in sheets.items():
            cols = cfg.get("columns") if isinstance(cfg, dict) else None
            if not isinstance(cols, dict):
                continue
            for col, meta in cols.items():
                if not isinstance(meta, dict):
                    continue
                kept = {k: v for k, v in meta.items()
                        if k in ("type", "min", "max", "unique", "regex")}
                if kept:
                    out.setdefault(stem, {}).setdefault(sheet, {}) \
                       .setdefault("columns", {})[col] = kept
    return out


def get_required_fields_overlay() -> dict:
    """校验规则中 required:true 的列 → {stem: {sheet: [col, ...]}}。"""
    rules = load_validate_rules()
    out: dict = {}
    for stem, sheets in rules.items():
        if not isinstance(sheets, dict):
            continue
        for sheet, cfg in sheets.items():
            cols = cfg.get("columns") if isinstance(cfg, dict) else None
            if not isinstance(cols, dict):
                continue
            req = [c for c, m in cols.items()
                   if isinstance(m, dict) and m.get("required")]
            if req:
                out.setdefault(stem, {})[sheet] = req
    return out


def get_enum_overlay() -> dict:
    """校验规则中 enum:[...] 列 -> {stem: {sheet: {col_lower: set(values)}}}。

    供 validate_field_layer 的 enum_set 白名单检查用（val 必须 ∈ set）。
    """
    rules = load_validate_rules()
    out: dict = {}
    for stem, sheets in rules.items():
        if not isinstance(sheets, dict):
            continue
        for sheet, cfg in sheets.items():
            cols = cfg.get("columns") if isinstance(cfg, dict) else None
            if not isinstance(cols, dict):
                continue
            for col, meta in cols.items():
                if not isinstance(meta, dict):
                    continue
                vals = meta.get("enum")
                if isinstance(vals, (list, tuple, set)) and vals:
                    col_lower = (col or "").split(":")[0].strip().lower()
                    out.setdefault(stem, {}).setdefault(sheet, {})[col_lower] = set(vals)
    return out


def get_primary_key_overlay() -> dict:
    """校验规则中 sheet 级 ``primary_key`` 声明 -> {stem: {sheet: [col1, col2, ...]}}。

    支持复合主键：在 sheet 级（与 ``columns`` 同层）声明 ``primary_key: [列1, 列2]``，
    Step2/Step3 的唯一性/冲突检测按"组合值"判定而非单列，避免误把同一实体的
    多个等级行（如 fabao.FabaoLevel 的 (法宝id, 法宝等级)）判成 PK 冲突。

    返回结构：
        {stem_lower: {sheet_name: ["col1", "col2"]}}
    - 列名保留 yaml 原写法（中文表头/英文规范名），由消费者按 _norm_col 比对。
    - 单元素 ``primary_key: [id]`` 等价于旧列级 ``unique: true``，两条路收敛。
    - ``*`` 通配（``"*"`` 表/sheet）照常解析，由消费者按通配规则应用。
    """
    rules = load_validate_rules()
    out: dict = {}
    for stem, sheets in rules.items():
        if not isinstance(sheets, dict):
            continue
        for sheet, cfg in sheets.items():
            if not isinstance(cfg, dict):
                continue
            pk = cfg.get("primary_key")
            if isinstance(pk, str):
                pk = [pk]
            if not isinstance(pk, (list, tuple)) or not pk:
                continue
            # 去空/去类型后缀但保留大小写（消费者统一 _norm_col 比对）
            cols = [str(c).split(":")[0].strip() for c in pk
                    if c is not None and str(c).strip()]
            if cols:
                out.setdefault(str(stem).lower(), {})[str(sheet)] = cols
    return out


def reset_cache() -> None:
    """丢弃缓存（规则文件热更新后调用）。"""
    global _VALIDATE_CACHE, _FILL_CACHE
    _VALIDATE_CACHE = None
    _FILL_CACHE = None


__all__ = [
    "_norm_col",
    "load_fill_rules",
    "load_validate_rules",
    "get_value_constraints_overlay",
    "get_required_fields_overlay",
    "get_enum_overlay",
    "get_primary_key_overlay",
    "reset_cache",
]

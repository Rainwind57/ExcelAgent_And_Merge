"""Excel 多版本智能合并引擎：读取 merge_strategies.yaml，按列策略自动合并冲突/变更。

策略说明（见 merge_strategies.yaml）：
  base_priority  - 优先保留基准版本值（ID/外键列）
  take_newer     - 取首个非基准版本值
  take_longest   - 取最长字符串值（文本列）
  take_max       - 取最大数值
  range_check    - 衍生值在 [0,100] 范围内则取衍生，否则取基准
  manual         - 跳过，保持冲突待人工解决

引擎对每个 conflict/changed 的单元格查列策略并应用，返回自动合并结果与剩余人工项。
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from engine.compare import _semantic_key

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# server/agent/excel/skills/L1_derived/merge_strategies.yaml
# R25-fix: 之前少写了 L1_derived 这一层目录，导致这个文件从来没被真正加载到
# （_load_strategies 靠 mtime 判断文件是否存在，路径错了直接判"不存在"返回空
# dict，_get_column_strategy 永远走 default='manual'），外键列识别
# （strategy=='base_priority'）在 ref_integrity.py::_load_fk_columns 里因此
# 永远拿不到任何外键候选列，"ID 重映射后同步外键引用"这条链路实际从未生效过。
# 对照同文件里 value_constraints.yaml（下面一行）的正确写法改的。
_SKILL_PATH = Path(__file__).resolve().parent.parent / "agent" / "excel" / "skills" / "L1_derived" / "merge_strategies.yaml"
# value_constraints.yaml：列类型约束（int/float/bool/string…）
_VC_PATH = Path(__file__).resolve().parent.parent / "agent" / "excel" / "skills" / "L1_derived" / "value_constraints.yaml"

_cache: dict = {}
_vc_cache: dict = {}


def _deep_merge_tables(base: dict, extra: dict) -> dict:
    """递归深合并 extra 到 base（extra 优先，dict 级深合并，list 整值替换）。

    与 agent.core.agent._deep_merge_tables 同语义，engine 层独立实现避免跨层 import。
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
    overlay 来自 rules_loader.get_value_constraints_overlay()（rules/validate/*.md，
    与 agent.core.agent._load_value_constraints 读的是同一份用户规则源），engine 层
    独立合并与缓存（mtime 校验 L1 派生文件），不 import agent.py 本体，避免跨层耦合。
    required/enum 不在此校验范围内（走 Agent 侧 Step2 独立通道，Merge 引擎不做业务
    语义裁决，只做"改动后的值有没有明显违反已知约束"的兜底）。
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
        data = yaml.safe_load(_VC_PATH.read_text(encoding="utf-8")) or {}
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
    """查列完整约束配置（type/min/max/unique/regex）。未命中返回空 dict（不校验）。

    数据来自 L1 自动派生 + rules/validate 用户规则深合并（见 _load_value_constraints）。
    """
    header = (col_header or "").split(":")[0].strip()
    if not header:
        return {}
    vc = _load_value_constraints()
    sheets = vc.get(table_stem, {})
    cols = sheets.get(sheet, {}).get("columns", {})
    info = cols.get(header) or cols.get(col_header)
    return info or {}


def _get_col_type(table_stem: str, sheet: str, col_header: str) -> str:
    """查列类型标注（int/float/bool/string/…）。未命中返回空串（不校验）。"""
    return (_get_col_constraints(table_stem, sheet, col_header).get("type", "") or "").strip().lower()


def _check_value_type(col_type: str, value) -> tuple[bool, str]:
    """轻量类型校验（int/float/bool）。空 col_type 视为通过（无约束）。

    与 agent.core.agent._check_type_constraint 同语义，engine 层独立实现避免跨层 import。
    string 类型不做强校验（数值也能存为文本），仅数值类型强校验。
    """
    t = (col_type or "").strip().lower()
    if not t:
        return True, ""
    if t in ("int", "integer", "long"):
        try:
            int(float(str(value)))
            return True, ""
        except (ValueError, TypeError):
            return False, f"int 类型不符：{value!r}"
    if t in ("float", "double", "number"):
        try:
            float(str(value))
            return True, ""
        except (ValueError, TypeError):
            return False, f"float 类型不符：{value!r}"
    if t in ("bool", "boolean"):
        if isinstance(value, bool) or str(value).strip().lower() in ("0", "1", "true", "false", "是", "否"):
            return True, ""
        return False, f"bool 类型不符：{value!r}"
    return True, ""


def _check_value_constraints(col_cfg: dict, value) -> tuple[bool, str]:
    """完整列约束校验：type + min + max + regex（来自 rules/validate overlay 深合并）。

    在 _check_value_type 基础上补上 rules/validate 才有的 min/max（数值范围）和
    regex（格式正则）两项，让 Merge 自动合并也能吃到用户在 rules/validate/*.md 里
    显式声明的取值约束，不再局限于 L1 自动派生的纯类型信息。
    unique 需要整列扫描，单值校验做不了，沿用旧行为交人工判断；required/enum 走
    Agent 侧 Step2 独立通道，engine 层不做业务语义裁决。
    """
    if not isinstance(col_cfg, dict) or not col_cfg:
        return True, ""
    type_ok, type_reason = _check_value_type(col_cfg.get("type", ""), value)
    if not type_ok:
        return False, type_reason
    if value is None:
        return True, ""
    vmin, vmax = col_cfg.get("min"), col_cfg.get("max")
    if vmin is not None or vmax is not None:
        try:
            fv = float(str(value))
            if vmin is not None and fv < float(vmin):
                return False, f"值 {value!r} 小于规则允许的最小值 {vmin}"
            if vmax is not None and fv > float(vmax):
                return False, f"值 {value!r} 大于规则允许的最大值 {vmax}"
        except (ValueError, TypeError):
            pass  # 非数值时 min/max 不适用，已由 type 校验兜底
    pattern = col_cfg.get("regex")
    if pattern:
        try:
            import re as _re
            if not _re.search(str(pattern), str(value)):
                return False, f"值 {value!r} 不匹配规则要求的格式 {pattern!r}"
        except Exception:
            pass
    return True, ""


def _load_strategies() -> dict:
    """加载并缓存 merge_strategies.yaml。文件缺失或无 yaml 时返回空 dict 降级。

    M18: 缓存加 mtime 校验，yaml 热更新后运行中服务能感知新配置，避免永不过期。
    """
    try:
        cur_mtime = _SKILL_PATH.stat().st_mtime if _SKILL_PATH.exists() else 0.0
    except OSError:
        cur_mtime = 0.0
    if _cache.get("mtime") == cur_mtime and "data" in _cache:
        return _cache["data"]
    if not _HAS_YAML or not cur_mtime:
        _cache["data"] = {}
        _cache["mtime"] = cur_mtime
        return {}
    data = yaml.safe_load(_SKILL_PATH.read_text(encoding="utf-8")) or {}
    _cache["data"] = data
    _cache["mtime"] = cur_mtime
    return data


def _get_column_strategy(table_stem: str, sheet: str, col_header: str) -> Tuple[str, str]:
    """查 table_stem + sheet + 列名 → (strategy, reason)。

    查找顺序：精确表 → 通配表 "*"；表内精确 sheet → 通配 sheet "*"；
    列名先精确匹配，失败则做子串包含匹配（双向）。全未命中返回 (default_strategy, '').
    """
    data = _load_strategies()
    default = data.get("default_strategy", "manual")
    tables = data.get("tables", {})

    # 精确表优先，回退通配表
    tbl = tables.get(table_stem) or tables.get("*") or {}
    # 精确 sheet 优先，回退通配 sheet
    sheet_cfg = tbl.get(sheet) or tbl.get("*") or {}

    col_header = (col_header or "").strip()
    # 精确列名
    if col_header and col_header in sheet_cfg:
        c = sheet_cfg[col_header]
        return c.get("strategy", default), c.get("reason", "")
    # 子串包含匹配（双向），取最长 key 命中以避免短串误匹配
    if col_header:
        best_key = None
        for key in sheet_cfg.keys():
            if not key:
                continue
            if key in col_header or col_header in key:
                if best_key is None or len(key) > len(best_key):
                    best_key = key
        if best_key is not None:
            c = sheet_cfg[best_key]
            return c.get("strategy", default), c.get("reason", "")
    return default, ""


def _val_str(v: Any) -> str:
    return str(v) if v is not None else ""


def _apply_strategy(
    strategy: str,
    versions: Dict[str, Any],
    base_name: str,
    other_files: List[str],
) -> Optional[Any]:
    """应用单列策略，返回合并后的值。返回 None 表示该列需人工解决（manual 或未知策略）。"""
    if strategy == "manual":
        return None
    if strategy == "base_priority":
        return versions.get(base_name)
    if strategy == "take_newer":
        # 取首个非空衍生版本值；都无则取基准
        for fn in other_files:
            v = versions.get(fn)
            if v is not None:
                return v
        return versions.get(base_name)
    if strategy == "take_longest":
        best = None
        best_len = -1
        for v in versions.values():
            s = _val_str(v)
            if len(s) > best_len:
                best_len = len(s)
                best = v
        return best
    if strategy == "take_max":
        best = None
        best_num: Optional[float] = None
        for v in versions.values():
            try:
                num = float(v) if v is not None else None
            except (ValueError, TypeError):
                num = None
            if num is not None and (best_num is None or num > best_num):
                best_num = num
                best = v
        return best if best is not None else versions.get(base_name)
    if strategy == "range_check":
        # 衍生值落在 [0,100] 取衍生，否则保留基准
        for fn in other_files:
            v = versions.get(fn)
            try:
                num = float(v) if v is not None else None
            except (ValueError, TypeError):
                num = None
            if num is not None and 0 <= num <= 100:
                return v
        return versions.get(base_name)
    # 未知策略 → 人工
    return None


def auto_merge_sheet(
    table_stem: str,
    sheet_name: str,
    headers: List[str],
    rows: List[dict],
    base_name: str,
    all_files: List[str],
) -> dict:
    """对单个 sheet 执行智能合并。

    遍历所有 conflict/changed 单元格，按列策略自动合并。
    返回:
      {
        auto_merged: [{ri, ci, value, strategy, reason}],
        manual_left: [{ri, ci, strategy, reason, col_header}],
        stats: {auto_merged, manual_left}
      }
    """
    other_files = [f for f in all_files if f != base_name]
    auto_merged: List[dict] = []
    manual_left: List[dict] = []

    for ri, row in enumerate(rows):
        cells = row.get("cells", []) if isinstance(row, dict) else getattr(row, "cells", [])
        for ci, cell in enumerate(cells):
            # 兼容 dict 与 CellData 对象
            if isinstance(cell, dict):
                is_diff = cell.get("conflict") or cell.get("changed")
                versions = cell.get("versions", {})
            else:
                is_diff = getattr(cell, "conflict", False) or getattr(cell, "changed", False)
                versions = getattr(cell, "versions", {})
            if not is_diff:
                continue

            col_header = headers[ci] if ci < len(headers) else ""
            strategy, reason = _get_column_strategy(table_stem, sheet_name, str(col_header) if col_header else "")
            new_val = _apply_strategy(strategy, versions, base_name, other_files)

            # 约束校验：value_constraints.yaml（L1 派生 type）+ rules/validate 用户
            # 规则（min/max/regex）深合并后的列约束，自动采纳值不符时降级为人工
            # （避免把等级列 int 写成 string、把数值列写成超范围值等"看似合理但
            # 违约束"的自动合并）。实际用户改动一般符合约束，此处只兜底异常分支。
            col_cfg = _get_col_constraints(table_stem, sheet_name, str(col_header) if col_header else "")
            type_ok, type_reason = _check_value_constraints(col_cfg, new_val) if new_val is not None else (True, "")

            if new_val is None or not type_ok:
                manual_left.append({
                    "ri": ri, "ci": ci,
                    "strategy": "manual" if new_val is None else strategy,
                    "reason": (reason or "未配置自动策略") if new_val is None
                              else f"{reason or '自动策略'}；约束校验未通过：{type_reason}",
                    "col_header": col_header,
                    "type_violation": not type_ok,
                })
            else:
                auto_merged.append({
                    "ri": ri, "ci": ci,
                    "value": new_val,
                    "strategy": strategy,
                    "reason": reason,
                })

    return {
        "auto_merged": auto_merged,
        "manual_left": manual_left,
        "stats": {"auto_merged": len(auto_merged), "manual_left": len(manual_left)},
    }


def recommend_version(
    table_stem: str,
    sheet: str,
    col_header: str,
    versions: Dict[str, Any],
    base_name: str,
    all_files: List[str],
) -> Optional[dict]:
    """对单个冲突单元格给出推荐版本（基于列策略 + 多数表决启发式）。

    优先按列策略推荐；manual/未配置时退化为多数表决（多数版本同值则推荐）；
    全不同时保守推荐基准。返回 {version, reason, strategy} 或 None。
    非强制，用于前端"⭐推荐"提示，用户仍可自选。未来 AI 基座接入后替换此逻辑即可。
    """
    from collections import Counter
    other_files = [f for f in all_files if f != base_name]
    strategy, reason = _get_column_strategy(table_stem, sheet, col_header)

    if strategy == "base_priority":
        return {"version": base_name, "reason": reason or "ID 列，建议保留基准值", "strategy": strategy}
    if strategy == "take_newer":
        for fn in other_files:
            v = versions.get(fn)
            if v is not None:
                return {"version": fn, "reason": reason or "数值列，建议取最新版本", "strategy": strategy}
        return {"version": base_name, "reason": "衍生版本无值，保留基准", "strategy": strategy}
    if strategy == "take_longest":
        best_fn, best_len = None, -1
        for fn, v in versions.items():
            l = len(_val_str(v))
            if l > best_len:
                best_len = l
                best_fn = fn
        if best_fn:
            return {"version": best_fn, "reason": reason or "文本列，建议取最完整内容", "strategy": strategy}
    if strategy == "take_max":
        best_fn, best_num = None, None
        for fn, v in versions.items():
            try:
                num = float(v) if v is not None else None
            except (ValueError, TypeError):
                num = None
            if num is not None and (best_num is None or num > best_num):
                best_num = num
                best_fn = fn
        if best_fn:
            return {"version": best_fn, "reason": reason or "数值列，建议取最大值", "strategy": strategy}
    if strategy == "range_check":
        for fn in other_files:
            v = versions.get(fn)
            try:
                num = float(v) if v is not None else None
            except (ValueError, TypeError):
                num = None
            if num is not None and 0 <= num <= 100:
                return {"version": fn, "reason": reason or "数值在合理范围，建议取该版本", "strategy": strategy}
        return {"version": base_name, "reason": "衍生值超范围，建议保留基准", "strategy": strategy}

    # manual / 未配置：多数表决启发式
    # #24: 多数表决用语义归一 key（与 compare 三方判定同尺），使 "100"/100/"1e2"
    # 计入同一票，避免 recommend 把语义等值组误判为"全不同"回退基准。
    val_counts: Counter = Counter()
    val_to_fn: dict = {}
    for fn, v in versions.items():
        k = _semantic_key(v)
        val_counts[k] += 1
        if k not in val_to_fn:
            val_to_fn[k] = fn
    most_common_val, count = val_counts.most_common(1)[0]
    total = len(versions)
    if count >= 2:
        if _semantic_key(versions.get(base_name)) == most_common_val:
            return {"version": base_name, "reason": f"多数版本（{count}/{total}）与基准一致，建议保留基准", "strategy": "majority_vote"}
        rec_fn = val_to_fn[most_common_val]
        return {"version": rec_fn, "reason": f"多数版本（{count}/{total}）取此值", "strategy": "majority_vote"}

    # 全不同：保守推荐基准
    return {"version": base_name, "reason": "各版本值均不同，建议优先评估基准是否仍合理", "strategy": "fallback_base"}


def recommend_sheet(
    table_stem: str,
    sheet_name: str,
    headers: List[str],
    rows: List[dict],
    base_name: str,
    all_files: List[str],
) -> dict:
    """对单个 sheet 的所有冲突/变更单元格生成推荐。

    返回 { recommendations: [{ri, ci, version, reason, strategy}], stats: {total} }
    """
    recs: List[dict] = []
    for ri, row in enumerate(rows):
        cells = row.get("cells", []) if isinstance(row, dict) else getattr(row, "cells", [])
        for ci, cell in enumerate(cells):
            if isinstance(cell, dict):
                is_diff = cell.get("conflict") or cell.get("changed")
                versions = cell.get("versions", {})
            else:
                is_diff = getattr(cell, "conflict", False) or getattr(cell, "changed", False)
                versions = getattr(cell, "versions", {})
            if not is_diff:
                continue
            col_header = headers[ci] if ci < len(headers) else ""
            rec = recommend_version(
                table_stem, sheet_name, str(col_header) if col_header else "",
                versions, base_name, all_files,
            )
            if rec:
                recs.append({"ri": ri, "ci": ci, **rec})
    return {"recommendations": recs, "stats": {"total": len(recs)}}


def resolve_and_validate_sheet(
    table_stem: str,
    sheet_name: str,
    headers: List[str],
    rows: List[Any],
    base_name: str,
    all_files: List[str],
    cross_sheet_pks: Dict[str, set] = None,
    conflict_mode: str = "split",
) -> dict:
    """对单 sheet 执行：ID 冲突重映射 → 自动合并 → 引用完整性校验。

    conflict_mode 控制多分支同主键新增行的处理：
      "split"（默认）：视为独立新行，先到先得重映射主键
      "conflict"：视为同一行冲突，保留合并行交人工裁决

    返回 auto_merge 结果 + 重映射后的行 + ID 重映射报告 + 引用校验报告。
    """
    from .id_resolver import resolve_id_conflicts
    from .ref_integrity import validate_sheet_references

    # 1. ID 冲突重映射（含错误合并行拆分 / 主键冲突标记）
    id_res = resolve_id_conflicts(rows, headers, base_name, all_files, mode=conflict_mode)
    resolved_rows = id_res["resolved_rows"]

    # 2. 自动合并（基于重映射后的行；conflict 模式下未决主键冲突行会进入 manual_left）
    auto_res = auto_merge_sheet(table_stem, sheet_name, headers, resolved_rows, base_name, all_files)

    # 3. 引用完整性校验（消费 id_mapping 同步外键值，检测悬空）
    ref_res = validate_sheet_references(
        resolved_rows, headers, table_stem, sheet_name,
        id_res["id_mapping"], cross_sheet_pks or {},
    )

    return {
        "auto_merged": auto_res["auto_merged"],
        "manual_left": auto_res["manual_left"],
        "resolved_rows": resolved_rows,
        "id_resolution": {
            "id_mapping": id_res["id_mapping"],
            "conflicts": id_res["conflicts"],
            "pk_conflicts": id_res["pk_conflicts"],
            "stats": id_res["stats"],
        },
        "ref_integrity": ref_res,
        "stats": {
            **auto_res["stats"],
            "rows_split": id_res["stats"]["rows_split"],
            "ids_remapped": id_res["stats"]["ids_remapped"],
            "pk_conflicts": id_res["stats"]["pk_conflicts"],
            "remapped_refs": ref_res["remapped_refs"],
            "dangling_refs": len(ref_res["dangling"]),
        },
    }

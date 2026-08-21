"""值语义合理性门（Level 0）：写后基于列历史分布 + 字段语义 + 枚举白名单检测离群值。

设计动机：
    _verify_write 现有规则校验只做类型强转 + id_scope + anti_pattern + ref_integrity，
    不做"值在该列语义分布里是否合理"的判断。take_max 类策略会采纳"攻击力 100→10000"
    因为它更大；value_constraints.yaml 的 min/max 写死 [0,100] 换张表就失效。

    本模块作为 verify-repair Level 0（在 Level 1 规则校验之前），用列历史分布
    自适应判定离群，无需人工维护每列范围。纯代码、零 LLM（快路径）；LLM 诊断在
    repair Level 2。

检测维度：
    1. 硬编码范围：value_constraints.yaml 的 min/max（_verify_write 未用，此处补）
    2. 数值离群：读列历史值（排除本次新写值）算 median + MAD，新值极端偏离判离群
    3. 枚举白名单：enum_mappings.yaml 查 int 列枚举，新值不在白名单判离群

保守策略：仅标记极端离群（10x median 或超绝对阈值），避免误报腐蚀合法值。
    修复用 suggested_fix（clamp 到边界或 median），coerce 到 median 仅在极端离群时
    才触发（合法值不会命中）。

返回：semantic_issues [{column, value, reason, severity, suggested_fix}]
    severity="error" → 进 repair（VerifyResult.failed_kind=SEMANTIC_OUTLIER）
    severity="warn"  → 记录但不阻断
"""
from __future__ import annotations

import logging
import statistics
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 模块级分布缓存：(table_stem, sheet, mtime) → {col_name: ColStats}
# mtime 变化即失效（写操作后文件改了，下次重算）。避免每次写都读全表。
_DIST_CACHE: dict[tuple[str, str, float], dict[str, "ColStats"]] = {}

# 离群判定阈值（保守，减少误报）
_MIN_HISTORY = 5          # 历史值少于此数不判分布离群（样本不足）
_MAD_K = 20.0             # |x - median| > k * MAD 判离群（保守，20x 典型偏差）
_EXTREME_HIGH_RATIO = 10.0  # 高值离群：x > median * ratio（10x 跳变才算异常）
_BIG_VALUE_ABS = 1000     # median==0 列：x 超此绝对值判离群


class ColStats:
    """单列数值分布统计（惰性计算、缓存）。"""

    __slots__ = ("values", "median", "mad", "p99", "count")

    def __init__(self, values: list[float]) -> None:
        self.values = values
        self.count = len(values)
        if self.count >= 2:
            self.median = statistics.median(values)
            abs_dev = [abs(v - self.median) for v in values]
            self.mad = statistics.median(abs_dev) if abs_dev else 0.0
            sv = sorted(values)
            n = len(sv)
            k = max(1, n // 20)
            self.p99 = sv[n - k]
        else:
            self.median = values[0] if values else 0.0
            self.mad = 0.0
            self.p99 = values[0] if values else 0.0


def _try_numeric(v: Any) -> Optional[float]:
    """尝试把值转 float。失败返回 None。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        for suf in ("%", "x", "倍"):
            if s.endswith(suf):
                try:
                    return float(s[:-len(suf)])
                except ValueError:
                    break
        return None


def _load_enum_values(table_stem: str, sheet: str, col_name: str) -> Optional[list]:
    """从 enum_mappings.yaml 读某列的合法枚举值列表。无则 None。"""
    try:
        from .skill_loader import _load_yaml
        em = _load_yaml("enum_mapping.yaml")
        if not isinstance(em, dict):
            return None
        sheet_em = em.get("tables", {}).get(table_stem, {}).get(sheet, {})
        if not isinstance(sheet_em, dict):
            return None
        col_em = sheet_em.get("columns", {}).get(col_name, {})
        if not isinstance(col_em, dict):
            return None
        vals = col_em.get("values", [])
        if not isinstance(vals, list) or not vals:
            return None
        out = []
        for v in vals:
            if isinstance(v, dict):
                ev = v.get("value")
                if ev is not None:
                    out.append(ev)
            else:
                out.append(v)
        return out if out else None
    except Exception:
        return None


def _get_col_stats(table_stem: str, sheet: str, path, cli,
                   headers_clean: list[str], col_name: str,
                   exclude_value: Optional[float] = None) -> Optional[ColStats]:
    """读全表取某列数值历史分布（带 mtime 缓存）。

    exclude_value：排除等于此值的样本（post-write 校验时排除本次新写值，避免污染分布）。
    注：缓存键不含 exclude_value（同列同 mtime 的分布基础一致），exclude 仅在本次收集时过滤。
    故缓存命时仍需按 exclude 过滤缓存里的 values 重算 stats——为简，缓存仅存基础 values，
    调用方按需过滤后现算 stats（样本通常 <1000，开销可忽略）。
    """
    try:
        p = Path(str(path))
        mtime = p.stat().st_mtime
    except Exception:
        mtime = 0.0
    key = (table_stem, sheet, mtime)
    cached = _DIST_CACHE.get(key)
    if cached is not None and col_name in cached:
        base_values = cached[col_name].values
    else:
        if cached is None:
            cached = {}
            _DIST_CACHE[key] = cached
        try:
            rows = cli.read_sheet(path, sheet) if cli else []
        except Exception:
            logger.debug("semantic_gate read_sheet 失败 %s/%s", table_stem, sheet, exc_info=True)
            return None
        if not rows or not headers_clean:
            return None
        try:
            col_idx = headers_clean.index(col_name)
        except ValueError:
            return None
        start = 1 if rows and rows[0] and col_idx < len(rows[0]) and str(rows[0][col_idx]).strip() == col_name else 0
        base_values: list[float] = []
        for r in rows[start:]:
            if not r or col_idx >= len(r):
                continue
            nv = _try_numeric(r[col_idx])
            if nv is not None:
                base_values.append(nv)
        if not base_values:
            return None
        cached[col_name] = ColStats(base_values)
    # 按 exclude_value 过滤后现算（小样本开销可忽略）
    filtered = [v for v in base_values if exclude_value is None or v != exclude_value]
    if not filtered:
        return None
    return ColStats(filtered)


def _check_hardcoded_range(col_meta: dict, num_val: float) -> Optional[str]:
    """value_constraints.yaml 的 min/max 硬范围检查。违例返回 reason。"""
    if not isinstance(col_meta, dict):
        return None
    mn = col_meta.get("min")
    mx = col_meta.get("max")
    try:
        if mn is not None and num_val < float(mn):
            return f"值 {num_val} 低于硬编码下限 {mn}"
        if mx is not None and num_val > float(mx):
            return f"值 {num_val} 超过硬编码上限 {mx}"
    except (TypeError, ValueError):
        pass
    return None


def _check_distribution_outlier(stats: ColStats, num_val: float) -> Optional[str]:
    """基于列历史分布判离群。保守：仅标记极端高值偏离。

    只检测高值离群（低值多为合法默认 0，不报）。用 median 倍数 + MAD 偏离双重确认，
    两者都满足才判常数列（MAD==0）用 median 倍数判定。
    """
    if stats.count < _MIN_HISTORY:
        return None
    med = stats.median
    mad = stats.mad
    if num_val <= med:
        return None  # 仅检测高值离群
    if mad > 0:
        # 非常数列：MAD 偏离 + median 倍数双重确认
        if (num_val - med) > _MAD_K * mad:
            ratio = num_val / med if med > 0 else num_val
            if ratio > _EXTREME_HIGH_RATIO:
                return f"值 {num_val} 远高于列历史分布（median={med}, MAD={mad}）"
    else:
        # 常数列（MAD==0）
        if med > 0:
            ratio = num_val / med
            if ratio > _EXTREME_HIGH_RATIO:
                return f"值 {num_val} 远超列历史常数（median={med}）"
        else:
            # median==0 列：绝对量级
            if num_val > _BIG_VALUE_ABS:
                return f"值 {num_val} 远超列历史分布（列默认 0）"
    return None


def _check_enum_whitelist(table_stem: str, sheet: str, col_name: str,
                          new_value: Any) -> Optional[tuple[str, Any]]:
    """枚举白名单检查。违例返回 (reason, suggested_fix)。"""
    enum_vals = _load_enum_values(table_stem, sheet, col_name)
    if not enum_vals:
        return None
    nv = _try_numeric(new_value)
    candidates = []
    for ev in enum_vals:
        evn = _try_numeric(ev)
        if evn is not None and nv is not None and evn == nv:
            return None  # 命中白名单
        candidates.append((ev, evn))
    suggested = None
    if nv is not None:
        best_diff = None
        for ev, evn in candidates:
            if evn is None:
                continue
            d = abs(evn - nv)
            if best_diff is None or d < best_diff:
                best_diff = d
                suggested = ev
    return (f"值 {new_value} 不在枚举白名单 {enum_vals[:8]}（共 {len(enum_vals)} 项）", suggested)


def run_semantic_gate(
    table_stem: str,
    sheet: str,
    path,
    headers_clean: list[str],
    result_rows: list[dict],
    cli,
    vc: dict,
    action: str = "",
) -> list[dict]:
    """值语义合理性门主入口。返回 semantic_issues 列表。

    参数:
        table_stem: 表名 stem（如 "pet"）
        sheet: sheet 名
        path: 文件路径（cli.read_sheet 用）
        headers_clean: 清洗后列名列表（去 :后缀）
        result_rows: _verify_write 的 result_rows [{col_name, new_value, col, ...}]
        cli: RealCodeMakerCLI 实例
        vc: value_constraints columns dict {col_name: {type, min, max, unique}}
        action: 写操作类型（add/set/modify）。add 时跳过分布离群检测——
            新增行引入的新值（ID/编号）本就偏离历史分布，分布离群对 add 无意义
            且易误判合法新 ID（如 spell_id 700010 在 median=0 列触发 _BIG_VALUE_ABS）。
            硬范围 + 枚举白名单仍保留（这两类对 add 仍是有效约束）。

    返回: [{column, value, reason, severity, suggested_fix}]
        severity="error" 触发 repair；"warn" 仅记录。
        纯代码、零 LLM、防御式（任何异常返回空列表不阻断）。
    """
    issues: list[dict] = []
    if not result_rows or not headers_clean:
        return issues
    try:
        for rr in result_rows:
            col_name = (rr.get("col_name") or "").split(":")[0]
            new_val = rr.get("new_value")
            if not col_name or new_val is None:
                continue
            col_meta = vc.get(col_name, {}) if isinstance(vc, dict) else {}
            col_type = (col_meta.get("type") or "").strip().lower() if isinstance(col_meta, dict) else ""

            # 1. 硬编码范围（min/max）
            num_val = _try_numeric(new_val)
            if num_val is not None:
                reason = _check_hardcoded_range(col_meta, num_val)
                if reason:
                    mn = col_meta.get("min")
                    mx = col_meta.get("max")
                    fix = None
                    try:
                        if mn is not None and num_val < float(mn):
                            fix = float(mn)
                        elif mx is not None and num_val > float(mx):
                            fix = float(mx)
                    except (TypeError, ValueError):
                        pass
                    issues.append({
                        "column": col_name, "value": new_val,
                        "reason": reason, "severity": "error",
                        "suggested_fix": fix,
                    })
                    continue  # 硬范围违例已记，不再叠判

                # 2. 分布离群（仅数值列，排除本次新写值避免污染）
                # #21 R5：add 操作跳过分布离群——新增行的新值（ID/编号）本就偏离历史
                # 分布，median=0 列的 _BIG_VALUE_ABS 会误判合法新 ID（700010/22199）。
                # set/modify 仍检测（改已有行值到离群是数据错误信号）。
                if action != "add" and (
                    col_type in ("int", "integer", "long", "float", "double", "number") or num_val is not None
                ):
                    stats = _get_col_stats(table_stem, sheet, path, cli, headers_clean, col_name,
                                           exclude_value=num_val)
                    if stats is not None:
                        dreason = _check_distribution_outlier(stats, num_val)
                        if dreason:
                            issues.append({
                                "column": col_name, "value": new_val,
                                "reason": dreason, "severity": "error",
                                "suggested_fix": round(stats.median, 6) if stats.median != 0 else None,
                            })
                            continue

            # 3. 枚举白名单（int 列）
            if col_type in ("int", "integer", "long"):
                er = _check_enum_whitelist(table_stem, sheet, col_name, new_val)
                if er is not None:
                    reason, suggested = er
                    issues.append({
                        "column": col_name, "value": new_val,
                        "reason": reason, "severity": "error",
                        "suggested_fix": suggested,
                    })
                    continue
        return issues
    except Exception:
        logger.warning("semantic_gate 异常，降级放行（返回空 issues）", exc_info=True)
        return issues


def clear_cache() -> None:
    """清空分布缓存（测试用）。"""
    _DIST_CACHE.clear()

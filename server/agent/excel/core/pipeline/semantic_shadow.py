"""最小清单#5 + §影子评测：语义计划 shadow diff（纯函数，0 LLM）。

背景（docs §"使用影子评测灰度迁移"）：新 planner 上线前不直接替换主链路，而是
`compare(old_path, new_path)` + `record_diff(new_path)`，只执行 old、记录 new 与
old 的差异，累计达标后再切换。本模块提供**确定性 diff 工具**：把两组 intents
（旧主路径 vs 新 shadow 路径）按 (table, sheet, action) 归一比对，产结构化差异：
缺表 / 多表 / 字段键差 / action 差 / produces-consumes 链差。

纯函数、无 IO、无 LLM、不执行任何 intent。供 shadow 评测与灰度门控消费。
"""
from __future__ import annotations

import re
from typing import Any, Iterable

__all__ = ["intent_signature", "diff_intent_plans"]

_NORM_RE = re.compile(r"[\s_:\-./\\()\[\]{}（）【】]+")


def _norm(v: object) -> str:
    return _NORM_RE.sub("", str(v if v is not None else "").split(":")[0]).strip().lower()


def _get(it: Any, *keys: str) -> Any:
    for k in keys:
        v = it.get(k) if isinstance(it, dict) else getattr(it, k, None)
        if v not in (None, ""):
            return v
    return None


def _fields_of(it: Any) -> dict:
    f = None
    if isinstance(it, dict):
        f = it.get("fields") or (it.get("extras") or {}).get("fields")
    else:
        ex = getattr(it, "extras", None) or {}
        f = getattr(it, "fields", None) or ex.get("fields")
    return f if isinstance(f, dict) else {}


def intent_signature(it: Any) -> tuple:
    """(table, sheet, action) 归一签名——标识"是哪张表的哪种操作"。"""
    return (_norm(_get(it, "table_hint", "table")),
            _norm(_get(it, "sheet_hint", "sheet")),
            _norm(_get(it, "action")) or "add")


def diff_intent_plans(old_intents: Iterable[Any],
                      new_intents: Iterable[Any]) -> dict:
    """比对旧主路径与新 shadow 路径的 intents，产结构化差异。

    Returns:
        {
          "match": bool,                    # 是否完全一致（表集合 + 每表字段键集合）
          "missing_tables": [sig],          # 旧有、新缺（新 planner 漏拆）
          "extra_tables": [sig],            # 新有、旧无（新 planner 多产/幻觉）
          "field_diffs": [ {sig, only_old:[...], only_new:[...]} ],
          "summary": {old_count, new_count, common},
        }
        sig 以 "table/sheet/action" 字符串呈现，便于日志/断言。
    """
    def _index(intents):
        idx: dict[tuple, set] = {}
        for it in (intents or []):
            sig = intent_signature(it)
            keys = {_norm(k) for k in _fields_of(it).keys() if str(k).strip()}
            idx.setdefault(sig, set()).update(keys)
        return idx

    old_idx = _index(old_intents)
    new_idx = _index(new_intents)

    def _sig_str(sig): return "/".join(sig)

    missing = sorted(_sig_str(s) for s in (set(old_idx) - set(new_idx)))
    extra = sorted(_sig_str(s) for s in (set(new_idx) - set(old_idx)))
    field_diffs = []
    for sig in sorted(set(old_idx) & set(new_idx)):
        only_old = sorted(old_idx[sig] - new_idx[sig])
        only_new = sorted(new_idx[sig] - old_idx[sig])
        if only_old or only_new:
            field_diffs.append({
                "sig": _sig_str(sig),
                "only_old": only_old,
                "only_new": only_new,
            })
    match = not missing and not extra and not field_diffs
    return {
        "match": match,
        "missing_tables": missing,
        "extra_tables": extra,
        "field_diffs": field_diffs,
        "summary": {
            "old_count": len(old_idx),
            "new_count": len(new_idx),
            "common": len(set(old_idx) & set(new_idx)),
        },
    }

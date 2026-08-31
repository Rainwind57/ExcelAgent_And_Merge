"""候选表通用 scorer（路线图 §3/§9.4，0 LLM、不绑业务词）。

把候选表排序从「只看定位器置信度」升级为「动作邻近度加权」。核心信号：

  动词邻近度（§3.1）：新增/配置/绑定/修改/删除/创建/刷新等动作词附近出现的
  实体名词（候选表 matched_term / stem / sheet）比远距离 alias 更有判别力。
  落在动作词窗口内的候选表获得小幅加权。

设计约束（防回归）：
  - 只对弱置信段（conf < 0.80）生效：alias 强命中（0.9）/ substring（0.85）等
    规则确定性命中不受影响，保证既有排序契约不漂移。
  - 加权上限 0.06，绝不跨过 0.80 强命中分界线。
  - 纯函数、0 LLM、无业务词特判（动作词为通用配表动作白名单）。

与既有机制的边界：
  - 列名通用性/跨表歧义列抑制（_GENERIC_COLS / _ambig_cols）在 LocatorAgent
    收候选阶段已做，本模块不重复。
  - FK 闭包层级（fk_inferred 0.60 / fk_expanded 0.50/0.40）已体现「远端降权」，
    本模块只补「动作主语邻近」这一缺失维度。
"""

from __future__ import annotations

import re

# 通用配表动作词（不绑业务表/测例）。配/建/造 等单字词仅在与实体名同现时才有
# 判别力，故单独按「动作+实体」组合匹配，避免误伤。
_ACTION_VERBS = [
    "新增", "增加", "添加", "创建", "生成", "建立",
    "配置", "绑定", "绑到", "关联", "引用", "指向",
    "修改", "改成", "改为", "设置", "设为",
    "删除", "去掉", "移除", "清除",
    "查询", "查看", "刷新", "刷",
]

# 邻近窗口（动作词前后各 N 字符内视为「附近」）
_WINDOW_BEFORE = 8
_WINDOW_AFTER = 14

# 加权上限：不跨 0.80 强命中分界线
_MAX_BOOST = 0.06
_STRONG_BAND = 0.80


def _term_variants(cand) -> list[str]:
    """候选表的可匹配词面：matched_term / sheet / stem（去空去重）。"""
    out: list[str] = []
    for v in (
        getattr(cand, "matched_term", ""),
        getattr(cand, "sheet", ""),
        getattr(cand, "stem", ""),
    ):
        s = str(v or "").strip()
        if s and s not in out:
            out.append(s)
    return out


def verb_proximity_hits(text: str, cand) -> int:
    """候选表词面落在动作词邻近窗口内的次数（§3.1 动词邻近度）。"""
    if not text:
        return 0
    terms = _term_variants(cand)
    if not terms:
        return 0
    hits = 0
    for vb in _ACTION_VERBS:
        for m in re.finditer(re.escape(vb), text):
            lo = max(0, m.start() - _WINDOW_BEFORE)
            hi = min(len(text), m.end() + _WINDOW_AFTER)
            window = text[lo:hi]
            if any(t and t in window for t in terms):
                hits += 1
    return hits


def rescore_candidates(text: str, candidates: list) -> list:
    """按动词邻近度对弱置信段候选做小幅加权，返回新列表（保序重排）。

    强命中（conf >= 0.80）原样保留；弱命中按邻近命中次数加成
    （min(0.06, 0.02 * hits)），仍封顶 0.79 不跨强命中分界线。
    排序稳定：同置信度保持原顺序（原置信度先降序，再按命中次数降序）。
    """
    if not candidates:
        return list(candidates)

    def _adjust(c):
        conf = float(getattr(c, "confidence", 0.0) or 0.0)
        if conf >= _STRONG_BAND:
            return conf, 0
        hits = verb_proximity_hits(text, c)
        boost = min(_MAX_BOOST, 0.02 * hits)
        return min(_STRONG_BAND - 0.01, conf + boost), hits

    # 保留原对象，只重新排序（confidence 就地微调，弱段内）
    decorated = [(c, *_adjust(c)) for c in candidates]
    for c, new_conf, _ in decorated:
        c.confidence = round(new_conf, 4)
    # 稳定排序：新置信度降序，同置信度按邻近命中降序，再保原序
    decorated.sort(key=lambda x: (-x[0].confidence, -x[2]))
    return [c for c, _, _ in decorated]


__all__ = ["rescore_candidates", "verb_proximity_hits"]

"""模糊匹配引擎（层2）：查询值无法精确命中时，在候选集合中返回最相似项。

匹配策略（加权融合）：
  1. 子串匹配：输入是候选的子串/超串/前缀，前缀权重最高
  2. 编辑距离：Levenshtein 相似比率 = 1 - dist / max(len)
  3. 字符重叠度：字符集合交并比（Jaccard on char set）

输出：按综合得分降序的候选列表，标注置信度（高/中/低），供用户确认后再操作。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

try:
    from rapidfuzz import fuzz as _rfz
    _HAS_RAPIDFUZZ = True
except ImportError:  # pragma: no cover
    _HAS_RAPIDFUZZ = False
    _rfz = None


def levenshtein(a: str, b: str) -> int:
    """Levenshtein 编辑距离（迭代实现，O(n*m) 时间 O(min(n,m)) 空间）。"""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    # 保证 a 是较短串，减少滚动数组宽度
    if len(a) > len(b):
        a, b = b, a
    prev = list(range(len(a) + 1))
    for i, cb in enumerate(b, start=1):
        cur = [i] + [0] * len(a)
        for j, ca in enumerate(a, start=1):
            cost = 0 if ca == cb else 1
            cur[j] = min(cur[j - 1] + 1, prev[j] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[-1]


def levenshtein_ratio(a: str, b: str) -> float:
    """编辑距离相似比率，范围 [0,1]，1 表示完全相同。"""
    if not a and not b:
        return 1.0
    m = max(len(a), len(b))
    if m == 0:
        return 1.0
    return 1.0 - levenshtein(a, b) / m


def char_overlap(a: str, b: str) -> float:
    """字符集合 Jaccard 重叠度：|A∩B| / |A∪B|，范围 [0,1]。"""
    sa, sb = set(a), set(b)
    if not sa and not sb:
        return 1.0
    union = sa | sb
    if not union:
        return 0.0
    return len(sa & sb) / len(union)


@dataclass
class FuzzyCandidate:
    """单个模糊匹配候选。

    Attributes:
        value: 候选原始值
        score: 综合得分 [0,1]
        confidence: 置信度档位 "high" | "medium" | "low"
        reasons: 命中的策略说明（如 ["prefix", "edit:0.83"]）
    """
    value: str
    score: float
    confidence: str
    reasons: list[str] = field(default_factory=list)


# 置信度阈值：score >= high → high，>= medium → medium，>= low → low，低于 low 则丢弃
_CONF_HIGH = 0.80
_CONF_MED = 0.55
_CONF_LOW = 0.30


def _confidence(score: float) -> str:
    if score >= _CONF_HIGH:
        return "high"
    if score >= _CONF_MED:
        return "medium"
    if score >= _CONF_LOW:
        return "low"
    return ""


def _substr_score(query: str, cand: str) -> tuple[float, str]:
    """子串匹配得分与标签。前缀 > 包含 > 反向包含 > 无。

    Returns:
        (score, label)，label 为 "" 表示无子串关系。
    """
    if not query or not cand:
        return 0.0, ""
    if query == cand:
        return 1.0, "exact"
    if cand.startswith(query):
        return 0.9, "prefix"
    if query.startswith(cand):
        return 0.7, "reverse_prefix"
    if query in cand:
        return 0.8, "contains"
    if cand in query:
        return 0.6, "reverse_contains"
    return 0.0, ""


class FuzzyMatcher:
    """模糊匹配引擎。

    对候选集合逐个计算子串/编辑距离/字符重叠三路得分，加权融合后按得分降序返回。
    """

    # 三路权重：子串主导（语义最直接），编辑距离次之，字符重叠辅助
    _W_SUBSTR = 0.5
    _W_EDIT = 0.35
    _W_OVERLAP = 0.15

    def __init__(self, *, top_k: int = 5, min_score: float = _CONF_LOW):
        self.top_k = top_k
        self.min_score = min_score

    def score(self, query: str, cand: str) -> FuzzyCandidate | None:
        """计算单候选得分，低于阈值返回 None。"""
        if not query or not cand:
            return None
        s_substr, label = _substr_score(query, cand)
        # 编辑距离：rapidfuzz.fuzz.ratio（C++ Levenshtein，与 levenshtein_ratio 同语义但更快）
        # 不可用时回退纯 Python levenshtein_ratio
        s_edit = (_rfz.ratio(query, cand) / 100.0) if _HAS_RAPIDFUZZ else levenshtein_ratio(query, cand)
        s_overlap = char_overlap(query, cand)
        # 子串无命中时其权重转移到编辑距离（子串为 0 不应直接拉低总分）
        if label:
            score = self._W_SUBSTR * s_substr + self._W_EDIT * s_edit + self._W_OVERLAP * s_overlap
        else:
            score = (self._W_SUBSTR + self._W_EDIT) * s_edit + self._W_OVERLAP * s_overlap
        score = round(score, 4)
        conf = _confidence(score)
        if not conf or score < self.min_score:
            return None
        reasons = []
        if label:
            reasons.append(label)
        reasons.append(f"edit:{s_edit:.2f}")
        reasons.append(f"overlap:{s_overlap:.2f}")
        return FuzzyCandidate(value=cand, score=score, confidence=conf, reasons=reasons)

    def search(self, query: str, candidates: Iterable[str]) -> list[FuzzyCandidate]:
        """在候选集合中模糊搜索，返回按得分降序的候选列表（不超过 top_k）。"""
        if not query:
            return []
        seen: set[str] = set()
        out: list[FuzzyCandidate] = []
        for cand in candidates:
            if not cand:
                continue
            cs = str(cand)
            if cs in seen:
                continue
            seen.add(cs)
            r = self.score(query, cs)
            if r is not None:
                out.append(r)
        out.sort(key=lambda x: x.score, reverse=True)
        return out[: self.top_k]

    @staticmethod
    def format_candidates(candidates: list[FuzzyCandidate]) -> str:
        """把候选列表格式化为带置信度的多行文本，供交互确认展示。"""
        if not candidates:
            return "（无匹配候选）"
        lines = []
        for i, c in enumerate(candidates, start=1):
            tag = {"high": "高", "medium": "中", "low": "低"}.get(c.confidence, c.confidence)
            lines.append(f"  {i}. [{tag}] {c.value}  (score={c.score:.2f}, {', '.join(c.reasons)})")
        return "\n".join(lines)


if __name__ == "__main__":
    m = FuzzyMatcher()
    cands = ["刑天一阶", "刑天二阶", "刑天三阶", "饕餮", "朱雀", "白虎一阶"]
    res = m.search("刑天", cands)
    print(FuzzyMatcher.format_candidates(res))

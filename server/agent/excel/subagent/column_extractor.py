"""Step1 前置列名提取 Stage（§Step1 改造核心）。

职责：在 LocatorAgent 粗路由之前，先从用户输入确定性提取列名候选 token，
用反向列名索引 + ColumnMatcher.match_topk 反查该列出现在哪些 (stem, sheet)，
产出"列名 → 候选表/sheet"的 topK 结果，供 LocatorAgent 收敛候选表。

修复三个失败案例根因：
  - 案例一 QA 饕餮：提取"属性""名称"等列名 → 定位到含"名称"列的 pet 表 + 行
  - 案例二 测试法宝3：提取"法宝名称""法宝描述"列名 → 用列名验证候选表是否真含该列
  - 案例三 春节活动：提取"活动类型""名称"列名 → activity 表（含"活动类型"列）
    命中数压过 spirit，避免 LLM 误路由

设计原则：
  - 确定性优先：用 candidate_terms（jieba/n-gram 规则）+ 反向索引反查，0 LLM
  - topK 不收敛单值：保留 topK 候选表供 LocatorAgent/DecomposeAgent 参考
  - 列名提取与值提取分离：本阶段只提列名候选，值由 DecomposeAgent LLM 处理
  - 失败回退：提取不到列名时返回空，不阻断后续 LocatorAgent 主路径
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

logger = logging.getLogger(__name__)


@dataclass
class ColumnLocateHit:
    """单条列名定位命中。

    Attributes:
        column: 命中的真实列名（header_name）
        stem: 所属表 stem
        sheet: 所属 sheet
        score: 列名匹配得分 [0,1]
        source: "exact" | "fuzzy" | "alias"
    """
    column: str
    stem: str
    sheet: str
    score: float
    source: str = "fuzzy"


@dataclass
class ColumnLocateResult:
    """列名提取 Stage 输出。

    Attributes:
        extracted_terms: 从用户输入提取的列名候选 token（candidate_terms 产出）
        hits: 按 (stem,sheet) 聚合的 topK 命中，按命中列名数+得分降序
        candidate_stems: 命中表 stem 去重列表（供 LocatorAgent 收敛用）
    """
    extracted_terms: list[str] = field(default_factory=list)
    hits: list[ColumnLocateHit] = field(default_factory=list)
    candidate_stems: list[str] = field(default_factory=list)

    @property
    def has_signal(self) -> bool:
        return bool(self.hits)


class ColumnExtractor:
    """列名提取器：用户输入 → 列名候选 token → 反向索引 topK 反查表/sheet。

    流程（0 LLM）：
      1. candidate_terms(text) 生成候选词（jieba 切词 + 去填充词 + bigram）
      2. 每个候选词对反向列名索引做精确 + FuzzyMatcher.search 模糊命中
      3. 聚合到 (stem, sheet)，按命中列名数+得分排序
      4. 输出 topK 候选表供 LocatorAgent 参考
    """

    def __init__(self, locator=None):
        self._locator = locator

    def _get_locator(self):
        if self._locator is not None:
            return self._locator
        try:
            from ..locator.table_locator import TableLocator
            self._locator = TableLocator()
        except Exception as e:
            logger.debug("TableLocator 初始化失败: %s", e, exc_info=True)
            return None
        return self._locator

    def extract(self, text: str) -> ColumnLocateResult:
        """主入口：text → ColumnLocateResult。

        失败返回空 result，不阻断后续 LocatorAgent 主路径。
        """
        if not text or not text.strip():
            return ColumnLocateResult()
        try:
            from ..parser.segmenter import candidate_terms
            terms = candidate_terms(text, max_terms=20)
        except Exception:
            logger.debug("candidate_terms 失败", exc_info=True)
            terms = []
        if not terms:
            return ColumnLocateResult(extracted_terms=[])

        locator = self._get_locator()
        if locator is None:
            return ColumnLocateResult(extracted_terms=terms)

        try:
            loc_results = locator.locate_by_column(terms, k=10)
        except Exception:
            logger.debug("locate_by_column 失败", exc_info=True)
            return ColumnLocateResult(extracted_terms=terms)

        hits: list[ColumnLocateHit] = []
        for r in loc_results:
            # matched_term 形如 "活动类型,名称"，拆分后逐列记录
            cols = [c for c in (r.matched_term or "").split(",") if c]
            for col in cols:
                hits.append(ColumnLocateHit(
                    column=col, stem=r.stem, sheet=r.sheet or "",
                    score=r.confidence, source="column_reverse"))

        # 候选表 stem 去重（按命中置信度降序）
        seen: set = set()
        candidate_stems: list[str] = []
        for r in loc_results:
            if r.stem and r.stem not in seen:
                seen.add(r.stem)
                candidate_stems.append(r.stem)

        return ColumnLocateResult(
            extracted_terms=terms, hits=hits, candidate_stems=candidate_stems)


__all__ = ["ColumnExtractor", "ColumnLocateResult", "ColumnLocateHit"]

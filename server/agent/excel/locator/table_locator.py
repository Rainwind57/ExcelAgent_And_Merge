"""表格定位器（层3）：自然语言描述 → 文件路径 + Sheet 名。

采用 5 级递进定位策略，每级带置信度：
  1. 精确文件名匹配（100%）：输入 == "pet.xlsx" 或 == stem
  2. 别名匹配（90%）：输入含已注册别名（如"灵兽" → pet.xlsx）
  3. 文件名模糊匹配（80%）：输入是 stem 子串或超串（如"pet"匹配 pet.xlsx）
  4. Sheet 名匹配（75%）：输入含某 sheet 名
  5. 列名语义匹配（60%）：输入含某列名（header_name）

歧义消解：同一置信度出现多个匹配时，返回候选列表供上层交互确认，不自动猜测。
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .alias_mapping import AliasMapping
from .column_matcher import _tokenize, _bow, _cosine
from .fuzzy_matcher import FuzzyMatcher, levenshtein_ratio
from .table_index import TableMeta, SheetMeta, load_index


@dataclass
class LocateResult:
    """单条定位结果。

    Attributes:
        path: 相对 workspace 的文件路径（如 pet.xlsx）
        stem: 文件名无后缀
        sheet: 命中的 sheet 名（级别 4/5 时可能为空，由调用方再消歧）
        confidence: 置信度 [0,1]
        level: 命中级别标签 "exact_file" | "alias" | "fuzzy_file" | "sheet_name" | "column_semantic"
        matched_term: 实际匹配到的字符串
    """
    path: str
    stem: str
    sheet: Optional[str] = None
    confidence: float = 0.0
    level: str = ""
    matched_term: str = ""


@dataclass
class LocateOutcome:
    """定位器整体输出：最佳结果 + 歧义候选列表。"""
    best: Optional[LocateResult] = None
    ambiguous: list[LocateResult] = field(default_factory=list)

    @property
    def is_ambiguous(self) -> bool:
        """最佳结果与次优结果置信度接近（差 < 0.05）时视为歧义。"""
        return bool(self.ambiguous)


class TableLocator:
    """5 级递进表格定位器。

    依赖表格注册中心（table_index）的元数据 + 别名映射（alias_mapping）。
    """

    # 5 级置信度常量
    C_EXACT_FILE = 1.00
    C_ALIAS = 0.90
    C_FUZZY_FILE = 0.80
    C_EDIT_FILE = 0.70
    C_SHEET_NAME = 0.75
    C_COLUMN = 0.60
    C_SEMANTIC = 0.55  # level6 余弦语义兜底（低于 column，level1-5 无命中时回退）

    # 歧义判定：同级别多匹配，或次优与最优置信度差值小于该阈值
    _AMBIGUITY_GAP = 0.05

    # 编辑距离补级阈值：ratio >= 该值才纳入候选（应对输错/近似文件名）
    _EDIT_THRESHOLD = 0.60

    def __init__(self, index: list[TableMeta] | None = None,
                 alias_mapping: AliasMapping | None = None,
                 fuzzy_matcher: FuzzyMatcher | None = None):
        self.index = index if index is not None else load_index()
        self.alias_mapping = alias_mapping or AliasMapping.load()
        self.fuzzy = fuzzy_matcher or FuzzyMatcher(top_k=5)

    # ── 各级匹配 ──────────────────────────────────────────────

    def _level1_exact_file(self, text: str) -> list[LocateResult]:
        """级别1：输入完全等于文件名或 stem。"""
        out: list[LocateResult] = []
        t = text.strip()
        if not t:
            return out
        t_l = t.lower()
        for tm in self.index:
            # 匹配 stem（不区分大小写）或完整文件名
            if t_l == tm.stem.lower() or t_l == Path(tm.path).name.lower():
                out.append(LocateResult(
                    path=tm.path, stem=tm.stem, confidence=self.C_EXACT_FILE,
                    level="exact_file", matched_term=t,
                ))
        return out

    def _level2_alias(self, text: str) -> list[LocateResult]:
        """级别2：文本包含已注册别名。一个别名可能指向同一文件，去重后返回。"""
        out: list[LocateResult] = []
        hits = self.alias_mapping.lookup_in_text(text)
        if not hits:
            return out
        # alias → file，同一文件只保留最长别名命中
        seen_file: dict[str, str] = {}
        for alias, fp in hits:
            seen_file.setdefault(fp, alias)
        # 别名命中需要文件确实在索引中
        stem_by_path = {tm.path: tm for tm in self.index}
        for fp, alias in seen_file.items():
            tm = stem_by_path.get(fp)
            if tm is None:
                # 别名指向的文件不在索引，尝试按 stem 兜底
                tm = next((t for t in self.index if t.stem == Path(fp).stem), None)
            if tm is not None:
                out.append(LocateResult(
                    path=tm.path, stem=tm.stem, confidence=self.C_ALIAS,
                    level="alias", matched_term=alias,
                ))
        return out

    def _level3_fuzzy_file(self, text: str) -> list[LocateResult]:
        """级别3：输入是 stem 的子串或超串。"""
        out: list[LocateResult] = []
        t = text.strip()
        if not t:
            return out
        t_l = t.lower()
        for tm in self.index:
            s_l = tm.stem.lower()
            if not s_l or not t_l:
                continue
            if t_l == s_l:
                continue  # 已在级别1处理
            if t_l in s_l or s_l in t_l:
                # 取实际命中的串作为 matched_term
                matched = t if t_l in s_l else tm.stem
                out.append(LocateResult(
                    path=tm.path, stem=tm.stem, confidence=self.C_FUZZY_FILE,
                    level="fuzzy_file", matched_term=matched,
                ))
        return out

    def _level4_sheet_name(self, text: str) -> list[LocateResult]:
        """级别4：文本包含某 sheet 名（精确包含 + 编辑距离模糊）。"""
        out: list[LocateResult] = []
        t = text.strip()
        if not t:
            return out
        t_l = t.lower()
        seen: set[tuple] = set()
        for tm in self.index:
            for s in tm.sheets:
                sn = s.name or ""
                if not sn or len(sn) < 2:
                    continue
                sn_l = sn.lower()
                hit = sn_l in t_l or t_l in sn_l
                if not hit and len(t_l) >= 2:
                    hit = levenshtein_ratio(t_l, sn_l) >= 0.6
                if hit and (tm.path, sn) not in seen:
                    seen.add((tm.path, sn))
                    out.append(LocateResult(
                        path=tm.path, stem=tm.stem, sheet=sn,
                        confidence=self.C_SHEET_NAME, level="sheet_name",
                        matched_term=sn,
                    ))
        return out

    def _level5_column(self, text: str) -> list[LocateResult]:
        """级别5：文本包含某列名（header_name），或表头与文本存在显著公共子串。

        原实现仅单向 `hn in text`，导致中文复合表头（如"任务目标类型"）
        遇到输入简写（如"目标类型"）时漏匹配。补足方向：
          - `hn in t`（原：表头是文本子串）
          - 表头长度 ≥4 时，表头与文本存在长度 ≥ 半个表头的连续公共子串
            （"任务目标类型" vs "目标类型" → 公共子串"目标类型"=4 ≥ 6/2=3 →命中）
        为限制全索引开销：仅长 ≤17 的表头做窗口扫描，更长表头退化为前/后缀窗口。

        §topK 改造：不再命中一个列名即 break，保留所有命中列名并按命中次数/相似度
        加权 sheet 置信度，避免"测试宝箱"等无关表噪声压过目标表。命中数越多、
        列名越具体，sheet 置信度越高。
        """
        out: list[LocateResult] = []
        t = text.strip()
        if not t:
            return out
        for tm in self.index:
            sheet_hits: dict[str, list[str]] = {}  # sheet -> 命中的列名列表
            for s in tm.sheets:
                for hn in s.header_names:
                    if not hn or len(hn) < 2:
                        continue
                    if self._column_in_text(hn, t):
                        sheet_hits.setdefault(s.name, []).append(hn)
            for sname, cols in sheet_hits.items():
                # 命中多列名 → 置信度上浮（每多一列 +0.02，上限 0.75）
                conf = min(self.C_COLUMN + 0.02 * (len(cols) - 1), 0.75)
                out.append(LocateResult(
                    path=tm.path, stem=tm.stem, sheet=sname,
                    confidence=conf, level="column_semantic",
                    matched_term=",".join(cols[:3]),
                ))
        return out

    @staticmethod
    def _column_in_text(hn: str, t: str) -> bool:
        """表头是否与文本相关：精确子串，或显著连续公共子串（中文复合表头放宽）。"""
        if hn in t:
            return True
        n = len(hn)
        if n < 4:
            return False
        min_len = max(3, (n + 1) // 2)  # 公共子串不短于半数(向上取整)
        if n > 17:
            # 长表头退化为前/后缀窗口，避免 O(n*m) 全索引膨胀
            return hn[:min_len] in t or hn[-min_len:] in t
        # 短表头滑动窗口扫描：任一长度 min_len 子串出现在文本即命中
        for i in range(0, n - min_len + 1):
            if hn[i:i + min_len] in t:
                return True
        return False

    def _level6_semantic(self, text: str) -> list[LocateResult]:
        """级别6:BoW 余弦语义回退(level1-5 无命中时兜底)。

        对 stem + 中文 header_names 做词袋余弦匹配,捕获 NL 关键词/精确匹配未覆盖的语义关联
        (对应 TableResolver 第二层余弦回退能力)。置信度按余弦分归一化到 [0, C_SEMANTIC],
        低于 column(0.6),仅作最后兜底,不压过 level1-5。
        """
        out: list[LocateResult] = []
        t = text.strip()
        if not t:
            return out
        q = _bow(_tokenize(t.lower()))
        if not q:
            return out
        for tm in self.index:
            chinese_terms = []
            for s in tm.sheets:
                for hn in s.header_names:
                    if hn and any('\u4e00' <= c <= '\u9fff' for c in hn):
                        chinese_terms.append(hn)
            context_text = tm.stem + " " + " ".join(chinese_terms)
            stem_bow = _bow(_tokenize(context_text))
            sc = _cosine(q, stem_bow)
            if sc > 0.05:
                out.append(LocateResult(
                    path=tm.path, stem=tm.stem,
                    confidence=self.C_SEMANTIC * min(sc, 1.0),
                    level="semantic", matched_term=f"(sim={sc:.2f})",
                ))
        return out

    def _resolve_sheet(self, tm: TableMeta, text: str) -> Optional[str]:
        """sheet 二级消歧(level1-3 命中 sheet=None 时填)。

        优先级:
          1. 单业务 sheet → 直接返回
          2. header_names 余弦 + 行数加权(辅助类 sheet 降权)
          3. 兜底首个业务 sheet

        对应 TableResolver._resolve_sheet 能力(简化:不依赖 sheet_aliases/table_context yaml,
        用 header 余弦 + 行数加权覆盖大部分场景)。
        """
        sheets = [s for s in tm.sheets if s.name.lower() != "config"]
        if not sheets:
            return None
        if len(sheets) == 1:
            return sheets[0].name
        helper_keywords = ("说明", "备注", "副本", "Sheet1", "sheet1", "枚举", "配置")
        data_sheets = [s for s in sheets if not any(kw in s.name for kw in helper_keywords)]
        candidates = data_sheets if data_sheets else sheets
        q = _bow(_tokenize(text))
        best, best_score = None, 0.0
        for s in candidates:
            txt = s.name + " " + " ".join(s.header_names)
            sc = _cosine(q, _bow(_tokenize(txt)))
            row_bonus = min(getattr(s, 'row_count', 0) * 0.001, 0.1)
            if any(kw in s.name for kw in helper_keywords):
                row_bonus = 0
                sc *= 0.7
            sc += row_bonus
            if sc > best_score:
                best, best_score = s.name, sc
        if best and best_score > 0.02:
            return best
        return sheets[0].name

    # ── 主入口 ────────────────────────────────────────────────

    def _bm25_recall(self, text: str) -> list[LocateResult]:
        """方法 G2：BM25 召回层（复用 _table_index search_blob + jieba）。

        对 _table_index.json 每条 sheet 的 search_blob 做 BM25 召回，返回 top-K
        LocateResult（confidence 归一到 0.3-0.5，低于规则命中避免压过）。
        env CODEMAKER_RAG_MODE=off 时返空（向后兼容，默认 bm25）。
        """
        mode = os.environ.get("CODEMAKER_RAG_MODE", "bm25").lower()
        if mode == "off" or not text:
            return []
        try:
            from .table_index import _idx_path
            from .rag_searcher import bm25_search
            hits = bm25_search(text, _idx_path(), top_k=5)
        except Exception:
            return []
        out: list[LocateResult] = []
        for h in hits:
            # BM25 score 归一：raw score 通常 0-20，映射到 0.3-0.5 confidence
            # （低于 level1-4 的 0.7-1.0，但可能补 level5/6 未命中的语义近邻）
            conf = min(0.5, 0.3 + h.score / 40.0)
            if conf < 0.31:
                continue
            out.append(LocateResult(
                path=h.path, stem=h.stem, sheet=h.sheet,
                confidence=conf, level="bm25",
                matched_term=",".join(h.matched_terms[:3]) or "bm25",
            ))
        return out

    def locate_all(self, text: str) -> list[LocateResult]:
        """执行全部 5 级匹配 + BM25 召回，返回合并后按置信度降序的结果列表。"""
        if not text:
            return []
        results: list[LocateResult] = []
        results.extend(self._level1_exact_file(text))
        results.extend(self._level2_alias(text))
        results.extend(self._level3_fuzzy_file(text))
        results.extend(self._level4_sheet_name(text))
        results.extend(self._level5_column(text))
        results.extend(self._level6_semantic(text))
        # 方法 G：BM25 召回层（规则未命中时的语义近邻补召回）
        results.extend(self._bm25_recall(text))

        # 同一 (path, sheet) 去重：保留置信度最高者
        best_by_key: dict[tuple, LocateResult] = {}
        for r in results:
            key = (r.path, r.sheet or "")
            cur = best_by_key.get(key)
            if cur is None or r.confidence > cur.confidence:
                best_by_key[key] = r
        out = list(best_by_key.values())
        out.sort(key=lambda r: r.confidence, reverse=True)
        return out

    def locate(self, text: str) -> LocateOutcome:
        """定位主入口：返回最佳结果 + 歧义候选。

        歧义规则：最高置信度级别有多个匹配，或次优结果与最优置信度差 < _AMBIGUITY_GAP，
        则把次优及同档结果放入 ambiguous，调用方应交互确认。
        """
        all_results = self.locate_all(text)
        if not all_results:
            return LocateOutcome(best=None, ambiguous=[])
        best = all_results[0]
        # level1-3 命中 sheet=None 时,二级消歧填 sheet(对应 TableResolver._resolve_sheet)
        if best.sheet is None:
            tm = next((t for t in self.index if t.path == best.path), None)
            if tm is not None:
                best.sheet = self._resolve_sheet(tm, text)
        # 同档（同 confidence）的其它结果视为歧义候选
        same_conf = [r for r in all_results[1:] if r.confidence == best.confidence]
        # 接近档（差值小于阈值）也纳入歧义候选
        close = [r for r in all_results[1:]
                 if best.confidence - r.confidence < self._AMBIGUITY_GAP
                 and r.confidence < best.confidence]
        ambiguous = same_conf + close
        # 去重并按置信度降序
        seen: set[tuple] = set()
        dedup: list[LocateResult] = []
        for r in ambiguous:
            k = (r.path, r.sheet or "")
            if k not in seen:
                seen.add(k)
                dedup.append(r)
        dedup.sort(key=lambda r: r.confidence, reverse=True)
        return LocateOutcome(best=best, ambiguous=dedup)

    def locate_best(self, text: str) -> Optional[LocateResult]:
        """便捷方法：返回最高置信度结果，歧义时仍返回首个（调用方应优先用 locate() 做确认）。"""
        out = self.locate(text)
        return out.best

    # ── 列名 topK 定位（Step1 前置列名提取 Stage 用） ────────────

    def locate_by_column(self, column_terms: list[str],
                         k: int = 5) -> list[LocateResult]:
        """按列名 token topK 反查表/sheet，返回按置信度降序的候选列表。

        Step1 列名提取阶段调用：用户输入"活动类型""名称"等列名 token 时，
        反查这些列出现在哪些 (stem, sheet)，按命中列名数+相似度加权排序，
        收敛候选表集合。修复案例三 spirit 误路由——"活动类型"列若在 activity 表
        则 activity 命中数高，压过 spirit。

        Args:
            column_terms: 从用户输入提取的列名候选（candidate_terms 产出）
            k: 返回候选数上限
        Returns:
            按 confidence 降序的 LocateResult 列表。命中 (stem,sheet) 聚合
            多列名置信度，单列名命中基础 0.65，每多一列 +0.03，上限 0.85。
        """
        if not column_terms:
            return []
        try:
            from .table_index import get_column_reverse_index
            rev_idx = get_column_reverse_index()
        except Exception:
            rev_idx = {}
        if not rev_idx:
            # 反向索引未建（旧索引/首次加载），回退到全索引扫描
            return self._locate_by_column_scan(column_terms, k)

        # (stem, sheet) -> [命中的 (列名, score)]
        sheet_scored: dict[tuple[str, str], list[tuple[str, float]]] = {}
        for term in column_terms:
            if not term or len(term) < 2:
                continue
            term_l = term.strip()
            # 精确命中反向索引
            exact_hits = rev_idx.get(term_l, [])
            if exact_hits:
                for stem, sname in exact_hits:
                    sheet_scored.setdefault((stem, sname), []).append((term_l, 1.0))
                continue
            # 模糊命中：用 FuzzyMatcher.search 在反向索引键集合找 topK 列名
            col_keys = list(rev_idx.keys())
            fuzzy_hits = self.fuzzy.search(term_l, col_keys)
            for fc in fuzzy_hits:
                for stem, sname in rev_idx.get(fc.value, []):
                    sheet_scored.setdefault((stem, sname), []).append((fc.value, fc.score))

        out: list[LocateResult] = []
        for (stem, sname), hits in sheet_scored.items():
            # 置信度：基础 0.65，每多一列命中 +0.03，相似度最高项加权
            base = 0.65
            bonus = 0.03 * (len(hits) - 1)
            max_sim = max(s for _, s in hits) if hits else 0.0
            conf = min(base + bonus + 0.05 * (max_sim - 0.5), 0.85) if max_sim > 0.5 \
                else min(base + bonus, 0.85)
            tm = next((t for t in self.index if t.stem == stem), None)
            if tm is None:
                continue
            out.append(LocateResult(
                path=tm.path, stem=stem, sheet=sname,
                confidence=conf, level="column_reverse",
                matched_term=",".join(h[0] for h in hits[:3]),
            ))
        out.sort(key=lambda r: r.confidence, reverse=True)
        return out[:k]

    def _locate_by_column_scan(self, column_terms: list[str],
                               k: int = 5) -> list[LocateResult]:
        """反向索引未建时的回退：全索引遍历 header_names 做 topK 列名匹配。"""
        if not column_terms:
            return []
        sheet_scored: dict[tuple[str, str], list[tuple[str, float]]] = {}
        for tm in self.index:
            for s in tm.sheets:
                for hn in s.header_names:
                    if not hn or len(hn) < 2:
                        continue
                    for term in column_terms:
                        if not term or len(term) < 2:
                            continue
                        fc = self.fuzzy.score(term.strip(), hn)
                        if fc is not None and fc.score >= 0.5:
                            sheet_scored.setdefault((tm.stem, s.name), []).append((hn, fc.score))
        out: list[LocateResult] = []
        for (stem, sname), hits in sheet_scored.items():
            base = 0.65
            bonus = 0.03 * (len(hits) - 1)
            max_sim = max(s for _, s in hits) if hits else 0.0
            conf = min(base + bonus + 0.05 * (max_sim - 0.5), 0.85) if max_sim > 0.5 \
                else min(base + bonus, 0.85)
            tm = next((t for t in self.index if t.stem == stem), None)
            if tm is None:
                continue
            out.append(LocateResult(
                path=tm.path, stem=stem, sheet=sname,
                confidence=conf, level="column_reverse",
                matched_term=",".join(h[0] for h in hits[:3]),
            ))
        out.sort(key=lambda r: r.confidence, reverse=True)
        return out[:k]

"""领域 RAG 阶段一 BM25 召回（方法 G）。

复用 `_table_index.json` 的 `search_blob`（非长文本列去重值小写，"\n" 分隔）+
jieba 分词做 BM25 召回，提升 TableLocator 命中率（覆盖规则未命中的语义近邻）。

阶段一零新依赖（jieba>=0.42.1 已在 requirements）。阶段二 vector（sentence-transformers
/faiss/chromadb）留后续。

接入方式：
  - TableLocator.locate_all 末尾加 BM25 召回层（G2）：候选 = 规则候选 ∪ BM25 top-K（去重）
  - env CODEMAKER_RAG_MODE=bm25|off，默认 bm25
  - dialog_failures/*.jsonl 索引（G3）：DecomposeAgent/Validator prompt 注入 few-shot 留后续
"""
from __future__ import annotations

import logging
import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# BM25 参数（标准 Okapi BM25）
_BM25_K1 = 1.5
_BM25_B = 0.75
_SEARCH_BLOB_MAX = 3000  # 与 table_index._SEARCH_BLOB_CAP 一致


@dataclass
class RAGHit:
    """BM25 召回命中条目。"""
    path: str
    stem: str
    sheet: str
    score: float
    matched_terms: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "path": self.path, "stem": self.stem, "sheet": self.sheet,
            "score": round(self.score, 4), "matched_terms": self.matched_terms,
        }


def _tokenize(text: str) -> list[str]:
    """jieba 分词（复用 segmenter.segment），去停用词 + 空串过滤。"""
    if not text:
        return []
    try:
        from .parser.segmenter import segment
    except Exception:
        # 兜底：简单按非字母数字切
        return [t for t in re.split(r"\W+", text.lower()) if t]
    toks = segment(text)
    # 过滤纯空白 + 单字符噪声（保留有区分度的词）
    return [t for t in toks if t and len(t) >= 1]


class _BM25Index:
    """内存 BM25 索引（建一次复用，thread-safe 读）。

    对 _table_index.json 每条 sheet 的 search_blob 建 doc tokens，
    计算 df（文档频率）+ doc_len + avgdl，查询时 BM25 打分。
    """

    def __init__(self):
        self._docs: list[dict] = []  # [{path, stem, sheet, tokens}]
        self._df: dict[str, int] = {}
        self._avgdl: float = 0.0
        self._built = False

    def build(self, table_index: dict) -> None:
        """从 _table_index.json 内容建索引。重复调用重建（无增量）。"""
        self._docs = []
        self._df = {}
        tables = table_index.get("tables") or table_index.get("data") or {}
        if not isinstance(tables, dict):
            # 某些版本 tables 是 list
            for t in tables if isinstance(tables, list) else []:
                self._add_table(t)
        else:
            for stem, tmeta in tables.items():
                self._add_table_entry(stem, tmeta)
        total_len = sum(len(d["tokens"]) for d in self._docs)
        self._avgdl = total_len / len(self._docs) if self._docs else 0.0
        self._built = True

    def _add_table(self, tmeta: dict) -> None:
        stem = tmeta.get("stem", "")
        path = tmeta.get("path", "")
        for sm in tmeta.get("sheets", []):
            self._add_sheet(path, stem, sm)

    def _add_table_entry(self, stem: str, tmeta: dict) -> None:
        path = tmeta.get("path", "")
        for sm in tmeta.get("sheets", []):
            self._add_sheet(path, stem, sm)

    def _add_sheet(self, path: str, stem: str, sm: dict) -> None:
        blob = sm.get("search_blob", "") or ""
        if not blob:
            return
        # search_blob 是 "\n" 分隔的值串，对每个值独立分词（值内复合词 jieba 切分，
        # 跨值不合并），保留值边界。整串分词会导致查询"灵兽饕餮"成一 token 无法匹配
        # 分开的"灵兽"+"饕餮"。
        tokens: list[str] = []
        for val in blob.split("\n"):
            val = val.strip()
            if not val:
                continue
            tokens.extend(_tokenize(val))
        if not tokens:
            return
        self._docs.append({
            "path": path, "stem": stem,
            "sheet": sm.get("name", ""), "tokens": tokens,
        })
        seen = set()
        for t in tokens:
            if t not in seen:
                self._df[t] = self._df.get(t, 0) + 1
                seen.add(t)

    def search(self, query: str, top_k: int = 5) -> list[RAGHit]:
        """BM25 查询。返回 top-K 命中（score 降序）。"""
        if not self._built or not self._docs:
            return []
        q_tokens = _tokenize(query)
        if not q_tokens:
            return []
        N = len(self._docs)
        scores: list[tuple[float, dict, list]] = []
        for doc in self._docs:
            tf_map: dict[str, int] = {}
            for t in doc["tokens"]:
                tf_map[t] = tf_map.get(t, 0) + 1
            dl = len(doc["tokens"])
            score = 0.0
            matched: list[str] = []
            for qt in q_tokens:
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue
                df = self._df.get(qt, 0)
                if df == 0:
                    continue
                idf = math.log((N - df + 0.5) / (df + 0.5) + 1.0)
                denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * dl / (self._avgdl or 1))
                score += idf * (tf * (_BM25_K1 + 1)) / denom
                matched.append(qt)
            if score > 0:
                scores.append((score, doc, matched))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [RAGHit(path=s[1]["path"], stem=s[1]["stem"], sheet=s[1]["sheet"],
                       score=s[0], matched_terms=list(set(s[2])))
                for s in scores[:top_k]]


# 单例索引（建一次复用，避免每次查询重扫 _table_index.json）
_bm25_index: Optional[_BM25Index] = None
_bm25_index_path: Optional[str] = None


def _load_table_index(index_path: Path) -> dict:
    """加载 _table_index.json（全量读，与 table_index.load_index 一致）。"""
    import json
    try:
        with open(index_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("[RAG] _table_index.json 加载失败 %s: %s", index_path, e)
        return {}


def _get_index(index_path: Path) -> _BM25Index:
    """获取/构建 BM25 单例索引（按 path 缓存）。"""
    global _bm25_index, _bm25_index_path
    sp = str(index_path)
    if _bm25_index is not None and _bm25_index_path == sp:
        return _bm25_index
    idx = _BM25Index()
    data = _load_table_index(index_path)
    idx.build(data)
    _bm25_index = idx
    _bm25_index_path = sp
    return idx


def bm25_search(query: str, index_path: Path, top_k: int = 5) -> list[RAGHit]:
    """BM25 召回入口（方法 G1）。

    对 _table_index.json 每条 sheet 的 search_blob 做 jieba 分词 + BM25 排序，
    返回 top-K {path, stem, sheet, score, matched_terms}。
    env CODEMAKER_RAG_MODE=off 时返空 list（关闭召回，向后兼容）。
    """
    mode = os.environ.get("CODEMAKER_RAG_MODE", "bm25").lower()
    if mode == "off" or not query:
        return []
    try:
        idx = _get_index(index_path)
        return idx.search(query, top_k=top_k)
    except Exception as e:
        logger.debug("[RAG] bm25_search 失败: %s", e)
        return []

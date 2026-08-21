"""O18 方法 G 阶段一 BM25 RAG 单测（rag_searcher + table_locator 注入）。

覆盖：
- G1 bm25_search(query, index_path, top_k) → list[RAGHit]（score 降序 + matched_terms）
- G2 table_locator.locate_all 含 BM25 召回（规则未命中时 BM25 补召回）
- G4 env CODEMAKER_RAG_MODE=off → bm25_search 返空 + locate_all 不调 BM25
- BM25 索引单例缓存（重复调用不重建）

用 tmp_path 构造迷你 _table_index.json（含 search_blob）+ jieba 分词。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from agent.excel.rag_searcher import (
    bm25_search, RAGHit, _BM25Index, _get_index,
)


def _write_mini_index(path: Path, tables: dict):
    """构造迷你 _table_index.json。tables={stem: {path, sheets:[{name, search_blob}]}}。"""
    data = {"tables": tables}
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


class TestG1BM25Search:
    def test_off_mode_returns_empty(self, tmp_path, monkeypatch):
        """G4：CODEMAKER_RAG_MODE=off → 返空 list。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "off")
        idx_path = tmp_path / "_table_index.json"
        _write_mini_index(idx_path, {"pet": {"path": "pet.xlsx", "sheets": [
            {"name": "Pet", "search_blob": "灵兽\n饕餮\n白虎"}]}})
        # 重置单例（off 模式不建索引，但保险）
        import agent.excel.rag_searcher as _rs
        _rs._bm25_index = None
        assert bm25_search("灵兽", idx_path) == []

    def test_query_hits_relevant_table(self, tmp_path, monkeypatch):
        """G1：query 命中相关表 → top-K 含该表 + score>0 + matched_terms 非空。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "bm25")
        idx_path = tmp_path / "_table_index.json"
        _write_mini_index(idx_path, {
            "pet": {"path": "pet.xlsx", "sheets": [
                {"name": "Pet", "search_blob": "灵兽\n饕餮\n白虎"}]},
            "item": {"path": "item.xlsx", "sheets": [
                {"name": "Item", "search_blob": "法宝\n丹药\n材料"}]},
        })
        import agent.excel.rag_searcher as _rs
        _rs._bm25_index = None  # 重置缓存
        hits = bm25_search("灵兽饕餮", idx_path, top_k=5)
        assert len(hits) > 0
        # pet 表应排前（search_blob 含 灵兽/饕餮）
        top = hits[0]
        assert top.stem == "pet"
        assert top.score > 0
        assert "灵兽" in top.matched_terms or "饕餮" in top.matched_terms

    def test_no_match_returns_empty(self, tmp_path, monkeypatch):
        """query 无匹配 → 返空 list。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "bm25")
        idx_path = tmp_path / "_table_index.json"
        _write_mini_index(idx_path, {"pet": {"path": "pet.xlsx", "sheets": [
            {"name": "Pet", "search_blob": "灵兽"}]}})
        import agent.excel.rag_searcher as _rs
        _rs._bm25_index = None
        hits = bm25_search("不存在的词xyz", idx_path)
        assert hits == []

    def test_top_k_limits_results(self, tmp_path, monkeypatch):
        """top_k 限制返回数。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "bm25")
        idx_path = tmp_path / "_table_index.json"
        tables = {}
        for i in range(5):
            tables[f"t{i}"] = {"path": f"t{i}.xlsx", "sheets": [
                {"name": "S", "search_blob": f"共同词\n表{i}"}]}
        _write_mini_index(idx_path, tables)
        import agent.excel.rag_searcher as _rs
        _rs._bm25_index = None
        hits = bm25_search("共同词", idx_path, top_k=2)
        assert len(hits) <= 2


class TestBM25IndexCache:
    def test_index_cached_by_path(self, tmp_path, monkeypatch):
        """同 path 重复调用复用单例（_bm25_index_path 不变）。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "bm25")
        idx_path = tmp_path / "_table_index.json"
        _write_mini_index(idx_path, {"pet": {"path": "pet.xlsx", "sheets": [
            {"name": "Pet", "search_blob": "灵兽"}]}})
        import agent.excel.rag_searcher as _rs
        _rs._bm25_index = None
        idx1 = _get_index(idx_path)
        idx2 = _get_index(idx_path)
        assert idx1 is idx2  # 同一对象，复用
        assert _rs._bm25_index_path == str(idx_path)

    def test_different_path_rebuilds(self, tmp_path, monkeypatch):
        """不同 path → 重建索引（_bm25_index_path 更新）。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "bm25")
        p1 = tmp_path / "idx1.json"
        p2 = tmp_path / "idx2.json"
        _write_mini_index(p1, {"a": {"path": "a.xlsx", "sheets": [{"name": "S", "search_blob": "词a"}]}})
        _write_mini_index(p2, {"b": {"path": "b.xlsx", "sheets": [{"name": "S", "search_blob": "词b"}]}})
        import agent.excel.rag_searcher as _rs
        _rs._bm25_index = None
        i1 = _get_index(p1)
        i2 = _get_index(p2)
        assert i1 is not i2  # 不同 path 不同对象


class TestG2LocatorInjection:
    def test_bm25_recall_off_mode_no_injection(self, tmp_path, monkeypatch):
        """G2+G4：off 模式 → _bm25_recall 返空（不污染规则候选）。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "off")
        from agent.excel.locator.table_locator import TableLocator, TableMeta, SheetMeta
        # 构造空 index 避免读真 _table_index.json
        sm = SheetMeta(name="Pet", headers=["编号"], header_names=["编号"],
                       header_row=1, data_start_row=5, row_count=0,
                       row_index={}, samples=[], search_blob="灵兽")
        tm = TableMeta(stem="pet", path="pet.xlsx", sheets=[sm])
        loc = TableLocator(index=[tm])
        r = loc._bm25_recall("灵兽")
        assert r == []

    def test_bm25_recall_on_returns_hits(self, tmp_path, monkeypatch):
        """G2：bm25 模式 → _bm25_recall 返 LocateResult（confidence 归一 0.3-0.5）。"""
        monkeypatch.setenv("CODEMAKER_RAG_MODE", "bm25")
        idx_path = tmp_path / "_table_index.json"
        _write_mini_index(idx_path, {"pet": {"path": "pet.xlsx", "sheets": [
            {"name": "Pet", "search_blob": "灵兽\n饕餮"}]}})
        # mock _idx_path 返 tmp_path 索引
        import agent.excel.locator.table_index as _ti
        monkeypatch.setattr(_ti, "_idx_path", lambda: idx_path)
        import agent.excel.rag_searcher as _rs
        _rs._bm25_index = None
        from agent.excel.locator.table_locator import TableLocator, TableMeta, SheetMeta
        sm = SheetMeta(name="Pet", headers=["编号"], header_names=["编号"],
                       header_row=1, data_start_row=5, row_count=0,
                       row_index={}, samples=[], search_blob="灵兽")
        tm = TableMeta(stem="pet", path="pet.xlsx", sheets=[sm])
        loc = TableLocator(index=[tm])
        r = loc._bm25_recall("灵兽饕餮")
        assert len(r) > 0
        assert r[0].stem == "pet"
        assert 0.3 <= r[0].confidence <= 0.5
        assert r[0].level == "bm25"

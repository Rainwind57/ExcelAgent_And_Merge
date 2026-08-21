# O18 方法 G 阶段一 BM25 RAG

> 轮次：O18（2026-08-18）
> 范围：G1/G2/G4 闭环 — BM25 召回提升 TableLocator 命中率。G3 few-shot + G6 A/B 评估留 follow-up。见 `docs/OPTIMIZATION_LEDGER.md` §1 + §3 + `docs/archive/事前预防优化TODO.md` 第四波 G。
> 前置：jieba>=0.42.1（requirements）+ `_table_index.json` search_blob 字段 + `segmenter.segment` 现成。

## 设计

复用 `_table_index.json` 的 `search_blob`（非长文本列去重值小写，`\n` 分隔）+ jieba 分词做 Okapi BM25 召回。search_blob 按 `\n` 分值独立分词保值边界（避免整串分词导致查询复合词无法匹配分开的值）。BM25 top-5 归一 confidence 0.3-0.5（低于规则 0.7-1.0 不压过）注入 `locate_all` 末尾。

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| G1 新模块 | `server/agent/excel/rag_searcher.py`（新文件） | `RAGHit` dataclass（path/stem/sheet/score/matched_terms）；`_BM25Index` 类（Okapi BM25，k1=1.5/b=0.75，build 从 _table_index.json 建 doc tokens + df + avgdl，search 返 top-K）；`_tokenize` 复用 `segmenter.segment` jieba 分词（相对导入 `.parser.segmenter`）；`_get_index` 单例缓存按 path 复用；`bm25_search(query, index_path, top_k)` 入口（env gate）。 |
| G1 search_blob 分词 | `server/agent/excel/rag_searcher.py` `_BM25Index._add_sheet` | 按 `\n` 分值独立分词（保值边界，避免复合词失配）。 |
| G2 召回注入 | `server/agent/excel/locator/table_locator.py` `_bm25_recall`（新方法）+ `locate_all` | `locate_all` 末尾 `results.extend(self._bm25_recall(text))`；`_bm25_recall` 调 `bm25_search` + 归一 confidence 0.3-0.5 + level="bm25"。加 `import os`。 |
| G4 env gate | `server/agent/excel/rag_searcher.py` + `_bm25_recall` | `CODEMAKER_RAG_MODE=bm25|off`（默认 bm25，off 关闭召回向后兼容）。 |
| 测试 | `server/tests/test_rag_bm25_o18.py`（新增 8） | TestG1BM25Search 4（off 返空/命中相关表/无匹配返空/top_k 限制）+ TestBM25IndexCache 2（同 path 复用/不同 path 重建）+ TestG2LocatorInjection 2（off 不注入/on 返 hits 归一 0.3-0.5）。 |

## 确定性验证

```
python -m pytest server/tests/test_rag_bm25_o18.py -q
=> 8 passed in 1.81s

python -m pytest server/tests/ -q
=> 981 passed, 1 failed, 1 skipped in 228.95s
   # 1 预存红：test_column_matcher_semantic::test_fuzzy_simplified_colname_hits
   #   与 O18 无关（列匹配器优先级，未触及 column_matcher.py）
```

## 残留 follow-up

- **G3 few-shot 注入**：`dialog_failures/*.jsonl` 建索引注入 DecomposeAgent/Validator prompt（8 jsonl 含 table_stem/sheet/steps，需 prompt 接线）。
- **G6 A/B 评估**：扩 `skill_ab_test.py`/`table_case_eval.py` 加 BM25 对照维（bm25 off/on），断言 locate_rate 不退化（基线 1.0）。
- **阶段二 vector**：sentence-transformers/faiss/chromadb 装依赖，留后续。

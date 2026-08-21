# O20d 覆盖度修复（S1/S3/S4 候选策略 + 全链兜底 + 占位符未解入 failures）

> 来源：`docs/bench_failure_fix_requirements.md` §4。
> 日期：2026-08-19。
> 状态：代码完成（12 单测绿，全量 1006 passed / 1 预存红零回归），e2e 阻 R7。

---

## 0. 改动摘要

| 模块 | 改动 | 根因（§4.1） |
|---|---|---|
| `locator_agent.py:_expand_by_fk` | 单跳 → BFS 多跳（2 跳上限，置信度衰减） | 候选不全（`_expand_by_fk` 只从已命中候选补 FK 对端，未命中整表的业务子表不进入） |
| `decompose_agent.py:_full_chain_fallback` | 单表并发产 <2 intent 且 jobs≥2 时补一次全候选 schema 合置单 prompt | DecomposeAgent 单表漏拆（每表单 prompt LLM 对业务链识别不全） |
| `agent.py:5946-5960` | 占位符残留 → append failure 后 `res.ok=False` + `return res` 跳写库 | 占位符未解仍进 `_dispatch` 写库污染数据（`<列名>` 占位文本落盘） |

---

## 1. LocatorAgent `_expand_by_fk` 多跳

### 1.1 根因（§4.1）
- 原 `_expand_by_fk` 单跳：relation graph 中任一端 stem 在候选内则补对端。
- S1 期望 8 表（quest/combat/reward/item*/entity_prefab/interaction/spawn_*），alias 命中 quest 后单跳仅扩直接 FK 对端 combat，2 跳外的 reward/item 经 combat→reward 链不进候选。

### 1.2 修复
- BFS 多跳传递闭包：双向邻接表（任一端命中候选即扩对端），2 跳上限（env `CODEMAKER_LOCATOR_FK_HOPS` 默认 2，可调）。
- 置信度按跳衰减：hop1=0.50 / hop2=0.40（每跳 -0.10），level=`fk_expanded`，matched_term=`fk_h{hop}`。
- 2 跳上限避免无限膨胀（防 DecomposeAgent 上下文噪声，原注释"locate_all 全量列名匹配会引入 40+ 噪声表"）。

### 1.3 测试（`tests/test_coverage_o20d.py` TestExpandByFkMultiHop 7）
- 2 跳扩表：候选 quest → 扩 combat(h1) + reward(h2)
- 置信度衰减：hop1=0.50, hop2=0.40
- env 上限：`CODEMAKER_LOCATOR_FK_HOPS=1` → 仅扩 1 跳
- 双向扩表：候选 reward → 反向扩 combat + quest
- 不重复扩：候选已含 combat → 不重复
- 空候选 → 空
- 无 relation_graph → 空

---

## 2. DecomposeAgent `_full_chain_fallback` 全链 LLM 兜底

### 2.1 根因（§4.1）
- `DecomposeAgent.decompose` 每候选表单 prompt 并发，产出意图数受 LLM 单表识别能力限制。
- S1/S3 单表漏拆：LLM 对单表 schema 看不到完整跨表业务链（任务→战斗→奖励），漏拆子任务。

### 2.2 修复
- 触发条件：单表并发产 `<2` intent 且 `jobs≥2` 时，补一次全候选 schema 合置单 prompt。
- `_build_schema_block(candidates)` 接受 list 已支持多表合置，复用产全候选 schema 块。
- LLM 看完整 FK 链（schema_all + fk_block），识别业务子任务跨表拆分。
- 兜底产出 `> len(all_intents)` 则覆盖，否则保留单表原产（不退化）。
- 走同一 `_to_split_intents` + 候选校验（`valid_stems` 过滤幻觉表，同 `_run_one`）。
- **独立 cancel event**：不复用 F3 fail-fast 的 `_local_ce`（单表并发前 2 候选均空响应时 `_local_ce.set()` 取消剩余，但兜底是新独立 LLM 调用不应被旧 fail-fast 取消），新建 Event + mirror run 级 `_cancel_event`。
- timeout 翻倍（`per_to * 2`），全候选 schema 比单表大需更长 LLM 响应时间。

### 2.3 测试（TestFullChainFallback 4）
- 单表产 0（jobs=3 per 返空）→ 触发兜底，兜底产 3 > 0 覆盖
- 单表产 2（jobs=2 per 返本表 1）→ 不触发兜底，full_chain resp 不被读
- 单表产 0 + 兜底产 0 → 0 不 > 0 不覆盖，保留原 0
- 兜底含幻觉表 → 过滤（valid_stems 校验）

### 2.4 Mock 设计
- MockClient 按调用序号分发：前 `per_candidate_count` 次（=单表并发 jobs 数）返 `_per_candidate_resp`，之后返 `_full_chain_resp`（兜底单次调用）。
- DecomposeAgent.decompose 先 ThreadPoolExecutor 并发跑 N 个 `_run_one`，再按需调一次 `_full_chain_fallback` → 第 N+1 次 prompt 即兜底。

---

## 3. 占位符未解 → skip 写库 + failure 上报

### 3.1 根因（§4.1 第 3 点 + §4.3）
- `agent.py:5876-5960` 占位符残留时已 append `placeholder_unresolved` failure 到 `res.failures`。
- 但 5961 仍进 `_dispatch` 写库 → `<列名>` 占位文本落盘污染数据。
- 5876 注释明确"故暂停"，实际未暂停。

### 3.2 修复
- `agent.py:5946-5960` append failure 后：`res.ok=False` + `res.add_thinking(...)` + `return res`。
- 跳 `_dispatch` 写库，保 D6 不静默吞 + 不留半成品。
- `res.failures` 已聚合（4990）→ `_phase_summarize` 上报（5019）。

### 3.3 测试（TestPlaceholderUnresolvedSkipWrite 1）
- 占位符残留 → append `placeholder_unresolved` failure + `res.ok=False` + 不进 `_dispatch`。
- 验证 failure dict 结构（type/col/table/status）。

---

## 4. 确定性验证

| 测试文件 | 测数 | 覆盖 | 结果 |
|---|---|---|---|
| `tests/test_coverage_o20d.py` | 12 | locator 多跳×7 + full_chain_fallback×4 + placeholder skip×1 | 12/12 passed |
| 相关回归（decompose/parse_agent/O20b/c/connectivity/4step_config） | 71 | locator/decompose/validator/agent 链路 | 71/71 passed（1 skipped） |
| 全量回归 | 1006 | 全仓库 | 1006/1006 passed（1 预存红 `test_column_matcher_semantic`，1 skipped） |

**零回归**：1 预存红 `test_column_matcher_semantic`（列匹配器优先级，O5-O20c 持续存在，与所有 O 改动无关）。

---

## 5. 残留 follow-up

1. **LLM 能力缺口**（§4.4）：单表漏拆子任务，代码层候选策略 + 全链兜底可缓解，但 LLM 本身对复杂业务链理解不足需单独跟进。G3 few-shot RAG 注入 DecomposeAgent prompt 是长期解（O18 留 follow-up）。
2. **S1 6 条重复根治**（O20b follow-up）：fields 含不同占位符 sig 不同 → `_dedup_intents` 不去重，需 DecomposeAgent prompt 约束"同表只产一条主配置"或 Step5 层按 (stem,sheet) 放宽去重。
3. **S4 parser 崩**（独立项）：`codemaker_parser._parse_via_llm` 返 None（S4 长 LLM 空响应），需加 fallback/重试/降级，非 O20 引入。
4. **e2e 验证**（阻 R7）：serve:8666 + backend:8000 未在线，O20d e2e 指标待 serve 起后跑 S1/S3/S4 验证覆盖度提升。

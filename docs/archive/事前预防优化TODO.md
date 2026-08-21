# 事前预防优化 TODO（八方法落地清单）

> 输入：用户《八条优化建议 A-H》+ 三路验证子报告。
> 生成时间：2026-08-17。
> 流程：每块落地 → 跑 before/after 指标 → 填 `docs/优化全过程.md` R{n} 记录 → 更新仪表盘。
> 路径口径：本仓库源码根在 `server/` 下，所有引用以 `server/` 为基准。

---

## 〇、精校结果（对原假设的修正）

### 1. 路径映射修正

| 原引用 | 实际路径 |
|---|---|
| `agent.py`（CRUD 链/写库） | `server/agent/excel/core/agent.py`（非 `server/routers/agent.py`，那是路由版无 3860 链） |
| `formula_cache_validator.py` | `server/agent/excel/formula/formula_cache_validator.py`（含 `formula/` 子目录） |
| `merge_stages.py` | `server/routers/merge_stages.py`（不在 `engine/`） |
| `diff.py`（含 `_save_with_formula_cache`） | `server/routers/diff.py:598`（不在 `excel/`） |
| `schema_infer.py` | `server/agent/excel/parser/schema_infer.py`（**文件仅 913 行，:972 不存在**；推断逻辑在 `scan_sheet:218` / `scan_directory:290`） |
| `backup_audit.py` / `style_utils.py` | `server/agent/excel/core/backup_audit.py` / `core/style_utils.py` |
| `parser.py`（read_comments） | `server/engine/parser.py:79` |

### 2. 行号修正

| 项 | 原声称 | 实际 | 备注 |
|---|---|---|---|
| `_phase_execute` | agent.py:4929 | **core/agent.py:5318** | 差 ~400 行 |
| `_verify_write` | agent.py:4722 | **core/agent.py:5531** | 差 ~800 行 |
| `backup_and_record` 调用点 | agent.py:5424 | **core/agent.py:5426**（定义在 backup_audit.py:108） | ✓ 近准 |
| `_rollback_write` | agent.py:6653 | **core/agent.py:6650** | ✓ 近准 |
| `SheetDiff.missing_rows` | models.py:37 | models.py:42 | 近准 |
| `MergeRequest.incremental` | models.py:90 | models.py:96 | 近准 |
| `id_resolver.py split` default | :95 | :100（拆行逻辑 :137-186） | 近准 |
| 其余 B/C/F/G 关键函数行号 | — | — | 精确 |

### 3. 假设纠正（影响两块方法）

**方法 A 原假设不成立**：
- 原判断："StubCodeMakerCLI 写后直接 save 不触发重算，公式守门需在 agent 层接线"。
- 实际：`cli_interface.py:217 _save_with_cache_check` 已被 `write_cell:599` / `append_row:641` / `insert_column` / `delete_row` / `rename_column` 等所有写方法调用，内含 `snapshot_before` → `wb.save` → `validate_and_fix` → 丢失触发 LibreOffice 重算。
- 真正缺口：重算后**仍丢**（`result.needs_manual_fix=True`）时，`_save_with_cache_check:248` 只 `logger.warning`，**不阻断、不上报 agent 层**。agent 层写库流程也不消费 `CLICallResult.cache_message`。
- 结论：方法 A 重新定义为「补 `needs_manual_fix` → agent 层 hold 链路 + 环境开关」，非"从零接线"。

**方法 D 接线点错位**：
- 原判断："在 `_phase_execute` 写库后、`_verify_write` 后 restore 批注"。
- 实际：save 发生在 `cli_interface.py:236 wb.save`（CLI 内部），`_verify_write:5531` 只在 `enable_verify_repair_loop` 开启时跑，非通用。
- 真正缺口：restore 必须在**同一 wb 的 `wb.save` 之前**注入，否则二次 save 仍丢批注。
- 结论：方法 D 下沉到 CLI `_save_with_cache_check` 内做（同 wb，无需跨层传 snapshot），agent 层只做"二次读取做差 → hold/audit"。

### 4. 依赖与基础设施现状

- `requirements.txt` 有 `jieba>=0.42.1`，**无** sentence-transformers/faiss/chromadb（方法 G 第二阶段需装依赖，第一阶段 BM25 可直接用 jieba）。
- SSE 通道现成：`merge_branch.py:1214 branch_compare_progress`（`event:`/`data:` 格式）。
- step_sink 现成：`agent_service.py:1950 _step_sink`（每步 `{name,ok,detail,step_id,status}`）、`:1954 _subtask_sink`（Step5 子任务 start/done）。
- **`pre_commit_hold` 事件标识全局无**，需新建（方法 B 产出，方法 A/C/D 升级复用）。
- `docs/优化全过程.md` 存在，R1-R8（缺 R7）记录格式：仪表盘表格行 + `#### R{n}...` 明细章节。本系列承接为 **R9 起**。

---

## 一、八方法可行性重评

| 方法 | 原可行性 | 重评 | 重新定义/调整 |
|---|---|---|---|
| **A 公式守门** | 接线 0.5 天 | 假设不成立，重定位 | 改为「补 `needs_manual_fix` → hold 链路 + `CODEMAKER_FORMULA_GATE` 开关」。CLI 已有保护，工作量从接线变为"打通标志上抛 + 环境门控"。 |
| **B Pre-flight hold** | 1-1.5 天 | 可行 | 直接落地。新建 `pre_commit_hold` SSE 通道（基础设施，A/C/D 升级复用）。 |
| **C PATCH_CONFIG 守门** | 0.5-1 天 | 可行 | 直接落地。新建 `engine/patch_validator.py` 5 条硬规则。 |
| **D 批注/样式回放** | 1 天 | 接线点修正 | 下沉到 CLI `_save_with_cache_check` 内 save 前回写批注（同 wb），agent 层只做做差告警。 |
| **E Schema 血缘联动** | 1.5-2 天 | 可行（schema_infer 定位修正） | 新建 `engine/column_lineage.py`，schema 扫描复用 `schema_infer.scan_sheet:218`。 |
| **F 编号账本跨分支** | 1.5 天 | 可行 | 扩 `id_scope.validate_all` 支持多 root。 |
| **G 领域 RAG** | 阶段一 2-3 天 / 二 3-5 天 | 阶段一可行，二需装依赖 | 阶段一 BM25 复用 jieba + `search_blob`，零新依赖。 |
| **H 多 Agent 对抗网** | 3-5 天 | 可行（最复杂） | 最后做，挂 `dispatcher` + `_step5_verify:594` + `core/agent.py:3860` CRUD 链。AuditorAgent 纯规则终判。 |

---

## 二、调整后落地路线图

依赖关系决定先后。`pre_commit_hold` 通道是 A/C/D 升级的前置，提前到第二波首发。

### 第一波 P0 + 独立可测（约 2-3 天）

- [ ] **方法 D 核心批注回写**（下沉到 CLI，独立可测，不依赖 hold 通道）
- [ ] **方法 A 重新定义版阶段一**（补 `needs_manual_fix` → audit_log + warning 升级，hold 部分待第二波通道）

### 第二波 P0 + hold 通道基建（约 2-3 天）

- [ ] **方法 B Pre-flight hold**（建 `pre_commit_hold` SSE 通道 + `_preflight_row_manifest` 漏行预检 + hold 事件）
- [ ] **方法 C PATCH_CONFIG 守门**（新建 `engine/patch_validator.py`，复用 B 的 hold 通道）
- [ ] **方法 A 升级**（`needs_manual_fix=True` 接入 `pre_commit_hold`，kind=`formula_loss`）
- [ ] **方法 D 升级**（批注做差非空接入 `pre_commit_hold`，kind=`comment_loss`）

### 第三波 P0/P1 + 扩范围（约 3-4 天）

- [ ] **方法 F 编号账本跨分支**（`id_scope.validate_multi_branch`）
- [ ] **方法 E Schema 血缘联动**（新建 `engine/column_lineage.py` + `structural.py` 加 `column_added/changed`）

### 第四波 质量底座（约 5-8 天）

- [ ] **方法 G 阶段一 BM25 RAG**（`excel/rag_searcher.py`，复用 jieba）
- [ ] **方法 H 多 Agent 对抗网**（Red/Blue/Auditor，挂 dispatcher + `_step5_verify` + CRUD 链）

---

## 三、逐块落地 TODO + 验证

### 第一波 · 方法 D 核心批注回写 ✅ 已落地（R9-D）

> 目标：写表后批注 100% 保留（openpyxl save 偶丢批注的精细化保护层）。
> 接线点修正：原方案 `_verify_write:4722` 错误（只在 verify_repair_loop 路径跑，且 restore 须在 wb.save 同 wb 前注入）。**下沉到 `cli_interface._save_with_cache_check:217`**（同 wb，save 前 snapshot + save 后 reload 做差 + 回写 Comment + 二次 save + 二次做差记数）。
> 报告：`tests/reports/comment_guard_latest.{json,md}`

- [x] D1. `_comment_snapshot(wb)`：遍历 wb 所有 sheet 所有 cell 收集 `{(sheet,coord):(text,author)}`，无批注 fast-path 返回 `{}`。（注：当前仅批注，样式指纹留第二波——`style_utils.copy_cell_style` 工具就位未接）
- [x] D2. `_save_with_cache_check` 的 `wb.save(path)` 前调 `_comment_snapshot(wb)` 取快照。
- [x] D3. `wb.save` 后 `_detect_comment_loss` reload 做差 → 丢失非空 → `_replay_comments` 原 wb 回写 `Comment(text,author)` → 二次 `wb.save`。
- [ ] D4. 回写后二次做差仍丢 → 记 `audit_log` operation=`comment_replay_partial`。（留第二波升级，agent 层消费 comment_replay 时接）
- [x] D5. 环境开关 `CODEMAKER_COMMENT_GUARD=on|off`（默认 on，off 跳过守门仅 save）。
- [x] D6. 测试 `tests/test_comment_guard.py`：实际 5 用例（无批注 fast-path / 改非批注列 / 改批注列 / append_row / 开关 off），断言写后批注文本一致。
- [x] D7. 跑 `test_comment_guard.py`(5) + 回归 `test_save_cache_scope`(3)+`test_formula_cache`(6)+`test_fast_apply_eligible`(7)+`test_write_verification`(5) → 26 passed 零回归。
- [x] D8. 产出报告 `tests/reports/comment_guard_latest.{json,md}`（批注保留率 100%）。

**验证指标**：批注保留率 100%（5/5 用例写后文本一致）；26 测试零回归。

### 第一波 · 方法 A 重新定义版阶段一 ✅ 已落地（R9-A1）

> 目标：CLI 检测到公式重算后仍丢时，从"只 warning"升级为"audit_log 留痕 + 环境开关"。
> 假设纠正：原"CLI 写后直接 save 不触发重算"错——`_save_with_cache_check:217` 已全程接通（write_cell:599/append_row:641 都调）。真正缺口是 `needs_manual_fix=True` 时只 warning 不上报（agent 层 grep 0 匹配，信息死在 CLICallResult.final）。
> 接线设计：CLI 层懒加载 BackupAuditor 记 audit（非 agent 层消费，改动集中 cli_interface.py，agent 层不动）。
> 报告：`tests/reports/formula_guard_latest.{json,md}`

- [x] A1. `_save_with_cache_check` needs_manual_fix 分支调 `auditor.record(operation='formula_loss_detected', extra={cache_message, gate, replayed_comments, still_lost_comments})`（用 record 非 backup_and_record，不触发文件备份；BackupAuditor 懒加载）。
- [ ] A2. agent 层 `_run_set`/`_run_add` 消费 `CLICallResult.needs_manual_fix`（留第二波，GATE=hold 阻断 + pre_commit_hold 通道时一起接）。
- [x] A3. 环境开关 `CODEMAKER_FORMULA_GATE=on|off|hold`（默认 on=warning+audit；off=静默；hold=warning+audit，阻断留第二波）。
- [x] A4. 测试 `tests/test_formula_gate.py` 5 用例（mock snapshot_before+validate_and_fix 聚焦 audit 逻辑）：无公式 no-audit / GATE=on audit / GATE=off 静默 / GATE=hold audit / needs=False no-audit。
- [x] A5. 跑 `test_formula_gate.py`(5) + 回归 `test_formula_cache`(6)+`test_comment_guard`(5)+`test_save_cache_scope`(3)+`test_write_verification`(5)+`test_fast_apply_eligible`(7) → 31 passed 零回归。
- [x] A6. 报告 `tests/reports/formula_guard_latest.{json,md}`。

**验证指标**：needs_manual_fix=True audit 覆盖率 100%（on/hold）；off 静默率 100%；31 测试零回归。

### 第二波 · 方法 B Pre-flight hold ✅ 已落地首版（R9-B1）

> 目标：apply 前预检"合并此 patch 将丢失哪些 base id"，命中阻断 + audit 留痕 + SSE 事件产出。
> 首版范围：后端漏行预检 + `CODEMAKER_PREFLIGHT_HOLD` 开关阻断 + audit + SSE 函数就位。前端拦截卡 + agent serve + stage3 留后续。
> 接线点调整：原计划改 `merge_stages._validate_apply_refs` 返回，实际改为在 `merge_branch.py` apply 路径写盘前直接调 `preflight_row_manifest`（写前阻断 + 写后 ref_report warning 两套并存，默认 off 零回归）。
> 报告：`tests/reports/preflight_hold_latest.{json,md}`

- [x] B1. 新建 `server/routers/precommit_hold.py`：`PreCommitHoldEvent` + `PreflightReport` + `preflight_row_manifest` + `record_hold_audit` + `emit_hold_sse`。
- [x] B2. `preflight_row_manifest(mr, base_pks)`：`lost_ids = base_ids - mergeset_ids`，复用 `collect_disk_sheet_pks(ours_path)` 落盘 base 文件全量主键集（MergeRequest 不含 base 原始行数据）。
- [ ] B3. 改 `_validate_apply_refs` 返回加 holds/preflight（未做，改为 apply 路径直接调 preflight，两套并存；后续若要统一可再做）。
- [x] B4. SSE 事件 `pre_commit_hold` 产出函数 `emit_hold_sse` 就位（复用 `_compare_task_emit` pattern，前端无消费分支待接）。
- [ ] B5. override 时 audit_log 单独记 `operation=pre_commit_hold_override`（首版用 `audit_tables.preflight_holds` 字段记，单独 operation 留前端接时做）。
- [x] B6. 测试 `tests/test_merge_preflight.py` 7 用例（无漏行/漏行命中/§2.3.1 id=10500/多 sheet/base_pks 空/event to_dict/可序列化）。
- [x] B7. 跑 `test_merge_preflight`(7) + 回归 `test_merge_eval`+`test_merge_formula_cache`+`test_merge_progress_snapshot`+`test_formula_gate`(5)+`test_comment_guard`(5)+`test_formula_cache`(6) → 38 passed 零回归。
- [x] B8. 报告 `tests/reports/preflight_hold_latest.{json,md}`（独立报告，未合并 merge_eval_latest）。

**验证指标**：§2.3.1 漏行命中率 100%；on 模式阻断率 100%；off 零回归；38 测试零回归。
**留后续**：前端拦截卡（MergeGuideView.vue + index-*.js minified 需重建）/ agent serve `_step_sink` 加 etype 分支 / stage3 apply 盲区覆盖。

### 第二波 · 方法 C PATCH_CONFIG 守门 ✅ 已落地首版（R9-C1）

> 目标：apply 前校验 §3.3 五坑（sheet 名对不上 / 忘登记 / PATCH_CONFIG 缺失 / _capped 加 CONFIG / 同名重复），全硬规则 hold。
> 首版范围：5 坑硬规则校验函数 + 独立 POST 路由 + 8 单测。apply 路径接入 + 脱敏样例留后续。
> 报告：`tests/reports/patch_validator_latest.{json,md}`

- [x] C1. 新建 `server/engine/patch_validator.py`：`Violation` + `validate_capped_workbook(path, trunk_sheet_names) -> list[Violation]`，5 条硬规则全 hold 级（规则③命中提前返回）。
- [ ] C2. `_validate_apply_refs` 内补 `validate_capped_workbook` 调用（留后续，需确认 apply 何时写 _capped vs 全量表）。
- [x] C3. `server/routers/validate.py` 加 `POST /api/validate/capped` 路由（CappedValidateRequest {path, trunk_sheets}）。
- [ ] C4. 脱敏样例 `resources/sample_capped.xlsx`（未放，测试用 tmp_path 自构造避免污染脱敏目录）。
- [x] C5. 测试 `tests/test_patch_validator.py` 8 用例（合规/坑4/坑3/坑1/坑5/§3.2 非法方式/无 trunk 跳过①/to_dict 可序列化）。
- [x] C6. 跑 `test_patch_validator`(8) + 回归 53 → 61 passed 零回归。
- [x] C7. 报告 `tests/reports/patch_validator_latest.{json,md}`。

**验证指标**：5 坑全覆盖；8 单测全过；61 测试零回归。
**留后续**：C2 apply 接入 / C4 脱敏样例 / 违规接 pre_commit_hold（kind=patch_config）复用方法 B 通道。

### 第二波 · 方法 A/D 升级（接 hold 通道）

- [ ] AD1. 方法 A 的 `CODEMAKER_FORMULA_GATE=hold` 模式接入 `pre_commit_hold`（kind=`formula_loss`）。
- [ ] AD2. 方法 D 的二次做差非_commit_hold`（kind=`comment_loss`）。
- [ ] AD3. 升级测试断言 hold 事件命中。

### 第三波 · 方法 F 编号账本跨分支

> 目标：扩 `id_scope` 支持多 SVN 分支 root，查跨分支编号冲突。
> 接线点：`server/engine/id_scope.py`。

- [ ] F1. 新增 `validate_multi_branch(branch_roots: list[Path]) -> CrossBranchReport`：遍历每分支根 + trunk，对每 id 记 `{id, table, branch_origin, rev}`，输出 `{cross_branch_conflicts, reserved_segments}`。
- [ ] F2. 改 `get_id_scope_validator()` 支持多 root，向后兼容单 root。
- [ ] F3. `routers/validate.py` `GET /api/validate/id-scope` 加 `?mode=multibranch` + 新增 `GET /api/validate/id-claim?id=xxx`。
- [ ] F4. agent.py:1929 `_validate_id_scope` 前调 id-claim，冲突 → `pre_commit_hold`（kind=`id_conflict`）+ 建议下一空闲号（复用 `id_resolver.py:100 split`），不静默改编号。
- [ ] F5. 写测试 `tests/test_id_ref.py` 扩 2 用例（单分支同 id 冲突 / 多分支同 id 跨目录冲突）。
- [ ] F6. 跑 `test_id_ref.py` + 回归。
- [ ] F7. 报告写入 `tests/reports/skill_ab_latest.json`（id_scope 属 skill 体系）。

**验证指标**：单分支/多分支冲突命中率 100%；零回归。

### 第三波 · 方法 E Schema 血缘联动

> 目标：trunk CONFIG 加列后，预检哪些 _capped.xlsx / ca/dev/* 同名表缺此列。
> 接线点：新建 `server/engine/column_lineage.py` + `server/routers/structural.py:52`。

- [ ] E1. 新建 `server/engine/column_lineage.py`：`compute_column_lineage(resources_dir) -> ColumnLineageGraph`，扫所有 CONFIG sheet 列定义行，合成 `{table -> {sheet -> {column -> {type, not_empty, present_in_capped, present_in_dev}}}}`。schema 复用 `schema_infer.scan_sheet:218`。
- [ ] E2. `structural.py:52 compute_structural_changes` 增 `kind="column_added"` / `kind="column_changed"` 检测（对比 `header_names` 增量）。
- [ ] E3. 新增路由 `POST /api/structural/sync_preview?table=xxx&sheet=yyy` → 返回缺列文件清单。
- [ ] E4. `core/agent.py` _capped 写入前调本预检，缺列 → `pre_commit_hold`（kind=`structure_sync_missing`）。
- [ ] E5. 写测试 `tests/test_column_lineage.py`：构造"trunk 加 1 列，capped header 不变"样例（conftest autouse fixture 加临时 xlsx），断言 sync_preview 命中。
- [ ] E6. 跑测试 + 回归。
- [ ] E7. 报告 `tests/reports/skill_ab_latest.json` 补充段。

**验证指标**：加列联动预检命中率 100%；零回归。

### 第四波 · 方法 G 阶段一 BM25 RAG

> 目标：复用 `search_blob` + jieba 做 BM25 召回，提升 TableLocator 命中率。
> 接线点：新建 `server/agent/excel/rag_searcher.py` + `table_locator.py:85-244`。

- [ ] G1. 新建 `server/agent/excel/rag_searcher.py`：`bm25_search(query, top_k=5) -> list[Hit]`，对 `_table_index.json` 每条 `search_blob` 做 jieba 分词 + BM25 排序，返回 `{path, sheet, score, matched_terms}`。
- [ ] G2. `TableLocator.locate` 前置加 BM25 召回层：候选 = 规则候选 ∪ BM25 top-K（去重）。
- [ ] G3. `dialog_failures/*.jsonl` 建索引：DecomposeAgent/Validator prompt 注入 K 条同表名同意图失败案例作 few-shot。
- [ ] G4. 环境开关 `CODEMAKER_RAG_MODE=bm25|vector|off`，默认 bm25。
- [ ] G5. 写测试 `tests/test_rag_failure_recall.py`：用 `dialog_failures/` 真实样本，断言召回 top-K 命中相关失败 ≥ 70%。
- [ ] G6. 扩 `tests/skill_ab_test.py` 加 BM25 召回 A/B 评估（定位成功率基线 1.0，不退化）。
- [ ] G7. 跑测试 + 回归。
- [ ] G8. 报告 `tests/reports/rag_recall_latest.json`。

**验证指标**：失败案例召回 top-K 相关命中率 ≥ 70%；skill_ab 定位成功率零回归（1.0）。

### 第四波 · 方法 H 多 Agent 对抗网

> 目标：Red/Blue/Auditor 对抗，主动找新失败模式，AuditorAgent 纯规则终判。
> 接线点：`subagent/` 新建 3 agent + `pipeline/pipeline.py:594 _step5_verify` + `core/agent.py:3860` CRUD 链。

- [ ] H1. 新建 `subagent/red_team_agent.py`（RedTeamAgent，继承 LLMSubAgent）、`blue_team_agent.py`（BlueTeamAgent）、`auditor_agent.py`（AuditorAgent，纯规则）。
- [ ] H2. `pipeline.py:594 _step5_verify` 在 `PipelineVerifier.verify` 后追加 RedTeam（输入 fragments + produced + §5.1 P0/P1/P2 痛点清单，输出 attack_surface）→ BlueTeam 自证（30s 超时）→ AuditorAgent 纯规则终判。
- [ ] H3. `core/agent.py:3860-3894` CRUD 链加 Red→Blue 后置，层 2 能力（events/hooks）避免双跑 LLM。
- [ ] H4. 简单输入经 complexity gate 跳过 Red（扩 `_is_complex_input`）。
- [ ] H5. 全程用 `_isolated_empty_dir()`（base.py:35）空目录隔离，RAG 喂养文本不让 serve 读项目文件（§R7 教训）。
- [ ] H6. 写测试 `tests/adv_vuln_test.py` 复用 `skill_ab_test.py` A/B 框架：构造 6 类攻击向量（§2.3/§2.5/§3.3/§3.4 各一），断言 Red 命中 ≥ 1 类被 Auditor 拦截。
- [ ] H7. 跑测试 + 回归。
- [ ] H8. 报告 `tests/reports/adv_vuln_latest.json`。

**验证指标**：6 类攻击向量 Red 命中 + Auditor 拦截 ≥ 1 类；零回归。

---

## 四、生产环境迁移注意点（1041 表 / 7.9G）

testtest 是脱敏样本（83 表），机制验证通后切真 ca 目录时：

1. `id_scope.validate_multi_branch` 的 `branch_roots` 换 `ca/dev/*/` + `ca/cappedbranch/` + `ca/testbranch/` + `ca/` 根。
2. `column_lineage.compute_column_lineage` 全量扫 ca/，预计算哈希缓存避免每次启动扫 7.9G（§1.5 最大表 calendar-festival.xlsx 8.4MB、item.xlsx 3.4MB）。
3. 方法 A 真部署时 LibreOffice soffice 必须装（testtest 已配但 PATH 可能 fallback）；§7 提示 officecli 候选——若上 officecli 共享 resident 重算需 per-file mutex。
4. 方法 B 真分支目录对接 svn export（R6 parallel_map_tables + svn 已实现），merge_branch.py LCA 提取机制不变，只在 hold 决策点插 lost_ids 计算。
5. 所有 `pre_commit_hold` SSE 事件复用现成 `step_sink`，前端 MergeGuideView.vue 新增红 card（仿 batchAdoptHighConfidence/sortByConflict pattern）。

---

## 五、每轮交付物

每波次完成后更新 `docs/优化全过程.md` R{n} 记录 + 仪表盘。落盘报告：

| 波次 | 报告路径 |
|---|---|
| 第一波 D | `tests/reports/comment_guard_latest.{json,md}` |
| 第一波 A | `tests/reports/formula_guard_latest.{json,md}` |
| 第二波 B | `tests/reports/merge_eval_latest.md` 加"preflight hold"段 |
| 第二波 C | `tests/reports/patch_validator_latest.md` |
| 第三波 E/F | `tests/reports/skill_ab_latest.json` 补充段 |
| 第四波 G | `tests/reports/rag_recall_latest.json` |
| 第四波 H | `tests/reports/adv_vuln_latest.json` |

零回归基线：`conftest.py:28 _reset_skill_caches` autouse 清缓存；`table_case_eval.py:215 diff_sandbox` 真值比对可直接断言"AI 写表后 xlsx 实际增量符合预期"。

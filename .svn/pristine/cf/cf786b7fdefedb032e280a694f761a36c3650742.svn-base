# Excel-Agent 四步循环架构落地任务

> 对应设计文档：`docs/excel-agent-4step-loop-design.md`（§三 算法 / §四 D1-D8 决策 / §六 4 阶段迁移计划）
> 任务进度：全部 `- [ ]`（未启动）。每完成一项勾选；如阻塞，在末尾 `## 7 阻塞与备注` 记根因。
> 任务排序按 §十 #5 优先级：阶段 1 → 阶段 3 → 阶段 2 → 阶段 4。

---

## 0. 落地进度摘要（2026-08-17 更新）

> 本节汇总实际落地进度 + 与任务文档的偏差 + 阻塞项。原 `## 1-9` 的 `- [ ]` 保留供后续逐项勾选，
> 实际完成状态以本节为准。对应代码改动见 `server/agent/configuration.py` /
> `server/agent/excel/parse_agent.py`(新) / `server/agent/excel/parser/nl_parser.py` /
> `server/agent/excel/subagent/validator_agent.py` / `server/agent/excel/core/agent.py` /
> `server/services/agent_service.py` / `server/routers/agent.py`。

### 0.1 已完成（代码 + 单测）

| 阶段 | 任务 | 关键文件 | 单测 |
|---|---|---|---|
| §1.1 | 配置开关 6 字段（enable_4step_loop/schema_driven_decompose/schema_fetch_concurrency/schema_fetch_sheet_limit/splitter_decompose_threshold/execute_no_llm） | `server/agent/configuration.py` | test_4step_config 18 |
| §1.2 | fast-path 阈值 env 化（CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD 默认 2） | `core/agent.py:3899-3907` | 同上 |
| §1.3 | 配置单测 | `tests/test_4step_config.py` | 18 |
| §2.1 | ParseAgent 类 + 粗路由（复用 LocatorAgent.locate） | `excel/parse_agent.py`(新) | test_parse_agent 17 |
| §2.4-2.5 | schema-driven LLM 拆分 + 响应解析（复用 DecomposeAgent.decompose） | 同上 | 同上 |
| §2.7 | produces 推断（复用 produces_inference.infer_produces_consumes） | 同上 | 同上 |
| §2.8 | splitter_baseline 兜底（parse_baseline 方法） | 同上 | 同上 |
| §2.9 | NLIntent 扩展（路线 A：produces_label/consumes_labels/source/ai_check_skipped/validation/execution + ValidationResult/ExecutionResult/Issue/IssueType/assemble_tips） | `parser/nl_parser.py` | test_parse_agent + test_validate_agent_two_layer |
| §2.10 | 接入 agent.py Step1 入口（_4step_parsed 排除 detect+三 agent 链+_llm_chain_decompose） | `core/agent.py:3834+` | 193 回归 |
| §2.11 | ParseAgent 单测（mock 三组件，17 用例） | `tests/test_parse_agent.py` | 17 |
| §3.1 | execute_no_llm 开关 + 失败直接进 failures（跳 LLM 诊断/重试） | `core/agent.py:5487` + `configuration.py` | test_execute_agent 10 |
| §3.3-3.6 | 占位符断言/拓扑派发/resolved 同步/失败不阻塞同层（现状已落地，文档化） | `core/agent.py` 现有 | test_multi_table_orchestration 覆盖 |
| §3.7 | ExecuteAgent 单测（10 用例） | `tests/test_execute_agent.py` | 10 |
| §4.1 ①② | validate_field_layer 列存在性 + 类型 coerce（简化 int/float/bool/string + 占位符软跳过） | `subagent/validator_agent.py` | test_validate_agent_two_layer 22 |
| §4.1 ③ | 必填性（required_fields.yaml 加载 + MISSING_REQUIRED | 同上 | +5 |
| §4.2 | validate_fk_layer（拓扑序 _topo_order + FORWARD_REF_BROKEN） | 同上 | +6 |
| §4.4 | IssueType 枚举 + Issue dataclass + assemble_tips | `parser/nl_parser.py` | 同上 |
| §4.5 | ask_user 交互反问接口 + set_ask_callback + agent_service 注入 _ask_callback 到 validator | `validator_agent.py` + `agent_service.py:1981/2013` | +5 |
| §4.6/4.7 | validate_two_layer 整合（字段层+FK层+ask_user+重校+splitter_baseline 跳 LLM） | `validator_agent.py` | +6 |
| §5.1 | _phase_summarize 失败路径扩全量 failures（多失败聚合 + table/sheet/col + attempted_strategies list/tuple 兼容） | `core/agent.py:6679` | test_conclude_agent 14 |
| §5.4 | done_data failures payload（routers/agent.py SSE 加 failures 键） | `routers/agent.py:256` | 同上 |

**累计 259 测试零回归**（test_4step_config 18 + test_parse_agent 17 + test_execute_agent 10 + test_validate_agent_two_layer 45 + test_conclude_agent 14 + parse_layer/decompose/subagent_roles/multi_table_orchestration/ai_intent_check_fallback/message_consistency 155）。

### 0.2 现状已落地（文档化，不改代码）

| 任务 | 现状位置 |
|---|---|
| §3.2 verify-repair 解耦抽文件 | 未抽文件（大重构留后续）；execute_no_llm=1 时跳 verify-repair LLM |
| §3.3 占位符断言 | `_phase_execute:5355-5444`（_classify_placeholder_fields + _has_unresolved_placeholder） |
| §3.4 拓扑派发 | run() step5 循环调 `OperationOrchestrator._topo_order`(4136) + `_resolve_placeholders`(4286) + `_capture_produced`(4336) |
| §3.5 resolved 同步 | `produced` dict（4192/4286/4336，label→pk） |
| §3.6 失败不阻塞同层 | `_blocked_by`(4267-4282) + `broken_producers`(4306-4332) + G8 链回滚 |
| §5.2 连通校验 opt-in | `_check_dangling_fk_refs`(6430) + `CODEMAKER_CONNECTIVITY_DEEP_CHECK=0` 默认关 |
| §5.3 自学习触发 | `skill_updater.induce_anti_patterns`(663) + 生产调用 `agent.py:6364`(`CODEMAKER_INDUCE_PROD=0` 默认关) + `promote_with_guard`(792) + `anti_patterns.yaml` pending_review |
| §5.6 mini_regression | `MINI_REGRESSION_SAMPLE=30` + `CODEMAKER_SKIP_REGRESSION=1` 默认跳过 + `TABLE_CASE_EVAL_RUNNING` 保持 pending_review |

### 0.3 偏差与待后续

| 偏差 | 说明 |
|---|---|
| §2.2 lazy schema 拉取 | 任务假设 HTTP `?include_columns=1` + ThreadPool + schema_bundle；实际 `DecomposeAgent._build_schema_block` 用本地 `cli.read_header`（非 HTTP）。复用现状，HTTP 化 + 独立 schema_bundle 待 R21 接口落地 |
| §2.3 _suggest_cache 复用 | DecomposeAgent._build_schema_block 不走该缓存，待后续优化 |
| §2.6 列名校验 | 归 §4.1 ① 列存在性（validate_field_layer 已做）+ column_matcher 重映射留 Step2 字段层 |
| §4.1 ④⑤⑥ | 唯一性/枚举白名单/范围分布需表数据 + agent helpers（run_semantic_gate/_precoerce_enum_value 是 agent 方法），validator 无 path 解析；完整实现需接口重构（加 path + agent 引用 或 schema_getter 扩展返表数据+enum_set） |
| §3.2 verify-repair 解耦 | 大重构（264 行主循环 + 8 helper 抽到 verify_repair_loop.py），留后续 |
| 路径修正 | configuration.py 实际在 `server/agent/configuration.py`（非 excel/configuration.py）；NLIntent 在 `parser/nl_parser.py`（非 codemaker_parser.py）；produces_inference 在 `core/produces_inference.py`；validator 在 `subagent/validator_agent.py` |

### 0.4 阻塞项

- **§1.4 / §2.12 / §3.8 / §4.9 / §5.5 端到端 A/B + e2e**：依赖 codemaker serve（R7 serve 端 143.8k token/156s 卡死，`excel-agent问题与优化方向.md` §6.3 已定位非 excel-agent 代码 bug）。待 serve 侧根治（关 auto-context / 提供纯文本补全端点 / 非 agentic 通道）。

### 0.5 新增 env 开关清单（全部默认 opt-in 灰度，不破坏现状）

| env | 默认 | 作用 |
|---|---|---|
| `CODEMAKER_4STEP_LOOP` | 0 | =1 时 run() 走 ParseAgent 主导（合并老 Step1/2/3） |
| `CODEMAKER_SCHEMADRIVEN_DECOMPOSE` | 0 | =1 时 ParseAgent 主导 schema 注入 LLM 拆分 |
| `CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD` | 2 | cross_intents_nl 长度 < 此值触发 _llm_chain_decompose；99 强制 DecomposeAgent 接管 |
| `CODEMAKER_SCHEMA_FETCH_CONCURRENCY` | 8 | schema 拉取 ThreadPool 并发上限（待 §2.2 HTTP 化后生效） |
| `CODEMAKER_SCHEMA_FETCH_SHEET_LIMIT` | 15 | schema 拉取候选 sheet 总数上限 |
| `CODEMAKER_EXECUTE_NO_LLM` | 0 | =1 时 _phase_execute 失败路径跳 verify-repair + D3 retry LLM，失败直接进 failures（交 §5 ConcludeAgent 诊断） |

### 0.6 后续落地（2026-08-18 更新）

| 任务 | 落地 | 关键文件 | 测试 |
|---|---|---|---|
| §4.1 ④⑤⑥ 字段层扩展 | data_getter 注入（方案 B）+ ⑤枚举白名单（enum_set + `_check_enum_whitelist` 纯函数 fallback）+ ④唯一性（existing_values）+ ⑥范围分布（modify only + `run_semantic_gate` 纯函数 + result_rows/vc/cli/path）。validate_field_layer + validate_two_layer 加 data_getter 参数 | `subagent/validator_agent.py` | test_validate_agent_two_layer +8（累计 53） |
| §3.2 verify-repair 解耦最小版 | `check_type_constraint` 纯函数抽到 `repair/verify_repair_loop.py` + agent 薄转发；循环主体（464 行+8 helper）文档化待后续大重构（execute_no_llm=1 已跳 verify-repair LLM） | `repair/verify_repair_loop.py`(新) + `core/agent.py:5680` | test_verify_repair_loop 14 |
| §2.2 HTTP schema_bundle | `build_data_getter` 构造器（cli 直读替代 HTTP，同进程更快）+ `_stem_to_path`/`_existing_values_from_rows`/`_rows_to_dicts` helpers；R21 HTTP API 已落地供独立部署用 | `excel/schema_bundle.py`(新) | test_schema_bundle 27 |
| stage 4-Step 前端打印 | 后端 `_STAGE_ORDER`/`_STAGE_TITLES` 加 `_4STEP` 切换（4-Step：s1_parse/s2_validate/s3_execute/s4_summary）+ `_stage_for_thinking`/`_stage_for_step` 4-Step 映射分支；前端 `stageNo` 双 order（order4+order6）+ `stageTotal` 动态分母 + L686 `/6`→`stageTotal()` | `services/agent_service.py:2043+` + `frontend/src/views/AgentChatView.vue:1077+` | 前端 vite build ✓ |
| validator 接入 4-Step 路径 | ParseAgent 产出后调 `validate_two_layer`（字段层 6 项+FK 拓扑层+ask_user 交互反问+skipped 过滤）；schema_getter 用 `_stem_to_path`+`cli.read_header/read_type_row`，data_getter 用 `schema_bundle.build_data_getter` | `core/agent.py:3858+` | 267 回归 |

**§4 字段层完整 6 项落地**：①列存在 ②类型 coerce ③必填 ④唯一 ⑤枚举 ⑥范围分布。

**§3.2/§2.2/validator 剩余**：
- §3.2 完整版：`_run_verify_repair_loop` 循环主体（agent.py:6109-6372, 464 行）+ 8 helper 抽到 verify_repair_loop.py，大重构高风险，待后续
- §2.2 HTTP 化：excel-agent 独立服务部署时改 build_data_getter 走 HTTP GET 替代 cli.read_header/read_sheet
- 6 步路径补传 validate_two_layer：需 SplitIntent→NLIntent 适配，让默认关（enable_4step_loop=0）也跑新字段层 6 项，待后续
- ExecuteAgent 跳 skipped：_phase_execute 加 validation.skipped 检查跳写盘（4-Step 路径过滤已做，单 intent 跳检查留后续）

**累计 323+ 测试零回归**（7 单测文件：test_4step_config 18 + test_parse_agent 17 + test_execute_agent 10 + test_validate_agent_two_layer 53 + test_conclude_agent 14 + test_verify_repair_loop 14 + test_schema_bundle 27 + 现有回归 170）。前端 vite build ✓。

---

## 1. 环境准备与开关（前置，1 天）

- [ ] 1.1 在 `server/agent/excel/configuration.py` 新增配置项：`enable_4step_loop`（默认 `0` 灰度）、`schema_driven_decompose`（默认 `0`）、`schema_fetch_concurrency`（默认 `8`）、`schema_fetch_sheet_limit`（默认 `15`）。确认配置加载链路（`configuration.py → TableAgent / OrchestratorAgent`）能读取，关闭时退回当前 6 步 pipeline。
- [ ] 1.2 在 `server/agent/excel/core/agent.py:3899-3904` 把 splitter fast-path 阈值 `<2` 改 env 可调：读 `CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD`（默认 2，调到 99 即强制 DecomposeAgent 接管所有命中 fast_path 的输入）；不改默认行为（保持 `<2` 落 DecomposeAgent）。
- [ ] 1.3 单测 `server/tests/test_4step_config.py`：`enable_4step_loop=0` 时 `agent.run()` 走原 6 步；`=1` 时进入 ParseAgent 入口；阈值 env 生效。
- [ ] 1.4 A/B 基线快照：`table_case_eval.py --no-raise` 跑全量 30 用例，归档 before 报告到 `server/tests/reports/archive/4step_baseline_before.*`（§十六 #36 待办，本任务同时启动该基线表）。

---

## 2. 阶段 1：ParseAgent 落地（§3.1，P0，深 1 周）

- [ ] 2.1 新建 `server/agent/excel/parse_agent.py`：定义 `ParseAgent` 类 + `parse(input, ctx) -> list[SubTask]`；实现 §3.1 step 2（粗路由，零 LLM）。粗路由复用 `index_builder_hints.yaml`（stem→域）+ `alias_mapping.json`（`AliasMapping.autogenerate()` baseline，#9 已落地）；输出候选 stem 集合 S（典型 5-7 个）。
- [ ] 2.2 实现 §3.1 step 3 lazy schema 拉取（零 LLM，ThreadPool 并发 `schema_fetch_concurrency`）：对每个 stem 调 `GET /api/tables/{stem}` 取 sheet 列表 + 行数，对行数 >0 的非程序 sheet 调 `GET /api/tables/{stem}/sheets/{sheet}?include_columns=1`（R21，一次拿数据 + 列约束）；候选 sheet 总数 ≤ `schema_fetch_sheet_limit`。结果整合 `schema_bundle: dict[(stem,sheet), dict]`。
- [ ] 2.3 schema 拉取缓存：复用 `agent_service._suggest_cache`（OrderedDict 2000 条 / TTL 1h / LRU move_to_end，#11 已落地）；key = `(stem, sheet, columns_version)`；命中跳过 HTTP。`conftest autouse` 清缓存保测试隔离。
- [ ] 2.4 实现 §3.1 step 4 schema-driven 拆分（LLM 1-2 次）：复用 `decompose_agent.DecomposeAgent._build_schema_block`（#13 token 裁剪 `CODEMAKER_DECOMPOSE_SCHEMA_SHEETS`/`_COLS`）注入 schema_bundle + `table_relations.json` FK 图（R8b）；prompt 明确"列名必须用 schema 真实列名、不得臆造、未知列标 `<unknown_col:name_hint>`、本批产新 ID 标 `<produces:label>`、引用者写 `<consume:label>`、引用已存在行填字面值"。
- [ ] 2.5 LLM 响应解析：复用 `decompose_agent._parse_json_array` 单 dict fallback（#12）；LIST 包装 + JSON parse；失败重试指数退避（#15 `CODEMAKER_DECOMPOSE_RETRY_BACKOFF`）+ per-stem 并发上限 `CODEMAKER_DECOMPOSE_WORKERS`。
- [ ] 2.6 列名校验：拆出的 fields 列名必须在 `schema_bundle[(stem,sheet)].columns_meta`；未知名走 `column_matcher._make_matcher` BoW 重映射（#41 row2 alias 桥接，type_aliases 合并进 yaml_aliases 已落地 #45）；仍失败留 `<unknown_col:_>` 并入 SubTask.validation.issues。
- [ ] 2.7 实现 §3.1 step 5 produces 推断：调 `produces_inference.infer_produces_consumes(sub_tasks, table_relations)`（R8b 已落地）；显式 PK 字面代换（#44，`produces_inference._explicit_pk_literal_substitute`）；填回 SubTask.produces_label / consumes_labels。
- [ ] 2.8 兜底分支：LLM 拆分完全失败（空响应或解析失败）→ 回退 `cross_table_splitter.py` 11 模板 baseline 产出，`source="splitter_baseline"`；不影响主路径。
- [ ] 2.9 SubTask 结构落 field：路线 A 扩现有 `NLIntent`，在 `server/agent/excel/codemaker_parser.py` 的 `NLIntent` dataclass 加字段 `produces_label: str|None` / `consumes_labels: list[str]` / `validation: ValidationResult|None` / `execution: ExecutionResult|None` / `source: Literal["llm_decompose","splitter_baseline"] = "nl"`；保留旧字段不破坏 #22/#25 保护语义。`parse_agent` 末尾把 SubTask 适配注入 NLIntent。
- [ ] 2.10 接入 `core/agent.py` Step1 入口：`enable_4step_loop=1` 时 `run()` 走 ParseAgent；`=0` 走原 `_phase_parse` / `parse_multi` 路径。
- [ ] 2.11 单测 `server/tests/test_parse_agent.py`：
  - 单元：mock schema_bundle 验证拆分产 8+ SubTask（取 `table_operation_test_cases.json` 中"灵兽饲养员老李"NPC + 对话 3 层 + spawn 4 表链），断言列名全为真实列、无幻觉列。
  - 阶段目标用例：「封印魔龙」输入要求产 ≥8 个 SubTask（QuestGroup/Quest/combat_data/PveCombatNpc/Reward/GameplayAbilityChoicePool/entity_prefab/Interaction+InteractionConv+InteractionConvOption/spawn_world_entity ×3-7），覆盖 §3.1 全算法。
  - 兜底：mock LLM 返空时回退 splitter_baseline，SubTask.source 标记正确。
- [ ] 2.12 端到端 A/B：`table_case_eval --quick 5 --set CODEMAKER_4STEP_LOOP=1`，要求 locate ≥ 0.80、cov ≥ 0.75（vs 4.4 before 0.24）；不达标回 §2.4 prompt 调优，不上 §3。

---

## 3. 阶段 3：ExecuteAgent 去 LLM（§3.3，P1，深 1 天，须 §2 完成后启动）

- [ ] 3.1 抽空 `server/agent/excel/core/agent.py:_phase_execute`（约 L3754+）中的 LLM 调用点；唯一保留的诊断 LLM 移至 §5 ConcludeAgent。验证通过验证后零 LLM。
- [ ] 3.2 verify-repair loop 解耦：把 `_run_verify_repair_loop` 抽到新文件 `server/agent/excel/verify_repair_loop.py`，作为 ExecuteAgent 失败路径的可选分支（默认 `CODEMAKER_VERIFY_REPAIR_LOOP=0` 关，§agent-verify-repair-loop 工程师统一推；本任务保持默认关）。
- [ ] 3.3 占位符代换：执行前扫 `SubTask.fields` 中 `<consume:label>` 必须已被 §2.7 produces_inference 替换或在拓扑序前序已产出 `resolved_placeholders[label]`；未替换直接 `assert` + warning（复用 #21 placeholder 断言），不进 LLM。
- [ ] 3.4 拓扑序派发：复用 `OperationOrchestrator._topo_order`（Kahn + produces_inference，R8b 已落地）→ producerr 先于 consumer；add 走 R23 二段 dry_run→commit；modify 走 R22 atomic batch-update；delete 走 R24 row/delete；单 cell set 走 R21 cell/update。
- [ ] 3.5 resolved_placeholders 同步：add commit 成功后写 `resolved[label] = commit.new_row_pk`，下游 consumer 立即可读到字面值代入。
- [ ] 3.6 失败不阻塞同层独立子任务：拓扑序每层内部并行 dispatch（同层 no-DAG 依赖）；下游依存者必须 jump-skip，append 到 `failures[]`（结构化 #40）。
- [ ] 3.7 单测 `server/tests/test_execute_agent.py`：
  - mock dispatch：8 SubTask 拓扑序执行产 8 个 row_pk，无 LLM 调用（mock 计数 0）。
  - 失败：第 3 个 producer 失败时第 4 个 consumer 跳过且入 failures[1]；同层独立 producer 5–8 仍执行。
- [ ] 3.8 端到端 A/B：「封印魔龙」单条墙钟从 600s+ → <60s（§八 §3.3 全去 LLM 后弹性收益最大）。

---

## 4. 阶段 2：ValidateAgent 两段式 + 交互默认开（§3.2，P0，深 3 天）

- [ ] 4.1 在 `server/agent/excel/validator_agent.py` 新增 `validate_field_layer(subtasks, schema_bundle, parallel=True) -> dict[subtask_id, list[Issue]]`：字段层并行校验（ThreadPool size=8），每子任务独立做：① 列存在性 ② 类型 coerce（复用 `_coerce_value`，#15）③ 必填性（`required_fields.yaml` #30 空占位，index 非空列自动派生待 §6) ④ 唯一性（GET sheet data 搜该列）⑤ 枚举白名单（type_aliases row2）⑥ 范围/分布 only for modify（复用 `semantic_gate.run_semantic_gate`，#21，add 跳分布离群）。零 LLM。
- [ ] 4.2 在 `validator_agent.py` 新增 `validate_fk_layer(subtasks, table_relations) -> dict[subtask_id, list[Issue]]`：先 `OperationOrchestrator._topo_order` 拓扑序推进 `produced = {}`，每 SubTask 字段中 `<consume:label>` 必须在 produced（前向引用否则 issue）；FK 列字面值调 `_validate_forward_refs_llm`（#19 opt-in `CODEMAKER_VALIDATOR_LLM_FORWARD_REFS`，default off）；产出后写 `produced[label] = (stem, sheet, pk)`。
- [ ] 4.3 "L"形流水优化：topo 序推进时 `produced` 即时填充，允许 producer 一落地下游 consumer 并行校验；避免完全串行的延迟。
- [ ] 4.4 tips 序列化：把字段层 + FK 层 issues 整合为 `[{subtask_id, col, issue_type, expected, suggestion}]`；issue_type 枚举 `missing_required / type_mismatch / unique_violation / enum_invalid / forward_ref_broken / range_outlier`。
- [ ] 4.5 交互反问接入：复用 `agent_service._ask_callback`（#39，default on，`CODEMAKER_INTERACTIVE_REPAIR=1`，#41 已落地），ValidateAgent 通过 `_ask_callback` 发 `ask` SSE 事件 + 等 `reply_queue`（每 0.5s 轮询 + cancel 兼容），用户回复 mode=field/nl/skip 应用修订。
- [ ] 4.6 重校循环：用户回复后回 §4.1 字段层（+如需要走 §4.2 FK 层）；any issue 不 ok 反问；skipped 项标 `validation.ok=True` 标记跳过让下游 ExecuteAgent 跳过写盘。
- [ ] 4.7 splitter_baseline 子任务仍走 #25 跳过 LLM validate：在 §4.1 字段层仍跑规则校验，但跳 §4.2 LLM FK 裁决；用 SubTask.source=="splitter_baseline" 判别。
- [ ] 4.8 单测 `server/tests/test_validate_agent_two_layer.py`：
  - 字段层全过、FK 层前向引用命中 → 通过；
  - 字段层 unique_violation → issue + tips 序列化正确；
  - FK 字面值前向引用 → opt-in 时 LLM 裁决 build/exists/typo 三态 mock 验证；
  - splitter_baseline 子任务跳 FK LLM 路径但走规则字段层。
- [ ] 4.9 端到端 e2e 测试 `server/tests/e2e_interactive_repair.py`：构造 1 条"漏字段"用例（删 combat 的 model_id）+ 1 条"悬空 FK"用例（quest 引未建 npc_ids）；验证前端 `AgentChatView.vue` ask-card 弹出 → `replyAsk` POST `/reply`；reply_queue put 成功后增量续跑覆盖缺失项（§agent-verify-repair-loop 已搭 SSE+ask event，本任务补全 e2e）。

---

## 5. 阶段 4：ConcludeAgent + 自学习闭闭环（§3.4，P2，深 2 天）

- [ ] 5.1 在 `server/agent/excel/core/agent.py:_phase_summarize`（已扩 #38/#40）整合 4-Step 末尾汇总：success_list + failure_list（含 type/table/col/root_cause/attempted/suggestion，D2 #40 schema）+ all_ok。LLM 1 次（隔离 session，`CODEMAKER_AI_ENHANCER_ISOLATE_CONTEXT=1` 默认开，#26 已避免 R7 拖累）。
- [ ] 5.2 跨表连通性深度校验 opt-in：复用 `_check_dangling_fk_refs`（#17，CODEMAKER_CONNECTIVITY_DEEP_CHECK=1）选通到失败列表并 emitted to前端失败块。
- [ ] 5.3 自学习触发：failures 非空时调 `skill_updater.induce_anti_patterns(failed_traces, ai_enhancer)`（R2 #6 已落地）：LLM 1 次归纳 → 候选写入 `anti_patterns.yaml status=pending_review`；走 `promote_with_guard` mini_regression 通过才升 active；eval 环境（`TABLE_CASE_EVAL_RUNNING`）保持 pending_review（不放宽门禁）。
- [ ] 5.4 失败项结构化：`ServerResult.failures: list[dict]` 已是 #40 字段；接入 `AgentChatResponse.failures` done payload 让前端 `AgentChatView.vue` 渲染失败块（前端 UI 块待加）。
- [ ] 5.5 e2e 测试 `server/tests/e2e_conclude_self_learn.py`：注入 1 条"reward_id 未建"失败 trace → 验证归纳产出反模式候选写入 yaml；下次相同输入触发 lookup 反模式命中（精确匹配优先）→ 反问而非直接写盘。
- [ ] 5.6 mini_regression：promote_with_guard 跑 ≥5 用例不回归才升 active；self-updater 定时任务（cron）扫 pending_review 升 active；如配置 cron 不可用则人工 promote 入口（写运维说明到 `tools/README.md`）。

---

## 6. 全量回归与归档

- [ ] 6.1 全量重跑 `table_case_eval.py --no-raise` 跑 30 用例基线，对比 §1.4 before 报告：要求 4-Step 全开时 locate ≥ 0.80、cov ≥ 0.75、acc ≥ 0.75、pass ≥ 0.40、平均耗时降幅 ≥ 60%（vs before 0.80/0.24/0.36/0.33）。
- [ ] 6.2 `skill_ab_test.py` 扩展对比 4step=on/off，要求 locate/cov 不退化。
- [ ] 6.3 跑现有 `table_operation_test_cases.json`（46 用例）+ `task_chain.json`（10 用例）端到端；fast-path（`CODEMAKER_SPLITTER_FAST_PATH=1` + `enable_4step_loop=0`）行为不变（254 单测通过）。
- [ ] 6.4 单测套件回归：`uv run pytest server/tests/test_parse_agent.py server/tests/test_validate_agent_two_layer.py server/tests/test_execute_agent.py server/tests/e2e_interactive_repair.py server/tests/e2e_conclude_self_learn.py server/tests/test_4step_config.py` 全过。
- [ ] 6.5 文档收尾：
  - 更新 `docs/TABLE_MODE.md` 与 `tools/README.md` 补 `CODEMAKER_4STEP_LOOP` / `CODEMAKER_SCHEMADRIVEN_DECOMPOSE` 开关，运维延时特性（4-Step 默认灰度 → 全量上线门控）。
  - 更新 `TODO_OPTIMIZATION.md` §四 #9/#24、§三 #6 阶段 2、§十八 #38/#39 状态：移至 ✅，备注"经 4-Step Loop 架构落地解决"。
- [ ] 6.6 OpenSpec archive：建 `openspec/changes/excel-agent-4step-loop/` 目录树（.openspec.yaml / proposal.md / design.md / tasks.md / specs/{schema-driven-decompose,two-layer-validate,deterministic-execute,conclude-self-learn}/spec.md），copy `docs/excel-agent-4step-loop-design.md` 内容到 design.md，本文档 copy 到 tasks.md，跑 `openspec verify-change`，通过后 archive。

---

## 7. 阻塞与备注

> 实施中遇到理解偏差 / 设计变更 / 上游依赖未就绪时记录在此，便于同步进度与决策追溯。

- [ ] 7.1 （待启动后填）schema_fetch_concurrency 上限的本地压力实测：阶段 1 完成后实测 8 并发 GET 对 FastAPI / libreoffice 计算压力，如热 CPU 调到 4。
- [ ] 7.2 （待启动后填）`table_relations.json` 覆盖完备审计：阶段 1 推进中验证 SpawnQuestEntity→quest FK、PveCombatNpc→spell FK 等边是否齐全；缺则补 §五 #24"table 关键词并入 alias_mapping"作旁路。
- [ ] 7.3 （待启动后填）ParseAgent LLM 拆分单 prompt vs per-stem 多 prompt：§十 #3 决策点，阶段 1 实测候选 stem 数分布后定（≤4 单 prompt，>4 按分组并行）。
- [ ] 7.4 （待启动后填）pause-resume 增量续跑能力：当前 §4.5 仅"全量重 parse + skip 已成功项"，真 pause-resume 不在本期范围；如交互频繁触发卡顿，启动 §十 #4 后续版本。

---

## 8. 任务执行约定

1. 严格按 §序 1 → 2 → 3 → 4 → 5 → 6 顺序，§3 阶段 3 须于 §2 阶段 1 完成后启动（依赖 schema-grounded SubTask）。
2. 每个阶段完成后必跑 §6 对应子任务回归，不达标回本阶段调优，不进下一阶段。
3. 所有新增模块单测覆盖率 ≥70%；新增 env 开关默认 opt-in（防回归）。
4. 落地代码遵循 §五代码映射表（复用现有模块，不重写）；如改字段必须保留旧 #22/#25/#41 保护语义不变。
5. 每勾选一项前自检：① 是否真实改了代码 ② 是否跑了对应单测 ③ Q 不回归。

---

## 9. 深度发展路线 TODO（Agent 增强方法对齐）

> 对应设计文档 `docs/excel-agent-4step-loop-design.md` §十一。本节是 4-Step 之上的纵深演进 TODO，与 §2~§5 储备并行；落地须在 §6 全量回归基线建立之后；默认全部 env=0 opt-in，不削弱 §6 主路径。

### 9.1 Plan-Execute 显式化（replan-on-failure，P0-1）
- [ ] 9.1.1 新增 `server/agent/excel/replan_agent.py`：`ReplanAgent.replan(goal, failures, remaining_subtasks) -> list[SubTask]`；输入 §3 ExecuteAgent failures + remaining，输出修订 SubTask（复用 `OperationOrchestrator._topo_order` 重排 + `produces_inference`）；LLM ≤1 次。
- [ ] 9.1.2 `core/agent.py:_phase_execute` 失败分支接 replan 循环（上限 N=2），门控 `CODEMAKER_REPLAN_ON_FAILURE=0`（默认关）。
- [ ] 9.1.3 单测 `server/tests/test_replan_agent.py`：mock producer 失败 → replan 把 consumer 改为"补建"或"跳过 + 标 forward_ref"；不与 verify-repair 重复升 LLM。
- [ ] 9.1.4 A/B：构造"中途失败"用例，对比直接上报 vs replan，要求最终 success_count 提升。
- [ ] 9.1.5 openspec：建 `openspec/changes/replan-on-failure/` 提案，`openspec verify-change` 通过后 archive。

### 9.2 Multi-Agent 动态编排 + Reviewer 闭环（P0-2）
- [ ] 9.2.1 `OrchestratorAgent` 之上新增 `RoleDispatcher`：按 `SubTask.action/table_stem` 动态选 `engine_core/roles` 4 Fill 角色（替代固定串行派发）。
- [ ] 9.2.2 复用 `validator_agent` 扩 `ReviewerAgent`：对 ExecuteAgent 写盘结果做规则 + LLM 审查；不通过回 ExecuteAgent 修，门控 `CODEMAKER_REVIEW_LOOP=0`、`CODEMAKER_REVIEW_MAX_ROUNDS=3`。
- [ ] 9.2.3 单测 `server/tests/test_reviewer_loop.py`：mock 写盘缺字段 → Reviewer 标 issue → 回修 → 第 2 轮通过；超 max_rounds 走 §5 ConcludeAgent 上报。
- [ ] 9.2.4 openspec：建 `openspec/changes/multi-agent-review-loop/` 提案。

### 9.3 三层记忆系统（P1-1）
- [ ] 9.3.1 短期：`server/agent/state.py` 的 `AgentState` 扩 `history: list[dict]` + 接 LangGraph `checkpointer`（MemorySaver→可选 SQLite）支持 pause-resume（呼应 §十一 #4）。
- [ ] 9.3.2 工作记忆：把 `list[SubTask]` 链固化为 `AgentState.working_memory`，跨节点透传。
- [ ] 9.3.3 长期向量：选型 pgvector 或 sqlite-vec；实现 `LongTermMemory.store/recall/forget`；把 `anti_patterns.yaml` 迁为"可解释兜底 + 向量召回"双路（yaml 保留作审计/可解释）。
- [ ] 9.3.4 单测 `server/tests/test_long_term_memory.py`：mock 写成功模式入库 → 下次相似输入召回 → 召回命中率 ≥0.8；不活跃记忆遗忘生效。
- [ ] 9.3.5 门控 `CODEMAKER_LONG_TERM_MEMORY=0`（向量库依赖评审后开）。
- [ ] 9.3.6 openspec：建 `openspec/changes/three-layer-memory/` 提案。

### 9.4 ToolRegistry + 风险分级 + 审批流（P1-2）
- [ ] 9.4.1 新增 `server/agent/excel/tool_registry.py`：`ToolRegistry.register(name, func, group, risk_level)`，三态 `safe/write/dangerous`；`get_safe_tools()/get_dangerous_tools()`。
- [ ] 9.4.2 `tools.py:make_skill_tools` 迁移为注册表项；写类 tool 标 write、删多行 / 原始 SQL 标 dangerous。
- [ ] 9.4.3 危险工具审批流：复用 `_ask_callback`（§D6 #39/#41 通道）暂停等人工确认；CI 自动化 `CODEMAKER_INTERACTIVE_REPAIR=0` 走拒绝。
- [ ] 9.4.4 单测 `server/tests/test_tool_registry.py`：safe 自动跑、write 返 proposal、dangerous 必审批；三级逃生断言。
- [ ] 9.4.5 openspec：建 `openspec/changes/tool-registry-risk-level/` 提案。

### 9.5 成本控制：预算驱动模型分层（P1-3）
- [ ] 9.5.1 配置层支持多 model（`CODEMAKER_MODEL_HEAVY` / `CODEMAKER_MODEL_LIGHT`），`CodemakerClient` 按 model 名路由（确认 codemaker serve 支持多模型）。
- [ ] 9.5.2 新增 `server/agent/excel/cost_tracker.py`：`CostTracker(budget)` + `suggest_model(task_complexity, spent)`：分类/校验走 light、拆分/审查走 heavy；预算 >70% 自动降挡。
- [ ] 9.5.3 复用 `llm_counter` 累计花费做预算闭环；单测 `server/tests/test_cost_tracker.py`：超预算自动降挡 + 报警。
- [ ] 9.5.4 openspec：建 `openspec/changes/budget-model-tiering/` 提案。

### 9.6 安全沙箱 + 聚合监控（P2）
- [ ] 9.6.1 执行层写盘 subprocess 包 `docker run --read-only --network=none`（或 WASM）；保留 processpool 降级路径；单测沙箱逃逸回归。
- [ ] 9.6.2 新增 `server/agent/excel/agent_metrics.py`：`AgentMetrics.report()` 产出 tool_success_rate / llm_calls / budget_left / 熔断次数；`step_sink` 推前端 + `server/tests/reports` 落盘。
- [ ] 9.6.3 单测 `server/tests/test_agent_metrics.py`；复用 `test_processpool_isolation.py` 作沙箱降级回归。
- [ ] 9.6.4 openspec：建 `openspec/changes/security-sandbox-metrics/` 提案。

### 9.7 收敛约定
1. 9.1~9.6 全部默认 `=0` opt-in，不削弱 §6 主路径；每项勾选前跑 §6.4 回归 + 专项单测。
2. 与 `openspec` 规范一致：每项落地前建 `openspec/changes/<feature>/`（proposal/design/tasks/specs），`openspec verify-change` 通过后 archive。
3. 与 §设计文档 §D4 一致性：本路线禁止把"执行阶段现场 LLM 推理"作为主路径；仅 replan/reviewer/记忆召回作增量 LLM 触发点，且均默认关。

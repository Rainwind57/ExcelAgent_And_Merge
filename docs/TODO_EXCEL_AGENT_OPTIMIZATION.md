# Excel-Agent 优化 TODO

> 基线：4-Step V2 全链已走通（case2/3/5/6 ok=true），6 项提效已落地（schema 缓存+并发4+段裁剪+dry_run 跳 PK+7 步收口+分段兜底）。
> 本文记录后续优化点，按「投入产出比 × 落地可行性」排序，暂不实施。

---

## 一、根因层（最该先做）

### TODO-1: 修粗路由候选池 cap 绕过 bug + 动词规则 + alias 覆盖错

**症状**：case1（封印魔龙）漏 interaction/spawn_world_entity，导致对话树+刷新实体整块缺失。

**根因（3 处）**：
1. **cap 绕过 bug** — `locator_agent.py:222-226`，当 `len(strong) >= cap`（≥8 候选全 ≥0.80 置信度）时，`_cand_cap - len(strong)` 为负 → `max(0,·)=0` → `candidates = strong` 全留，cap 完全绕过。日志"保留 13"= 13 个 strong 全留，cap 没生效。
2. **interaction 从 FK 扩表只 0.50 进 weak 桶** — `locator_agent.py:365`，FK 扩表的 interaction/spawn 置信度低（0.50/0.40），裁剪时落 weak 桶被挤掉。
3. **alias_mapping.json 错覆盖** — `alias_mapping.json:59 "交互"→building.xlsx` 覆盖 autogen 的"交互→interaction.xlsx"（`load()` line 123 `merged.update(json_map)`），导致通词"交互"路由到 building 非 interaction。"对话/选项/弹/坐标/放在/刷"在 alias_mapping.json 完全无别名。

**改动**：
- `locator_agent.py:222` — strong 超 cap 时也截断（或改 weak 保留逻辑，保证 cap 生效）
- `locator_agent.py:218` 前 — 加动词 regex map（小改）：
  - 「弹|对话|选项|交互」→ interaction.xlsx
  - 「放在|刷|坐标|生成」→ spawn_world_entity.xlsx
  - conf ≥0.80 进 strong 桶，过 cap 保护
- `alias_mapping.json:59` — 删/改 `"交互": "building.xlsx"` 错条目（autogen 会补"交互→interaction"）
- 裁剪时给 `level in ("column_extract","column_reverse","alias")` 加权（来源已标，小改）

**alias_mapping.json 生成机制确认**：
- `alias_mapping.py:76 autogenerate()` 每次 load 动态生成 baseline（从 `index_builder_hints.yaml` 的 `stem_to_domain` + resources/ 扫描）
- `load()` line 111：base = autogenerate()，line 123：`merged.update(json_map)` → **json 覆盖 autogen**
- **结论**：json 是静态手工覆盖文件，autogen 不生成"交互→building"（yaml 里 `interaction: 交互`），错条目是 json 手工错加 → **直接改 json 即可**，SVN 版本管理后续提交
- 权威别名源是 `skills/index_builder_hints.yaml` 的 `stem_to_domain`（人工维护该 yaml），动词规则若要持久化应加进该 yaml 或独立动词映射表

**改动量**：小（bug fix + regex map + json 删一行）
**验收**：粗路由阶段产出候选必须含 interaction + spawn_world_entity，否则注定丢对话树。

---

### TODO-2: 治 R7 serve hang 上游阻塞（agent 层可改部分）

**症状**：DecomposeAgent 并发 8 候选全空响应/超时 → 直接降级单 prompt。

**根因**：serve 端 auto-context 自动文件读取致单 prompt 慢/空响应（非本仓范围）。agent 层"前 2 个失败即取消剩余"太激进。

**改动（agent 层，非 serve）**：
- `decompose_agent.py:_decompose_parallel` fail-fast 策略改「分批探测 + 各候选独立重试指数退避」
  - 当前：前 2 候选均空 → cancel 全部 → 降级单 prompt
  - 改：前 2 失败只 cancel 同批，剩余批次独立重试（指数退避 `CODEMAKER_DECOMPOSE_RETRY_BACKOFF`），不连坐
- serve 端三选一（非本仓，需用户介入）：
  - ① serve 端关 auto-context 自动文件读取
  - ② 提供纯文本补全端点（不走 agentic 通道）
  - ③ excel-agent 独立部署脱离 serve 拖累

**改动量**：agent 层小改（fail-fast 策略）；serve 端需用户介入
**约束**：不改 serve 侧代码（非本仓范围）

---

## 二、兜底层（serve 没修好也能扛住）

### TODO-3: 增强单 prompt 兜底的完整性门控

**症状**：单 prompt 兜底产 8 条意图就交差，interaction/spawn 整块漏掉。

**改动**：
- 单 prompt 兜底前后置「必出意图集合」完整性检查
- 对照粗路由命中的表集合（`locator_result.candidates` 的 stems）
- 若 DecomposeAgent 产出表集合 ⊊ 命中表集合（如漏了 interaction/spawn），触发第 2 轮专门补缺 prompt（"刚才漏了 X 表，请补充"）
- 两轮合并再交 Step2

**落地点**：`decompose_agent.py:decompose()` 主流程末尾（我已加的兜底链后），加命中表 diff 检查
**改动量**：小（增量增强，不依赖 serve）
**与已改 6 项关系**：增强我已改的「分段单 prompt 兜底」，补命中表 diff 门控

---

## 三、校验层（漏表/漏字段拦截不到的硬伤）

### TODO-4: 意图级完整性预检

**症状**：当前 Step2 只校验「已有意图的字段」，无法识别"应该有但没拆出来的意图"——entity_prefab 的 interaction_id 列空，是因为 interaction 意图根本没产出来。

**可行性确认**：`validate_two_layer` 已收 `locator_result`（agent.py:5166 传 `locator_result=locator_result`），可拿候选表集合。

**改动**：
- `validator_agent.py:validate_two_layer` 前加 `intent_coverage_precheck`
- 对照「输入明确点名的表集合」（`locator_result.candidates` 的 stems）vs「SubTask 表集合」（intents 的 table_hint）
- 差异超阈值反问用户「是否需要配置 interaction/spawn_world_entity」，否则不下发执行

**落地点**：`validator_agent.py:validate_two_layer` 入口（line 645 后）
**改动量**：中（需设计反问交互，但 validate_two_layer 已有 ask 机制）

---

### TODO-5: 字段覆盖率门控（补 required_fields.yaml entity_prefab 配置）

**症状**：两个 prefab 只写了 model_prefab + 名字 2 列，entity_class=WorldNonPlayer 和 interaction_id 都缺失，竟 step2_validate 通过。

**可行性确认**（认知纠正）：
- `required_fields.yaml` **非空（已填）**，用户说"当前空"是过时认知
- 结构：`{required_fields: {stem: {sheet: [aliases]}}}`（file header line 1-5）
- entity_prefab: Base sheet 当前只配 `[编号, model_prefab]` — **entity_class/interaction_id 确不在配置** → 残缺没拦下
- `MISSING_REQUIRED` 已在 `_hard_issue_types`（validator_agent.py:894）→ 阻断

**改动**：
- `skills/L1_derived/required_fields.yaml` — entity_prefab/Base 补 `entity_class`、`interaction_id` 到必填
- validator 复用现有 `validate_field_layer` ③必填性检查（line 409-423 已加载 yaml + 查 MISSING_REQUIRED），补配置即可生效
- 可选：add 意图执行前做「核心字段覆盖率门控」，阈值低于 60% 直接进 failures 不写盘或反问

**落地点**：`skills/L1_derived/required_fields.yaml` 配置补充（小改）
**改动量**：小（补 yaml 配置，validator 逻辑现成）
**alias_mapping 生成机制**：required_fields.yaml 是静态手工文件，直接改，SVN 管理

---

### TODO-6: 悬空 FK 检测移写前（认知纠正：默认已开但跑写prefab 的 interaction_id 列空 = 断链，但写盘照做。

**可行性确认**（认知纠正）：
- `_check_dangling_fk_refs`（agent.py:7904）+ `_fk_target_row_exists`（agent.py:7983）
- 开关 `CODEMAKER_CONNECTIVITY_DEEP_CHECK` 默认**"1"（已开）**，用户说"默认关 =0"是过时认知
- 但跑在**写后 Step6**（agent.py:5646-5708），**非写前 Step2**
- `FORWARD_REF_BROKEN`（validate_two_layer line 722，占位符悬空）与 dangling FK（指向行存在性）**不同**，非重复

**改动**：
- 可选：dangling FK 检测从写后移到写前 Step2（风险中，可能双 ask）
- 或：保持写后兜底（默认已开），接受写后才发现残废行→修复

**改动量**：中（移写前需处理与 FORWARD_REF_BROKEN 的双 ask 风险）
**优先级**：低于 TODO-4/5（写后已兜底，移写前收益有限）

---

## 四、汇总层（事后兜底）

### TODO-7: Step4 反向核对 + 自学习闭环

**症状**：Step4 step4_conclude 通过得太快，没察觉漏了 4 个表。

**可行性确认**：
- 自学习闭环**已建成**：`skill_updater.py:733 induce_anti_patterns` + `promote_with_guard:863` + `promote_pending_anti_patterns:1057` 全齐；Step4 已接（`step4_conclude_subagent.py:90-91` 调 `_collect_failed_traces` + `_induce_anti_patterns_via`）
- 反模式运行时消费：`engine_core/verifier.py:101 _check_anti_patterns` + `core/skill_loader.py:400-465 load_anti_patterns`（trigger_pattern 关键词匹配）→ 闭环反向匹配生效路径存在
- `anti_patterns.yaml` 结构：含 id/type/table_stem/sheet/trigger/occurrences/first_seen/last_seen/action/status/source/trigger_pattern/rationale

**未落地部分**：「输入关键词命中表集合 vs 写盘表集合」反向 diff
- `step4_conclude` 和 `_phase_summarize` 都只读 Step3 的 subtasks/failures 聚合，**没做反向比对**
- 命中表信息在 Step1（`covered_stems`，agent.py:4924/4968）有收集，但**未结构化下发 Step4**
- Step4 当前只收 `s3.artifacts["subtasks"]`，**缺 Step1 命中表上下文**

**改动**：
- 扩 Step4 契约传 Step1 命中表上下文（`covered_stems`）
- `_phase_summarize`/`step4_conclude` 加「输入命中表 vs 写盘表」反向 diff
- 差异超阈值在失败清单列「疑似漏配：interaction/spawn_world_entity」
- 把「意图级遗漏」trace 经 `skill_updater.induce_anti_patterns` 沉淀到 anti_patterns.yaml

**落地点**：`core/pipeline/step4_conclude_subagent.py` + `core/agent.py:_phase_summarize` + 契约扩参
**改动量**：中（反向 diff 需新写 + 扩 Step4 契约传参）
**风险**：anti_patterns.yaml 有三份路径（core/skills/、skills/、core/skills/_pending/），确认权威路径防双写冲突；induce 需 LLM（step4 内 try/except 降级，失败不阻断）

---

## 五、架构开关层

### TODO-8: 灰度开 schema-driven 拆分（认知纠正：开关空转）

**症状**：CODEMAKER_SCHEMADRIVEN_DECOMPOSE=0 默认关，设计文档要求开。

**可行性确认**（认知纠正）：
- `server/agent/configuration.py:57-58` 定义 `schema_driven_decompose`（默认关）
- 但 `grep` 全 `server/` 确认：该字段**仅被 `test_4step_config.py` 读取，agent 运行时零消费**（空转，设计残留）
- 真正控制"LLM 为主 vs splitter 为主"的旋钮：
  - `CODEMAKER_AGENT_CHAIN`（agent.py:4923）
  - `CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD`（agent.py:5050，默认 2，评论 5049 明说"调到 99 即强制 DecomposeAgent 接管"）
- `schema_fetch_concurrency/limit`（D2 lazy schema 拉取）同为空转——DecomposeAgent 已实现 schema 注入（`_build_schema_block`），但走 `_cli.read_header/read_type_row` 直读 xlsx，不走该 env 控制的 HTTP lazy 拉取

**改动**：
- env `CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD=99` + `CODEMAKER_AGENT_CHAIN=1` 即可灰度 =1 语义
- 建议接线 elim 双份漂移（Configuration 字段与 env 两套并存）
- 设计文档 §6 阶段1 验收门控 cov≥0.75 **无对应运行时代码**，只存在于评估脚本范式（`table_case_eval.py` A/B 跑全量用例的验收阈值），非内嵌门控

**改动量**：低（env 触发即可）
**风险**：threshold=99 时 LLM 链强制接管，超时慢+字段与 env 双漂；LLM 不稳靠三层兜底（`_splitter_baseline` + 分段单 prompt + splitter 11 模板）
**前置依赖**：不依赖 serve 稳定、不依赖 schema_bundle HTTP；依赖 LLM 可用性 + 兜底链
**优先级**：低于 TODO-1~5（当前兜底链已足够处理复杂多意图，灰度开主要提准确率非完整性）

---

## 六、大数据表格优化（10w 行+）

### 现状

- 已有 `engine/fast_apply.py`（10w 行 XML 快路径，避免 openpyxl 全量 load+save ~13s）
- 已有 `python-calamine`（Rust rapidxml，10w 行 0.05s vs openpyxl 5.5s，100× 提速）在 `engine/parser.py:28` M7-3 优先用
- 已有 `engine/id_scope.py` M8 性能优化（单遍 `iter_rows(values_only=True)` 定位末数据行与提取 ID 列）
- 已有 `engine/merge_branch.py:1327` + `merge_subdir.py:621` 优先 calamine
- 已有 `formula/formula_cache_validator.py:229` zip XML 字节扫描 `<f`（10w 行 ~0.05s vs openpyxl 全量）

**短板**：Excel-agent 的 `cli.read_header`/`read_type_row`（`cli_interface.py:449` `_load` 走 `openpyxl.load_workbook`）对大表仍 openpyxl 全量解析；DecomposeAgent `_build_schema_block` schema 读走 cli 这层；`validate_two_layer` 的 `data_getter` PK 检查扫全表行。

### TODO-9: 大数据表格 schema 读改 calamine + 行索引

**症状**：10w 行表 DecomposeAgent schema 读 + validator PK 检查（`data_getter` 全表扫描）极慢。

**改动**：
- `cli_interface.py:read_header`/`read_type_row` — 大表（行数阈值，如 >1w）优先 calamine 读 row1+row2（只读值，不读公式/批注，schema 不需这些）
- `cli_interface.py:_load` — 大表 wb 缓存改 calamine（或保留 openpyxl 但 read_only 模式 + 不缓存 wb）
- `validator_agent.py:data_getter` PK 检查 — 走行级倒排索引（`locator/table_index.py` 已有 `iter_rows(min_row=data_start, values_only=True)` O(n) 单遍扫描），避免每 intent 全表扫
- 已改的 `_schema_cache`（DecomposeAgent）对大表单次读仍慢，需底层 calamine 提速

**落地点**：`cli_interface.py:read_header` + `validator_agent.py` data_getter 路径
**改动量**：中（calamine 集成 + data_getter 改索引查询）
**收益**：10w 行表 schema 读从 ~5.5s → ~0.05s，PK 检查从全表扫改索引查询

### TODO-10: DecomposeAgent schema 读大表走 row1+row2 仅读

**症状**：DecomposeAgent `_build_schema_block` 对每候选表每 sheet 读 row1+row2，但 openpyxl `load_workbook` 解析全 sheet。

**改动**：
- `_build_schema_block` 大表走 `read_only=True` + `iter_rows(min_row=1, max_row=2)`（只读前 2 行，openpyxl read_only 流式不全量解析）
- 或集成 calamine 只读 row1+row2（calamine 不支持 sheet 内行范围，需读全 sheet 再取前 2 行，但 Rust 解析 10w 行 0.05s 仍远快于 openpyxl）
- 已改的 `_schema_cache` 缓存命中后跳过，但首次读仍需提速

**落地点**：`decompose_agent.py:_read_schema_cached`（我已加）+ `cli_interface.py:read_header`
**改动量**：小（read_only 模式 + iter_rows 行范围）
**收益**：首次 schema 读大表从 ~5.5s → ~0.05s

### TODO-11: validator data_getter PK 检查走索引

**症状**：dry_run 我已跳 PK 检查（TODO-6 相关），但真执行路径 PK 检查调 `data_getter(intent)` 扫全表行索引，10w 行表慢。

**改动**：
- `data_getter`（`schema_bundle.py:build_data_getter`）PK 检查走行级倒排索引（`locator/table_index.py` 已有 O(n) 单遍扫描 + 索引）
- 避免每 intent 全表扫，索引命中 O(1) 查询

**落地点**：`schema_bundle.py:build_data_getter` + `validator_agent.py:_suggest_next_id`/PK 扫描段
**改动量**：中（data_getter 改索引查询，需确认索引覆盖所有 PK 列）
**收益**：PK 检查从 O(n) 全表扫 → O(1) 索引查询

---

## 优先级总表

| TODO | 层 | 改动量 | ROI | 说明 |
|------|----|--------|-----|------|
| 1 粗路由 cap+动词+alias | 根因 | 小 | ★★★ | 漏表根因，必修 |
| 2 serve hang agent 层 | 根因 | 小（agent）/ 用户介入（serve） | ★★ | agent 层 fail-fast 改 |
| 3 兜底完整性门控 | 兜底 | 小 | ★★ | 增强已改兜底 |
| 4 意图级完整性预检 | 校验 | 中 | ★★ | 漏表拦截 |
| 5 字段覆盖率门控 | 校验 | 小（补 yaml） | ★★ | 字段残缺根因 |
| 6 悬空 FK 移写前 | 校验 | 中 | ★ | 写后已兜底，移写前收益有限 |
| 7 Step4 反向核对 | 汇总 | 中 | ★ | 自学习已建成，反向 diff 未落地 |
| 8 SCHEMADRIVEN 灰度 | 架构 | 低（env） | ★ | 开关空转，env 触发 |
| 9 大表 schema calamine | 大表 | 中 | ★★★ | 10w 行 100× 提速 |
| 10 DecomposeAgent row1+2 读 | 大表 | 小 | ★★ | 首次 schema 读提速 |
| 11 data_getter 索引 | 大表 | 中 | ★★ | PK 检查提速 |

**建议执行顺序**：1 → 5 → 2 → 3 → 4 → 10 → 9 → 11 → 6 → 7 → 8

---

## 已落地基线（不重复改）

- 6 项提效（commit `79a7429`）：schema 缓存+并发4+段裁剪+dry_run 跳PK+7步收口+分段兜底
- 零 LLM 兜底链（commit `c02b77d`）：DecomposeAgent `_splitter_baseline` + dry_run accept callback + PK 放行
- case2/3/5/6 ok=true 全 4 步通过

## alias_mapping.json 生成机制总结

- `alias_mapping.py:autogenerate()` 每次 load 动态生成 baseline（从 `index_builder_hints.yaml` stem_to_domain + resources/ 扫描）
- `load()`：base = autogenerate()，`merged.update(json_map)` → json 覆盖 autogen
- json 是静态手工覆盖文件，autogen 不生成错条目 → 直接改 json，SVN 管理
- 权威别名源：`skills/index_builder_hints.yaml` 的 `stem_to_domain`（人工维护该 yaml）
- 动词规则持久化：加进 `index_builder_hints.yaml` 或独立动词映射表（非 json，json 是 alias→path 扁平 dict）

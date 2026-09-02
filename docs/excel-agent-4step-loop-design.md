# Excel-Agent 四步循环架构设计

> 定型日期：2026-08-17
> 来源：执行日志「封印魔龙」(6 表链输入)耗时 10+ 分钟、覆盖度严重不足、字段大量漏写的根因复盘与架构重构设计。
> 关联文档：`TODO_OPTIMIZATION.md`（§一/§三/§四/§十八）、`优化全过程.md`（R8g/R8b/#6/#38/#39/#41）、`openspec/changes/agent-verify-repair-loop/design.md`。
> 本文是「4-Step Loop」的最终详细设计；落地前需经 openspec/changes/ 正式提交（见 §七）。

---

## 一、背景与根因

### 1.1 触发场景

输入「加一个新的主线任务叫"封印魔龙"，任务号 250600，归到任务组 600（组名"封魔录"，主线类型），限定条件填 hero_all……战斗 25060001 也一起配上……奖励包 100600 建一下……再配一个任务引导 NPC 叫"封魔长老"……」共点名 8+ 张表（quest.QuestGroup/Quest + combat.combat_data/PveCombatNpc + reward.Reward + item.xlsx.GameplayAbilityChoicePool + entity_prefab.Base + interaction.Interaction/InteractionConv/InteractionConvOption + spawn_world_entity.SpawnWorldEntity）。

执行结果：墙钟 10+ 分钟，LLM 调用 23+ 次，最终仅写出 2 张表（combat 7 列残缺 / reward 6 列残缺），quest 主线、PveCombatNpc 怪物、掉落池、NPC 对话链**全丢**，且描述性子句被误拆成无 table_hint 的 set 意图，被误路由到 `city/building.xlsx` 的 BuildingType sheet，最后被 Step4 拒绝。

### 1.2 根因（按 Step 归因）

| Step | 现象 | 根因 | 对应 TODO 项 |
|---|---|---|---|
| Step1 | 只产 2 条意图（combat + reward），描述被独立成 set | `cross_table_splitter.combat_reward` 模板命中 fast-path（`agent.py:3899`），且 `len(cross_intents_nl)=2` 卡在 `<2` 边界之外（`agent.py:3904`），DecomposeAgent LLM 链分解未接管；模板字段表写死，多一列就漏 | §三 #6（撤 11 模板） |
| Step1 | AI 校验发现 4 条缺失意图 + 2 条字段映射建议，但被 `规则结果优先` 丢弃 | `agent.py` 处理 fallback 时把 AI 增强输出当提示不采纳 | §十八 #38/#39 |
| Step2 | 描述性 set 意图（无 table_hint）被路由到 `city`/building.xlsx | Locator 走关键词/别名兜底（`#24` level6 BoW 余弦兜底）反向帮倒忙 | §四 #24 |
| Step3 | 描述性意图被 AI 硬凑字段（`建筑名→魔龙巢穴`、`建筑类型填<auto>`） | 走了 ai_plan_operation，但没有真实 schema 作 ground truth | §三 #6 阶段 2 |
| Step4 | set 缺 locator_value 被拒 | 拦截本身是对的，但根源在 Step1 | — |
| Step5 | 写盘字段大量缺失（坐标/怪物名/model_id/技能列表/AI/气血斜率/必给道具/词条池） | 没有 schema-driven 拆分 → 字段不可能凭模板补全 | §三 #6 |
| Step6 | 全局汇总标"部分成功 2/4"，但失败清单仅报 Step4 一条，漏失意图项不进结构化 failures | `_phase_summarize` 只汇总执行失败，AI 校验遗漏不进默认失败清单 | §十八 #38/#40 |

**核心架构病灶**：**"先拆后定位"**——拆分器在不知道表真实列定义的情况下，凭 11 个手维护模板硬拆出意图，定位/列映射都只能是事后修补。

### 1.3 设计目标

把「先拆后定位」反转为**「先定位按列拆」**，并把当前 6 步线性 pipeline（Step1 parse → Step1.5 ai_check → Step2 分区 → Step3 计划 → Step4 校验 → Step5 应用 → Step6 汇总）整合为 **4-Step 循环**：

```
┌─────────────────────────────────────────────────────────────────────────┐
│ Step1  ParseAgent    ─ 合并老 Step1/Step2/Step3                          │
│   定位 → 拉真实列约束 → schema-driven 拆分 → produces 推断 → 组装 SubTask  │
├─────────────────────────────────────────────────────────────────────────┤
│ Step2  ValidateAgent ─ 强化老 Step4 + 加交互                              │
│   字段层并行校验 + 跨表 FK 拓扑校验 → tips → ask 用户 → 修正                │
├─────────────────────────────────────────────────────────────────────────┤
│ Step3  ExecuteAgent  ─ 老老 Step5（去 LLM）                              │
│   拓扑排序 → 确定化派发原子 API → 收集结果 + failures                      │
├─────────────────────────────────────────────────────────────────────────┤
│ Step4  ConcludeAgent ─ 老老 Step6 + 自学习                               │
│   汇总 + 失败清单 + skill_updater 归纳反模式                              │
└─────────────────────────────────────────────────────────────────────────┘
```

---

## 二、数据契约

整个 4-Step 流水线以 `SubTask` 结构化对象为主线流转，Step1 产出 → Step2 校验/修订 → Step3 执行 → Step4 归因。

```python
@dataclass
class SubTask:
    # 身份
    id: str                      # 子任务唯一 ID（uuid4）
    action: str                  # add | modify | delete | set
    table_stem: str              # resources/<stem>.xlsx → 路由产物
    sheet: str                   # sheet 名 → 路由产物
    raw_text: str                # 该子任务对应的原自然语言片段

    # 定位
    locator_column: str | None   # modify/delete 的定位列名（schema-grounded）
    locator_value: object | None # 定位值（contains 匹配）

    # 字段（schema-grounded，列名必须是真实列）
    fields: dict[str, object]    # {真实列名: 值 | "<produces_label>" | "<consume:label>"}
                                 # 值类型已 coerce 过；占位符走 produces_inference

    # 关系图（DecomposeAgent 产 + R8b produces_inference 补）
    produces_label: str | None   # 本子任务产出的新 ID 标签（被其他子任务 consumes）
    consumes_labels: list[str]   # 本子任务引用的 produces 标签列表

    # 来源与保护
    source: Literal["llm_decompose", "splitter_baseline"]
                                 # splitter_baseline 跳 Step3 AI plan、跳 Step2 LLM validate
    ai_check_skipped: bool       # 是否已锁定字段不进 AI 重映射（#22/#25 保护）

    # 校验附加（Step2 填充）
    validation: "ValidationResult | None"
    # 执行附加（Step3 填充）
    execution: "ExecutionResult | None"
```

> 与现有 `NLIntent` 结构的关系：`SubTask` 是 `NLIntent` 的"已 schema 化、已 plan 化"超集，承载定位/校验/执行结果。落地阶段可选两条路：(a) 扩 `NLIntent` 加新字段；(b) 引入 `SubTask` 作为下游结构，Step1 产出末尾 NLIntent→SubTask 适配器。倾向 (a)，避免双套结构。

---

## 三、各 Step 详细设计

### 3.1 Step1 — ParseAgent（合并老 Step1+Step2+Step3）

**职责**：把单条自然语言指令拆为 `list[SubTask]`，每个 SubTask 已 schema-grounded（列名是真实的、值已类型匹配）。

**算法**：

```
ParseAgentFlow(input):
  ┌─ 1. 分类（复用现有规则短路，零 LLM）
  │    classify(request) -> {qa, crud}
  └─ if qa → 走 qa_handler（不在本文范围）

  ┌─ 2. 粗路由（零 LLM，复用 #9 autogen alias + #24 level1-3）
  │    a. 读 index_builder_hints.yaml (stem→domain)
  │    b. 读 alias_mapping.json (autogen baseline + json 覆盖)
  │    c. regex/关键词扫输入（npc/quest/combat/reward/item/mail/pet/building/...）
  │    → 候选 stem 集合 S（典型 5-7 个）
  └─
  ┌─ 3. Lazy schema 拉取（零 LLM，并行 HTTP，ThreadPool size=8）
  │    a. 对每个 stem in S:  GET /api/tables/{stem}                      → sheet 列表 + 行数
  │    b. 对每个候选 (stem, sheet):
  │         GET /api/tables/{stem}/sheets/{sheet}?include_columns=1      → R21 一次拿数据+列约束
  │         （候选 sheet 通常 8-15 个；行数 >0 的非程序 sheet 才拉）
  │    c. 整合为 schema_bundle = {(stem, sheet): {columns_meta, columns, row1/row2 aliases}}
  └─ 并发上限 + LRU 缓存（现有 _suggest_cache TTL=1h 复用）

  ┌─ 4. Schema-driven 拆分（LLM，1-2 次，复用 decompose_agent._run_one + #13 token 裁剪）
  │    a. _build_schema_block(schema_bundle, table_relations.json FK 图):
  │         - 每 sheet ≤3 行 × ≤12 列（CODEMAKER_DECOMPOSE_SCHEMA_SHEETS/_COLS 可调）
  │         - 显式标注"哪些列指向他表新 ID"（FK 图注入，#14 提示）
  │    b. prompt:
  │         「拆分以下指令为 per-(stem,sheet) 的 add/modify/delete 子任务。
  │          ① 列名必须用 schema 真实列名，不得臆造。
  │          ② 无法映射的字段标 <unknown_col:name_hint>，由 column_matcher BoW 兜底（#41 row2 alias 桥接）。
  │          ③ 本批产出的新 ID 标 <produces:label>（你的命名，语义化如 "new_pet_id"）。
  │          ④ 引用本批其他产出 ID 的，写 <consume:label>。
  │          ⑤ 引用已存在行的，填字面值。」
  │    c. LLM 返回 JSON 数组 → 包装到 [dict] fallback（#12 单 dict fallback）→ 解析验证：
  │         - 列名必须在 schema_bundle 真实列中（拒绝幻觉列）
  │         - 未知名走 column_matcher 重映射，仍失败留 <unknown_col:_> 并记 issue
  │    d. splitter_baseline 兜底（不影响主路径）：如果 LLM 拆分完全失败，回退 cross_table_splitter 11 模板
  │       产出作为 baseline（仍打 source="splitter_baseline" 让下游知道走快路径）
  └─
  ┌─ 5. produces_inference 推断（零 LLM，复用 R8b infer_produces_consumes）
  │    - 对 add 子任务：若含 FK 列且未显式设 produces_label，自动分配（如 quest 产 quest_id→combat 产 combat_id→reward 产 reward_id）
  │    - 对 consumes 显式 PK 字面代换（#44）：producer 有字面 PK 直接代消费者 FK 字段
  └─
  ┌─ 6. 组装 SubTask → 输出 list[SubTask]
  │    - 每条 NLIntent → SubTask（装 schema 真实列名 + produces + source=llm_decompose）
  └─
```

**LLM 调用预算**：粗路由 0 次 + schema 拉取 0 次 + 拆分 1-2 次（候选 stem 多时按 stem 分组并行 `ThreadPoolExecutor`，每候选一 prompt，#15 指数退避）= **1-2 次**

**关键开关**：`CODEMAKER_SPLITTER_FAST_PATH=0`（强制不走 fast-path、走 schema-driven LLM 拆分）；新增 `CODEMAKER_SCHEMADRIVEN_DECOMPOSE=1`（灰度总开关，默认关，开启后 ParseAgent 主导）。

**兼容 fast-path 的设计**：splitter_baseline 模板产出的意图作 baseline 仍保留，但**仅当 LLM 拆分失败或返回 <1 intent 时才回退使用 baseline**，否则 LLM 产出为主——这与 §三 #6「阶段2：灰度替换单模板，验证不回归后删模板」一致。

---

### 3.2 Step2 — ValidateAgent（强化老 Step4 + 交互式）

**职责**：对 `list[SubTask]` 做写前校验，失败给 tips + 反问用户，输出修订后的 `list[SubTask]`。

**两段式校验（必分层，不能纯并行）**：

```
ValidateAgentFlow(sub_tasks):

  ┌─ A. 字段层（完全并行，ThreadPool，零 LLM）
  │    for each st in sub_tasks (parallel):
  │      schema = schema_bundle[st.table_stem, st.sheet]
  │      for col, val in st.fields:
  │        ① 列存在性：col ∈ schema.columns_meta（幻觉列已在 Step1 截下，这里二重门控）
  │        ② 类型 coerce：复用 _coerce_value（#15，int/float/bool/string/array）
  │        ③ 必填性：col ∈ schema.required_fields（#30 空 placeholder，后续 index 派生自动生成）
  │        ④ 唯一性：GET sheet data 搜该列是否已有值（唯一列约束）
  │        ⑤ 枚举白名单：col ∈ type_aliases.row2 enum set
  │        ⑥ 范围/分布（仅 modify）：复用 semantic_gate（#21，add 跳分布离群）
  │      → st.validation = {issues: [{col, type, expected, suggestion}], ok: bool}
  └─
  ┌─ B. FK/跨表引用层（拓扑序，#19 opt-in LLM 裁决，复用 validator_agent）
  │    topo = OperationOrchestrator._topo_order(sub_tasks)  # R8b 关系图驱动
  │    produced = {}  # label → (stem, sheet, pk_value)
  │    for st in topo:                                     # 顺序，因需 produced
  │      for col, val in st.fields:
  │        if val 是 <consume:label>:
  │          assert label in produced else issue(forward_ref_broken)
  │        elif col 是 FK 列 (table_relations.json 关系图):
  │          if val 是字面值:
  │            # 前向引用未在本批 produces
  │            LLM 裁决（_validate_forward_refs_llm，#19）：需补建 or 已存在或用户笔误
  │              → build/exists/typo 三态
  │      if st.produces_label: produced[label] = (st.stem, st.sheet, <produces_pk>)
  │    # 注意：FK 层不并行，但有"L"形优化：produces 集合在拓扑序推进时即时填充
  └─

  ┌─ C. 交互门控（如有 issues）
  │    if any st.validation.not_ok 或 forward_ref_broken:
  │      tips = assemble_tips(issues)  # {subtask_id, col, issue_type, expected, suggestion}
  │      emit ask_event(tips) via _ask_callback  # #39/#41 已落地
  │      reply = wait reply_queue (mode=field/nl/skip)
  │      apply_correction(sub_tasks, reply)
  │      goto A  # 重校（防错改）
  │    until all_ok or skip
  └─

  Output: validated list[SubTask]（fields 已修订）
```

**LLM 调用预算**：字段层 0 次 + FK 层 0-1 次（`CODEMAKER_VALIDATOR_LLM_FORWARD_REFS` opt-in）+ 未声明主键的启发式主键缺失判定 0-1 次（`CODEMAKER_VALIDATOR_LLM_PK_JUDGE` opt-in，经验证据不足时才触发，见 `_pk_inferred_downgrade`/`_llm_judge_pk_required`）+ 业务必填豁免二次判断 0-1 次/列（`CODEMAKER_VALIDATOR_LLM_BUSINESS_REQUIRED` opt-in，4 条硬编码豁免都没命中时才触发，见 `_llm_judge_business_required`）= **0-2+ 次**（业务必填按列触发，理论上限随缺失列数增长，实际场景通常 0-1 列）；另修复中文枚举标签→数字码推断（`_auto_resolve_enum`，默认开）在无交互回调（批量/CI）场景下完全触达不到的覆盖盲区，使其在非交互路径也生效（该调用不新增预算档位，属于修复既有默认能力的覆盖面，不是新增开关）。

**避免的反模式**：完全并行 FK 层（无法判前向引用是否本批补建）；完全串行字段层（不必要，损失并行的延迟收益）。

**关键约束**：splitter_baseline 子任务可继续走 #25 「跳 AI validate」（在字段层仍跑规则校验，只是跳 LLM），因为它们字段已被模板锁定；但 schema-driven 子任务必须走完整字段层 + FK 层。

---

### 3.3 Step3 — ExecuteAgent（老 Step5 去 LLM）

**职责**：把验证后的 `list[SubTask]` 拓扑排序后确定化派发到原子 API，收 results + failures。

**算法**：

```
ExecuteAgentFlow(validated_sub_tasks):

  ┌─ 1. 拓扑排序（零 LLM，复用 OperationOrchestrator）
  │    topo = _topo_order(sub_tasks)  # Kahn + produces_inference，R8b
  │    # 关键：producer 在 consumer 前执行，holds consumed <produces:label> 能被解析为字面值
  └─
  ┌─ 2. 派发循环（零 LLM，纯 API 调用）
  │    resolved = {}  # label → 写盘后的真实 ID/PK
  │    results = []
  │    failures = []
  │    for st in topo:
  │      try:
  │        for col, val in st.fields:
  │          if val 是 <consume:label>:
  │            st.fields[col] = resolved[label]  # 占位符字面代换
  │        if st.action == "add":
  │          # R23 二段提交
  │          build = POST /api/tables/add-form/build  {text: st.raw_text}
  │          validate = POST /api/tables/add-form/validate {table_stem, sheet, values: fields}
  │          preview = POST /api/tables/add-form/commit {dry_run: True}
  │          commit = POST /api/tables/add-form/commit {dry_run: False}  # 不 confirm，验证过
  │          resolved[st.produces_label] = commit.new_row_pk
  │        elif st.action == "modify":
  │          # R22 批量原子
  │          row = locate_row(st.locator_column, st.locator_value)  # GET sheet data 找行号
  │          POST /api/tables/cells/batch-update {atomic: True, updates: [...]}
  │        elif st.action == "delete":
  │          POST /api/tables/row/delete {table_stem, sheet, row}
  │        elif st.action == "set":  # 单格
  │          POST /api/tables/cell/update {table_stem, sheet, row, col, value}
  │        results.append({subtask_id, ok: True, row, written_fields})
  │      except HardError as e:
  │        failures.append({  # #40 结构化
  │          subtask_id, type: e.error_type, table: st.table_stem, sheet: st.sheet,
  │          col: e.failed_col, root_cause: e.reason,
  │          attempted_strategies: ["direct_dispatch"], suggestion: ...
  │        })
  │    # 失败不阻塞独立子任务（同 topo 层并行的可继续），但依存的下游子任务必须跳过
  └─
```

**LLM 调用预算**：**0 次**（坚决去 LLM——把刚省下来的延迟全部留给 Step1 的 schema 拆分）

**关键约束**：
- 不在本 Step 内做 verify-repair 循环（那是 §agent-verify-repair-loop 的职责，可叠加但非默认；默认子任务失败直接进 failures 由 Step4 上报）。
- 占位符 `<consume:label>` 必须在执行前替换为字面值；未替换是有 bug（断言 + warning，复用 #21 placeholder 断言）。

---

### 3.4 Step4 — ConcludeAgent + 自学习（老 Step6 + skill_updater）

**职责**：基于 results+failures 输出结构化汇总，并把失败 trace 经 `skill_updater` 归纳为反模式（pending_review → active），下次同类输入更准。

**算法**：

```
ConcludeAgentFlow(results, failures):

  ┌─ 1. 汇总（LLM，1 次，复用 _phase_summarize + #38 失败分支 + #40 结构化）
  │    summary = {
  │      success_count: len([r for r in results if r.ok]),
  │      success_list: [简表 each: stem/sheet/row/写盘字段],
  │      failure_list: [简表 each: stem/sheet/col/root_cause/attempted/suggestion],  # #40
  │      all_ok: len(failures) == 0,
  │    }
  │    prompt 模板：面向用户的"成功总结 + 失败清单 + 建议补全方案"，前端区分成功/失败块
  └─
  ┌─ 2. 跨表连通性深度校验（opt-in，#17，CODEMAKER_CONNECTIVITY_DEEP_CHECK=1）
  │    _check_dangling_fk_refs(results, relationships) → 补充到 failure_list
  └─
  ┌─ 3. 自学习（LLM，1 次，仅 failures 非空时，复用 skill_updater.induce_anti_patterns）
  │    candidates = induce_anti_patterns(failures, ai_enhancer)
  │    # 复用 R2 #6 已落地的 LLM 归纳
  │    for c in candidates:
  │      promote_with_guard(c)  # mini_regression 通过才升 active，eval 环境保持 pending_review
  │    # 写 anti_patterns.yaml（pending_review）
  └─
  Output: summary + new_anti_pattern_candidates (供人工/定时升 active)
```

**LLM 调用预算**：汇总 1 次 + 自学习 1 次（条件触发）= **1-2 次**

---

## 四、关键决策（D1-D8）

### D1: DecomposeAgent 主导，splitter 模板降为兜底 baseline

**选择**：`CODEMAKER_SPLITTER_FAST_PATH=0` 默认改为"仅 LLM 拆分失败才回退 splitter_baseline"。新增 `CODEMAKER_SCHEMADRIVEN_DECOMPOSE=1` 总开关，默认 opt-in 灰度推。
**理由**：本轮日志显示 splitter 产 `len(cross_intents_nl)=2` 卡在 `<2` 边界（`agent.py:3904`），DecomposeAgent 未接管是直接漏字段根因。Schema-driven 让 LLM 看真实列后再拆，单条 6-表链可产 8+ 个 SubTask。
**替代方案**：保留 fast-path 作主路径，仅在同义匹配失败时回退 LLM——这是当前代码，验证漏字段严重。
**校验点**：开启 `CODEMAKER_SCHEMADRIVEN_DECOMPOSE` 后跑 `table_case_eval.py` 全量 30 用例，要求 locate/cov/acc 不退化才合并。

### D2: Schema 拉取 lazy，不 eager

**选择**：仅对粗路由圈出的候选 stem（5-7 个）拉 `?include_columns=1`，候选 sheet 上限 15 个。
**理由**：64+ stem × 多 sheet 全拉是 IO/token 爆炸；候选 stem 通常 5-7 个，整合后并行 HTTP 大约 10-15 次 GET，本地进程 <3s。
**替代方案**：eager 全拉——浪费，热路径加了无谓延迟。
**实现**：复用现有 `_suggest_cache`（OrderedDict 2000 条 / TTL 1h / LRU），避免重复 sheet 列重复 GET。

### D3: ValidateAgent 两段式（字段并行 + FK 拓扑）

**选择**：字段层完全并行（零 LLM）；FK 层必须按 topo 顺序推进，复用 `_validate_forward_refs_llm`（#19 opt-in）。
**理由**：完全并行无法判前向引用（本批补建的 ID 尚未"产出"）；完全串行损失字段层并行优势。两段式兼顾。
**替代方案 A**：完全并行（rapid run）——FK 校验失效，漏悬空引用。
**替代方案 B**：完全串行——延迟升级，违背快路径优先。
**FK 优化（"L"形流水）**：topo 序推进时 produced 集合即时填充，下游消费者可在 producer 落地瞬间并行校验。

### D4: ExecuteAgent 去 LLM，纯确定化派发

**选择**：执行阶段零 LLM 调用。`list[SubTask]` 已 schema-grounded、已校验过，只需 JSON → API 直译。
**理由**：LLM 进执行循环=把 Step1 省下的延迟全部加回来 + 引入不确定性 + 占位符代换逻辑错乱风险。Agent 框架可保留概念（dispatch table 仍叫 "ExecuteAgent"），但具体 `tool_call` 参数是 `SubTask` 直译，不是 LLM 现场推理。
**替代方案**：LLM-in-loop ReAct 执行——已验证（§agent-verify-repair-loop D4 替代方案被否），违背快路径优先。
**例外路径**：写后 verify-repair loop（§agent-verify-repair-loop）仍允许 LLM 介入，但那是失败路径，且与 ExecuteAgent 解耦（ExecuteAgent 触发后的 verify-repair 在同 Step3 内调度但走不同分支）。

### D5: 自学习复用 skill_updater，不新建模块

**选择**：Step4 自学习直接调 `skill_updater.induce_anti_patterns(failures, ai_enhancer)`，写 `anti_patterns.yaml` (pending_review)。
**理由**：R2 #6 已落地完整归纳逻辑（trigger_pattern/rationale/source 字段 + promote_with_guard 门控），新建就是重复造轮子。
**替代方案**：新建 `learning_agent.py` 独立归纳——重复，且与现有 anti_patterns/skill_updater 体系分裂。
**门控保留**：promote_with_guard mini_regression 通过才升 active，eval 环境保持 pending_review；防"反模式自学习反向 OVER-FIT 错误场景"。

### D6: 交互式修复默认开（#41 已是默认）

**选择**：`CODEMAKER_INTERACTIVE_REPAIR=1` 已默认开（`agent_service.py:1969`）。4-Step 架构继承。
**理由**：本轮原 AI 校验发现的遗漏当时无出口给用户，被 `规则结果优先` 黑洞吞掉。新架构下 ParseAgent 漏字段（Step4）→ ValidateAgent 应反问;"用户笔误 vs 需补建"（Step2 FK 层）应反问。
**替代方案**：默认关——回退 ABORT，但本架构明确需要交互（用户原话"询问用户进行修改"），故保持默认开。非交互场景用户显式设 `=0`。
**前端**：`AgentChatView.vue` ask-card 已落地，replyAsk POST `/reply`；未跑全链路 e2e（#41 备注）。本架构需补端到端测试。

### D7: produces_inference 完全复用 R8b

**选择**：Step1 末尾产 SubTask 时，produces/consumes 占位符自动标注完全走 `produces_inference.infer_produces_consumes`（R8b 已落地）+ 显式 PK 字面代换（#44）。
**理由**：R8b 已是关系图驱动（无 per-template 硬编码 produces），与本架构"撤硬编码"方向一致；重写就是回退。
**校验点**：扩 table_relations.json 覆盖 SpawnQuestEntity→quest FK 等（R8b 已声明，验证可能仍待扩展）。

### D8: 灰度推 + 回滚开关

**选择**：4-Step 架构以 `CODEMAKER_4STEP_LOOP=1` 总开关推，默认关（保持 6-Step 不动）；各子能力独立开关 `SCHEMADRIVEN_DECOMPOSE` / `INTERACTIVE_REPAIR` / `VALIDATOR_LLM_FORWARD_REFS`。
**理由**：大架构重构回归面大，需灰度验证；快速回滚至 6-Step 确保线上稳定。
**校验点**：`CODEMAKER_4STEP_LOOP=0` 跑 `table_case_eval.py` 全量基线，对比新老指标（locate/cov/acc/pass/elapsed）。4-Step 必须平均耗时降幅 ≥40% 且 cov/acc 不退化。

---

## 五、代码映射表

| 新模块/改动 | 复用现有 | 状态 | 操作 |
|---|---|---|---|
| `core/agent.py` Step1 重构（ParseAgent） | `decompose_agent.py`（#12/#13/#15 已优化）+ `locator_agent.locate_all`（#24 level6）+ `produces_inference`（R8b）+ `parser_config` yaml + `alias_mapping.json` (#9 autogen) | 全复用 | 新增 `parse_agent.py` 调度 + 关 fast_path 阈值 |
| `core/agent.py` Step1.5 / Step2 / Step3 合并 | 当前 Step2 分区、Step3 计划整体内化到 ParseAgent，不再独立 Step | 删旧 | 旧 Step2/Step3 代码降级为 ParseAgent 内部子流程 |
| ValidateAgent | `validator_agent.py`（#19/#20 已落地）+ `repair_playbook.py`（#29/#41）+ `_ask_callback`（#39，already默认开）+ `semantic_gate`（#21） | 复用 | 新增 tips 序列化 + Step2 两段拆分 |
| ExecuteAgent | `agent.py:_phase_execute` + `OperationOrchestrator._topo_order` + `_save_with_cache_check` | 复用 | 去 LLM，纯 API 派发；verify-repair 解耦但保留 |
| ConcludeAgent | `_phase_summarize`（#38/#40）+ `skill_updater.induce_anti_patterns`（R2 #6）+ `_check_dangling_fk_refs`（#17） | 复用 | 整合输出结构化 summary + failures + 反模式写入 |
| env 开关 | `CODEMAKER_SPLITTER_FAST_PATH` (现) / `CODEMAKER_INTERACTIVE_REPAIR` (现) / `CODEMAKER_AI_ASSIST` (现) | 复用 | 新增 `CODEMAKER_4STEP_LOOP` / `CODEMAKER_SCHEMADRIVEN_DECOMPOSE` |
| SubTask 结构 | 现 `NLIntent` | 扩 field | 路线 A：扩 `NLIntent` 加 `validation`/`execution`/`produces_label`/`consumes_labels` 字段（避免双套结构） |

---

## 六、迁移计划（分四阶段，灰度 + A/B 门控）

### 阶段 1（P0）：核心 ParseAgent 落地（约 1 周）
1. 新增 `parse_agent.py`（无侵入），实现 §3.1 算法的 step 2-6。
2. `core/agent.py:3899-3904` 阈值改 env 可调，新增 `CODEMAKER_SCHEMADRIVEN_DECOMPOSE=1` 开 ParseAgent 主导。
3. 不动 Step2-Step6 旧逻辑，仅让 ParseAgent 产出的 SubTask 适配为 NLIntent 注入 Step2。
4. A/B：`table_case_eval --no-raise` 跑全量，对比 locate/cov/acc，必须 cov ≥ 0.75（现 0.24）。
5. 回滚：`SCHEMADRIVEN_DECOMPOSE=0` 立即回到 fast-path。

### 阶段 2（P0）：ValidateAgent 两段式 + 交互默认开（约 3 天）
1. 集成 `_validate_forward_refs_llm` 到 Step4 字段层，新增 Step2 拓扑 FK 层。
2. `tips` 序列化复用 #39 ask 事件，前端 `AgentChatView.vue` 已支持。
3. 端到端测：构造 1 条"补建失败"用例触发反问，验证 `/reply` 双向 handoff。
4. 失败项结构化（#40）+ 阻断点接入（4 处：verify-repair 上限/AI verify missing/占位符未替换/悬空 FK）。
5. A/B：触发反问成功率 + 用户回复后增量续跑覆盖率。

### 阶段 3（P1）：ExecuteAgent 去 LLM（约 1 天）
1. 抽空 `_phase_execute` 内 LLM 调用，verify-repair 解耦到 `verify_repair_loop.py` 作为可选分支。
2. 拓扑序执行 + 占位符字面代换（#21 placeholder 断言 + #44 显式 PK 代换）。
3. A/B：耗时对比，期待单条 6 表链从 600s+ 降到 <60s。

### 阶段 4（P2）：ConcludeAgent 自学习闭闭环（约 2 天）
1. Step6 汇总复用 `_phase_summarize`（已扩 #38/#40）。
2. 失败时调 `skill_updater.induce_anti_patterns`，落 `anti_patterns.yaml (pending_review)`。
3. 单独提一个 `anti_pattern_e2e_test.py`：注入失败 trace，验证归纳 + 反向匹配生效。
4. 发布机制：定时升 active / 手动 promote。

### 全量上线门控
- `table_case_eval.py` 全量 30 用例基线（§十六 #36 待办），新架构跑一轮，必须：
  - locate ≥ 0.80、cov ≥ 0.75、acc ≥ 0.75、pass ≥ 0.40（vs 现状 0.80/0.24/0.36/0.33）
  - 平均耗时降幅 ≥ 60%
- `CODEMAKER_4STEP_LOOP=1` 默认开。

---

## 七、register openspec / 推进顺序

正式落地前，按规范走 openspec 提交：

```
openspec/changes/excel-agent-4step-loop/
├── .openspec.yaml
├── proposal.md   ← 本文档 §一/§二
├── design.md     ← 本文档 §三/§四
├── tasks.md      ← §六 阶段任务拆词
└── specs/
    ├── schema-driven-decompose/spec.md     ← §3.1
    ├── two-layer-validate/spec.md          ← §3.2
    ├── deterministic-execute/spec.md       ← §3.3
    └── conclude-self-learn/spec.md         ← §3.4
```

---

## 八、预期收益量化

| 指标 | 当前（封印魔龙日志） | 新架构目标 | 改善 |
|---|---|---|---|
| LLM 调用次数 | 23+ 次 | 3-5 次 | -80% |
| 墙钟耗时 | 10+ 分钟 | <90 秒 | >85% |
| 意图覆盖度（命中表数） | 2/8 表 | 8/8 表 | +12 pts |
| 漏字段率 | 70%+ | <10% | -60 pts |
| 失败清单结构化 | ❌（黑洞吞） | ✅（#40） | 新增能力 |
| 交互式反问 | ✅（#41 默认开但未跑通） | ✅ 端到端验证 | 新增能力 |
| 自学习沉淀 | ✅（#6 落地）但 smart trigger 缺 | ✅ ConcludeAgent 自动调 | 闭环 |

LLM 调用大头省在 Step1：当前 `parse_multi` 超时兜底走逐条 LLM parse（#23 修了一半），新架构里 schema-driven 拆分直接取代这条路径，6 表链从 ~16 次降到 ~2 次。

---

## 九、风险与权衡

1. **[LLM 拆分稳定性方差]** schema_block 注入是否能让 LLM 不漏表？阶段 1 A/B 必须验证。降级：拆分失败回退 splitter_baseline（保留 fallback）。
2. **[Schema 拉取缓存命中率]** 复用 `_suggest_cache` LRU+TTL，第二次类似输入命中缓存零延迟。但首次冷启动拉 10-15 sheet 仍是 ~3s 本地 IO，可接受。
3. **[交互式反问打断非交互场景]** `CODEMAKER_INTERACTIVE_REPAIR=0` 显式关闭走 ABORT（已有 #41）；CI/自动化测试必设此标志。
4. **[Verify-repair 与 ExecuteAgent 边界]** verify-repair loop（§agent-verify-repair-loop）默认 off，开启时在同 Step3 内调；不影响默认快路径。
5. **[自学习反向 OVER-FIT]** skill_updater 的 promote_with_guard mini_regression 必跑；eval 环境保持 pending_review，人工升 active。本架构不放宽门。
6. **[FK 关系图覆盖完备]** 架构强依赖 `table_relations.json` 准确性（R8b 已声明式扩展但仍有遗漏）；需补 §五 #24 的"table 关键词并入 alias_mapping"以旁路关系图缺失场景。
7. **[NLIntent → SubTask 兼容性]** 主张路线 A（扩 NLIntent）需保证旧字段 `source="splitter"` #22/#25 保护语义不变；阶段 1 适配器层过渡，验证不回归。
8. **[前端 ask-card 联调]** #41 已搭 `_SESSION_REPLIES` + ask SSE + AgentChatView ask-card，但未跑全链路 e2e；阶段 2 必须补端到端测试。

---

## 十、未决问题

1. **SubTask 路线 A 还是 B？** A=扩 NLIntent，B=新建 SubTask 类。倾向 A（避免双套结构），落地时验证旧路径不受影响。
2. **Schema 拉取限流？** 15 并发 GET 对本地 FastAPI / libreoffice 计算是否有压力？阶段 1 实测后定上限。倾向 8 并发 ThreadPool。
3. **ParseAgent 内 LLM 拆分是否复用 DecomposeAgent 单 prompt vs per-candidate-stem 多 prompt？** R8g 是每候选一 prompt；新架构是否同形？schema_block 全注入单 prompt 更省 LLM 但 token 易炸。倾向：候选 stem ≤4 时单 prompt，>4 时按 stem 分组并行（复用 `CODEMAKER_DECOMPOSE_WORKERS` 并发上限，#15）。
4. **交互反问的"增量续跑"如何实现？** #39 提到需 `agent.run()` 支持 pause-resume / 增量 patch；当前架构是否需引入 checkpointer？倾向：阶段 2 先支持"全量重 parse + skip 已成功项"，阶段 3 再上真 pause-resume（与 §-agent-verify-repair-loop P1 重叠）。
5. **优先级排序：先推哪个阶段？** 倾向 阶段 1（ParseAgent）→ 阶段 3（ExecuteAgent 去 LLM）→ 阶段 2（ValidateAgent）→ 阶段 4（ConcludeAgent）。理由：阶段 1 直接解决漏字段（用户最痛），阶段 3 直接解决耗时（用户也痛但需 1 完成才能验证），阶段 2 涉及交互前端需端到端（最复杂），阶段 4 是锦上添花。

---

## 十一、深度发展路线：Agent 增强模式对齐（ReAct / Plan-Execute / Multi-Agent / 记忆 / 工具 / 成本 / 安全 / 监控）

> 触发来源：对照 AI Agent 架构综述（"从 ReAct 到多 Agent 协作"）提出的四模式 + 三层记忆 + 工具风险分级 + 预算分层 + 沙箱 + 监控，审计本仓 4-Step Loop 架构的"已具备 / 部分 / 缺位"。
> 本节是 4-Step 之上的纵深演进 TODO，与 §三~§六 不冲突（§六 4 阶段仍先落地）；下列为后续版本的演进路线。本文是"前瞻路线"，非 §六 的阻塞性前置。
> 对应落地任务见 `docs/excel-agent-4step-loop-task.md` §9。

### 11.1 现状审计（方法 ↔ 代码映射）

| 综述方法 | 本仓现状 | 代码位置 | 评级 |
|---|---|---|---|
| ReAct（think→act→observe） | 仅 repair Level 2 失败回退路径有手写文本协议 ReAct 循环；主循环是固定线性 pipeline，且 §D4 明确否决"执行阶段 LLM-in-loop ReAct" | `server/agent/excel/core/agent.py:_run_react_repair`、`server/agent/tools.py:make_skill_tools` | 部分（仅失败路径） |
| Plan-Execute（规划→执行→失败再规划） | `OperationOrchestrator._topo_order` + `produces_inference` 做隐式规划（拓扑序 + produces 推断）；**无"失败自动重规划剩余步骤"** 的显式 plan/replan 循环，失败仅进 failures 上报 | `engine_core/operation_orchestrator.py`、R8b produces_inference | 缺位 |
| Router Agent（分类→分发） | 顶层 `OrchestratorAgent` classify 节点 → `RouteResult{qa,crud}` 二分发；是"2 路 router"而非"多专家 router" | `server/agent/graph.py`、`orchestrator.py:RouteResult` | 具备（窄） |
| Multi-Agent Orchestra（编排多角色 + reviewer 轮） | 有角色化 subagent（Decompose/Locator/Validator/LlmAgent/Dispatcher + 4 个 Fill 角色），但**固定串行 pipeline**，无 Orchestrator 动态选角、无 Coder→Reviewer 修订闭环 | `subagent/*`、`engine_core/roles.py` | 部分 |
| 三层记忆（短期 / 工作 / 长期向量） | **完全缺位**：无短期对话状态持久、无工作记忆 scratchpad、无向量长期记忆；仅有 `anti_patterns.yaml`(pending_review→active) 的模式化"长期记忆"与 `_suggest_cache` LRU schema 缓存 | `skills/L3_anti_patterns`、`agent_service._suggest_cache` | 缺位 |
| 工具注册表 + 风险分级（safe/write/dangerous） | 有 7 个 skill tool，写类 tool 返 `needs_confirm` 提案；但**无集中 ToolRegistry + risk_level 分级 + 自动/确认/审批三态门控** | `tools.py:make_skill_tools`、`SkillExecutor` | 部分 |
| 成本控制（预算驱动模型分层） | `llm_counter.py` 仅按 site 计数；**无预算驱动模型分层**（Haiku/Sonnet 自动择挡），单 codemaker 模型 | `server/agent/llm_counter.py` | 缺位 |
| 安全（沙箱隔离 + 审批流） | 有 `patch_validator` + 进程池隔离；**无 Docker/WASM 沙箱、无危险工具审批流** | `engine/patch_validator.py`、processpool | 部分 |
| 监控（指标仪表盘） | 有 `llm_counter` + `step_sink/thinking_sink` 事件；**无 tool_success_rate / 预算剩余 / 熔断等聚合指标** | `llm_counter.py`、orchestrator sinks | 部分 |
| 框架选型 | LangGraph（与综述建议一致）✓ | `graph.py:StateGraph` | 具备 |

> 净结论：**Router / Multi-Agent-角色 / LangGraph / 进程隔离 / skill-自学习**已具备；**主循环 ReAct、显式 Plan-Execute-with-replan、三层记忆、工具风险分级、预算分层、沙箱审批、聚合监控**是深度演进缺口。

### 11.2 深度发展路线（按性价比 × 与 4-Step 协同性排序）

#### P0-1｜Plan-Execute 显式化（replan-on-failure）— 与 §3 ExecuteAgent 不冲突的增强
- 现状：ExecuteAgent 失败即入 failures 上报，无重规划。
- 演进：失败子任务回灌新增 `ReplanAgent`（≤1 次 LLM），结合 failures.root_cause + remaining 子任务重新拓扑，产出修订 `list[SubTask]` 回 ExecuteAgent。等价于综述 `PlanExecuteAgent._replan`。
- 与 §D4 不冲突：D4 否决的是"执行阶段现场 LLM 推理"，replan 是"失败后离线重规划"，属 ConcludeAgent 之外的增量闭环，且默认关。

---

## 十二、落地进度与现状偏差（2026-08-17 更新）

> 本节汇总 4-Step Loop 实际落地进度 + 与设计文档的偏差 + 阻塞项。
> 详细任务级进度见 `docs/excel-agent-4step-loop-task.md` §0 落地进度摘要。

### 12.1 4 阶段核心落地状态

| 阶段 | Step | 状态 | 核心改动 |
|---|---|---|---|
| §1 配置 | 前置 | ✅ | `server/agent/configuration.py` 加 6 字段（enable_4step_loop/schema_driven_decompose/schema_fetch_concurrency/schema_fetch_sheet_limit/splitter_decompose_threshold/execute_no_llm）+ `agent.py:3899` fast-path 阈值 env 化 |
| §2 ParseAgent | Step1 | ✅ | 新建 `excel/parse_agent.py`（整合 LocatorAgent.locate + DecomposeAgent.decompose + produces_inference + SplitIntent→NLIntent 适配 + splitter_baseline 兜底）；`parser/nl_parser.py` 扩 NLIntent（produces_label/consumes_labels/source/ai_check_skipped/validation/execution + ValidationResult/ExecutionResult/Issue/IssueType/assemble_tips）；`agent.py:3834+` 接入 _4step_parsed 排除 |
| §3 ExecuteAgent | Step3 | ✅ | `agent.py:5487` 加 execute_no_llm 分支（失败直接进 failures 跳 LLM 诊断/重试）；现状已落地 §3.3-3.6（占位符断言/拓扑派发/resolved 同步/失败不阻塞同层） |
| §4 ValidateAgent | Step2 | ✅ 核心 | `subagent/validator_agent.py` 加 validate_field_layer（①列存在②类型③必填）+ validate_fk_layer（拓扑序+FORWARD_REF_BROKEN）+ validate_two_layer（整合+ask_user+重校+splitter_baseline 跳 LLM）+ set_ask_callback；`agent_service.py:1981/2013` 注入 _ask_callback 到 validator |
| §5 ConcludeAgent | Step4 | ✅ 核心 | `agent.py:6679` _phase_summarize 失败路径扩全量 failures 聚合；`routers/agent.py:256` done_data 加 failures payload；现状已落地 §5.2/5.3/5.6（连通校验/自学习/mini_regression） |

**累计 259 测试零回归**。

### 12.2 路径修正（vs §五代码映射表）

| 设计文档路径 | 实际路径 | 说明 |
|---|---|---|
| `server/agent/excel/configuration.py` | `server/agent/configuration.py` | LangGraph 运行时配置在 agent/ 根，非 excel/ 子包；excel-agent env 开关部分内联 `os.getenv`（跟随现有惯例） |
| `codemaker_parser.py` NLIntent | `server/agent/excel/parser/nl_parser.py` | NLIntent dataclass 实际在 parser/nl_parser.py:13（codemaker_parser.py 仅引用） |
| `produces_inference._explicit_pk_literal_substitute` | `produces_inference.infer_produces_consumes` 内联 | 显式 PK 字面代换逻辑内联在 infer_produces_consumes 的 producer_pk_values（core/produces_inference.py:89） |
| `LocatorAgent.locate_all` | `LocatorAgent.locate` | locate_all 在 TableLocator（locator/table_locator.py:289，不同模块）；LocatorAgent.locate 在 subagent/locator_agent.py:116 |
| `_phase_parse` 单 agent | agent_chain 三 agent | Step1 现状是 locator→decompose→validator 三 agent 链（agent.py:3860-3897），非单 _phase_parse；ParseAgent 复用三 agent 不重写 |
| §2.2 HTTP schema_bundle | 本地 cli.read_header | DecomposeAgent._build_schema_block 用 cli.read_header（非 HTTP ?include_columns=1）；HTTP 化待 R21 |

### 12.3 §2.2/2.3/2.6/4.1④⑤⑥ 偏差

| 任务 | 偏差 | 留后续 |
|---|---|---|
| §2.2 lazy schema 拉取 | 任务假设 HTTP API + ThreadPool + schema_bundle；实际复用 DecomposeAgent._build_schema_block 本地读 row1+row2 | HTTP 化 + 独立 schema_bundle 精细化待 R21 接口落地 |
| §2.3 _suggest_cache 复用 | DecomposeAgent 不走该缓存 | 待 §2.2 HTTP 化后接入 |
| §2.6 列名校验 | column_matcher 重映射归 §4.1 ① 列存在性（validate_field_layer 已做基础）+ Step2 字段层 | column_matcher BoW 重映射留 Step2 增强 |
| §4.1 ④唯一性 | 需表数据搜该列已有值 | validator 无 path 解析，需接口重构 |
| §4.1 ⑤枚举白名单 | 需 type_aliases row2 enum set | validator 无 agent helpers（_precoerce_enum_value 是 agent 方法） |
| §4.1 ⑥范围分布 | 需 run_semantic_gate + result_rows + vc | run_semantic_gate 是 agent 方法，validator 无 agent 引用 |

**④⑤⑥ 障碍**：validator 是 SubAgent，无 path + 无表数据 + 无 agent helpers。完整实现需 validator 接口重构（加 path + agent 引用 或 schema_getter 扩展返表数据+enum_set+result_rows）。

### 12.4 阻塞项

**§1.4 / §2.12 / §3.8 / §4.9 / §5.5 端到端 A/B + e2e**：依赖 codemaker serve。R7 serve 端 143.8k token/156s 卡死（`excel-agent问题与优化方向.md` §6.3 定位为 serve 端固定行为：对 prompt 文本出现的表 stem 自动文件读取上下文，90s 超时返回空）。非 excel-agent 代码 bug，待 serve 侧根治（关 auto-context / 提供纯文本补全端点 / 非 agentic 通道）。

### 12.5 env 开关清单（全部默认 opt-in 灰度）

| env | 默认 | 作用 | 对应任务 |
|---|---|---|---|
| `CODEMAKER_4STEP_LOOP` | 0 | =1 run() 走 ParseAgent 主导 | §2.10 |
| `CODEMAKER_SCHEMADRIVEN_DECOMPOSE` | 0 | =1 ParseAgent 主导 schema 注入 LLM 拆分 | §2.1 D1 |
| `CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD` | 2 | cross_intents_nl < 此值触发 _llm_chain_decompose；99 强制 LLM 接管 | §1.2 |
| `CODEMAKER_SCHEMA_FETCH_CONCURRENCY` | 8 | schema 拉取 ThreadPool 上限（待 §2.2 HTTP 化） | §2.2 |
| `CODEMAKER_SCHEMA_FETCH_SHEET_LIMIT` | 15 | schema 拉取候选 sheet 上限 | §2.2 |
| `CODEMAKER_EXECUTE_NO_LLM` | 0 | =1 _phase_execute 失败跳 LLM 诊断/重试，直接进 failures | §3.1 |

**设计原则**：所有新开关默认 opt-in（=0 关），保持 6 步 pipeline 现状不回归；灰度验证后（§6 全量回归门控）才默认开。

### 12.6 后续落地（2026-08-18 更新）

| 任务 | 落地 | 关键文件 | 测试 |
|---|---|---|---|
| §4.1 ④⑤⑥ 字段层扩展 | data_getter 注入（方案 B：path/stem/sheet/vc/existing_values/enum_set/result_rows/cli）+ ⑤枚举白名单（enum_set + `_check_enum_whitelist` 纯函数 fallback）+ ④唯一性（existing_values）+ ⑥范围分布（modify only + `run_semantic_gate` 纯函数）。validator 保持无 agent 引用，复用 semantic_gate 纯函数 | `subagent/validator_agent.py` validate_field_layer + validate_two_layer 加 data_getter 参数 | test_validate_agent_two_layer +8（累计 53） |
| §3.2 verify-repair 解耦最小版 | `check_type_constraint` 纯函数抽到 `repair/verify_repair_loop.py` + agent 薄转发；循环主体（464 行+8 helper）文档化待后续大重构 | `repair/verify_repair_loop.py`(新) + `core/agent.py:5680` 薄转发 | test_verify_repair_loop 14 |
| §2.2 HTTP schema_bundle | `build_data_getter` 构造器（cli 直读替代 HTTP，同进程更快）+ `_stem_to_path`/`_existing_values_from_rows`/`_rows_to_dicts` helpers | `excel/schema_bundle.py`(新) | test_schema_bundle 27 |
| stage 4-Step 前端打印 | 后端 `_STAGE_ORDER`/`_STAGE_TITLES` 加 `_4STEP` 切换（4-Step：s1_parse/s2_validate/s3_execute/s4_summary）+ `_stage_for_thinking`/`_stage_for_step` 4-Step 映射分支；前端 `stageNo` 双 order（order4+order6）+ `stageTotal` 动态分母 + L686 `/6`→`stageTotal()` | `services/agent_service.py:2043+` + `frontend/src/views/AgentChatView.vue:1077+` | 前端 vite build ✓ |
| validator 接入 4-Step 路径 | ParseAgent 产出后调 `validate_two_layer`（字段层 6 项+FK 拓扑层+ask_user 交互反问+skipped 过滤）；schema_getter 用 `_stem_to_path`+`cli.read_header/read_type_row`，data_getter 用 `schema_bundle.build_data_getter` | `core/agent.py:3858+` | 267 回归 |

**§4 字段层完整 6 项**：①列存在 ②类型 coerce ③必填 ④唯一 ⑤枚举 ⑥范围分布。

**§3.2/§2.2/validator 剩余**：
- §3.2 完整版：`_run_verify_repair_loop` 循环主体（agent.py:6109-6372, 464 行）+ 8 helper 抽到 verify_repair_loop.py，大重构高风险，待后续
- §2.2 HTTP 化：excel-agent 独立服务部署时改 build_data_getter 走 HTTP GET（R21 `/api/tables/{stem}/sheets/{sheet}?include_columns=1`）替代 cli.read_header/read_sheet
- 6 步路径补传 validate_two_layer：需 SplitIntent→NLIntent 适配，让默认关（enable_4step_loop=0）也跑新字段层 6 项，待后续
- ExecuteAgent 跳 skipped：_phase_execute 加 validation.skipped 检查跳写盘（4-Step 路径过滤已做，单 intent 跳检查留后续）

**累计 323+ 测试零回归**（7 单测文件 + 现有回归）。前端 vite build ✓。
- env：`CODEMAKER_REPLAN_ON_FAILURE=0`（默认关，灰度）。

#### P0-2｜Multi-Agent 动态编排 + Reviewer 闭环
- 现状：角色固定串行。
- 演进：在 `OrchestratorAgent` 之上引入 `RoleDispatcher`（按 SubTask.action/table_stem 选 Fill 角色），并加 Coder→Reviewer 修订闭环（Reviewer 对写盘结果做规则 + LLM 审查，不通过回 ExecuteAgent 修，最多 N 轮）。
- 复用：现有 `validator_agent` 做 Reviewer 雏形；`engine_core/roles.py` 4 Fill 角色作 Coder 分身。
- env：`CODEMAKER_REVIEW_LOOP=0`（默认关）、`CODEMAKER_REVIEW_MAX_ROUNDS=3`。

#### P1-1｜三层记忆系统（短期 / 工作 / 长期向量）
- 短期：`AgentState`（LangGraph 已有）扩 `history` 列表 + 会话级 checkpointer（pause-resume 能力，呼应 §十 #4）。
- 工作记忆：SubTask 链固化为 `AgentState.working_memory`，跨节点透传。
- 长期（向量）：选型 pgvector 或轻量 sqlite-vec；`LongTermMemory.store/recall/forget`；把 `anti_patterns.yaml` 升级为"可解释兜底 + 向量召回"双路（yaml 仍保留作人工审计/可解释）。
- env：`CODEMAKER_LONG_TERM_MEMORY=0`（向量库依赖引入需评审后开）。

#### P1-2｜ToolRegistry + 风险分级 + 审批流
- 集中 `ToolRegistry.register(name, func, group, risk_level)`，三态门控：safe 自动执行 / write 返 needs_confirm / dangerous 暂停等人工审批（接 §D6 交互反问通道复用 `_ask_callback`）。
- 现有 `make_skill_tools` 迁为注册表项；删多行 / 原始 SQL 等标 dangerous 走审批。

#### P1-3｜成本控制：预算驱动模型分层
- 配置层先支持多 model（`CODEMAKER_MODEL_HEAVY` / `CODEMAKER_MODEL_LIGHT`），`CodemakerClient` 按 model 名路由。
- 新增 `CostTracker(budget)` + `suggest_model(task_complexity, spent)`：分类/校验走 light、拆分/审查走 heavy；预算 >70% 自动降挡。
- 复用 `llm_counter` 累计花费做预算闭环。

#### P2-1｜安全沙箱 + 危险工具审批
- 执行层写盘 subprocess 包 `docker run --read-only --network=none`（或 WASM）；保留 processpool 作降级路径。
- 危险工具人工审批流（与 §P1-2 dangerous 工具复用同一审批通道）。

#### P2-2｜聚合监控仪表盘
- `AgentMetrics.report()`：tool_success_rate / llm_calls / budget_left / 熔断次数；`step_sink` 推前端 + `server/tests/reports` 落盘。
- 复用已有 `llm_counter` 作数据源，新增聚合视图。

### 11.3 收敛原则
1. 上列 11.2 全部为 4-Step 之上的"可叠加能力层"，默认全部 opt-in（env=0）；不削弱 §六 4 阶段灰度主路径。
2. 每项落地必跑 §6 全量回归（locate/cov/acc/pass/elapsed 基线不退化）+ 新增专项单测。
3. 每项落地前按 §七 规范建 `openspec/changes/<feature>/` 提案（proposal/design/tasks/specs），`openspec verify-change` 通过后 archive。
4. 与 §D4 一致性：本路线禁止把"执行阶段现场 LLM 推理"作为主路径；仅 replan/reviewer/记忆召回作为增量 LLM 触发点，且均默认关。

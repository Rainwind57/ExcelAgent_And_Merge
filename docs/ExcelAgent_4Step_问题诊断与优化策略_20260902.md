# ExcelAgent 4-Step 问题诊断与优化策略

日期：2026-09-02

## 结论摘要

当前 4-Step V2 已经把流程切成 Step1 Parse、Step2 Validate、Step3 Execute、Step4 Conclude，并在 Step3 明确要求零 LLM。但从代码现状看，主要瓶颈仍集中在 Step1：定位、候选表扩展、FK 扩展、schema 注入、规则 fallback、领域链展开器混在同一阶段，导致 Step1 既过重，也不够像一个真正依赖 LLM 理解复杂输入的 planner。

现在的问题不是“没有 LLM”，而是 LLM 被大量硬编码模板、正则分支、候选池裁剪、重拆兜底包围。复杂输入进来后，系统为了避免漏表，会扩很多候选表和 FK 邻接表；为了让 LLM 能选对，又把多表 schema、FK、列名信号塞进 prompt。结果是 prompt 变长、codemaker serve 响应时间变长，甚至出现长时间等待、空响应或降级到规则模板的情况。这样既削弱了 LLM 处理自然语言复杂输入的优势，也让后续 Step2/3 被 Step1 的不稳定输出拖累。

## 当前链路观察

4-Step 的主编排在 `server/agent/excel/core/pipeline/orchestrator.py`。Orchestrator 会按固定顺序执行 Step1-4，遇到 hard error 时停止后续执行，但仍进入 Step4 做汇总。Step 上下文与错误契约在 `server/agent/excel/core/pipeline/contracts.py`。

Step1 实际由 `Step1ParseSubAgent` 包装 `ParseAgent`，然后读取 Locator/Decompose 的中间产物，构建 plan graph、semantic plan、audit 和 quality report。相关入口在 `server/agent/excel/core/pipeline/step1_parse_subagent.py`。

Step1 的表定位主要在 `server/agent/excel/subagent/locator_agent.py`：先规则定位和 ColumnExtractor 反查列名，再用 LLM 做候选复核，然后做 FK 扩展、候选重打分和裁剪。候选数量由 `CODEMAKER_LOCATOR_MAX_CANDIDATES` 控制，复杂输入会放宽候选上限。

Step1 的跨表拆分主要在 `server/agent/excel/subagent/decompose_agent.py`：它会读取候选表 schema，构造单 prompt、并发 prompt 或链式分组 prompt，再把 LLM JSON 转成 SplitIntent。这里已有 `_schema_cache`、session 复用、`CODEMAKER_DECOMPOSE_TIMEOUT`、`CODEMAKER_DECOMPOSE_CHAIN_GROUP` 等优化，但本质仍是把候选 schema 批量塞给 LLM。

## Step1 主要问题

### 1. 硬编码分支过多，削弱 LLM 泛化价值

`cross_table_splitter.py` 中保留了大量面向具体领域的模板，如 pet、evolve、npc_dialogue、npc_composite、item、mail、quest、school_ability_spell、combat_reward、residence_building 等。`DecomposeAgent` 中还存在 `_try_domain_expander`，对某些高频链型直接走确定性展开。

这些规则能提高已知用例通过率，但副作用明显：

- 新链型或新表结构需要继续加模板，系统越来越像规则引擎。
- LLM 只在模板无法覆盖或候选歧义时补位，难以体现“理解复杂输入并产出计划”的优势。
- 规则、LLM、fallback 互相重叠，问题定位困难：一次失败可能来自 route、locator、schema、decompose、filter 或 fallback 任一层。
- 历史修复逐渐沉积成“特例优先”，后续维护成本会继续上升。

### 2. 候选表和 FK 扩展导致 prompt 过长

Locator 当前会把规则候选、列名反查候选、LLM 复核结果和 FK 扩展候选都放进候选池。复杂输入时，候选上限会提高，FK 扩展又会把相关表继续拉入。Decompose 再为这些候选读取 row1/row2 schema，构造 schema block 和 FK block。

这会直接放大 codemaker serve 的耗时：

- 候选表越多，schema block 越长。
- 一个 workbook 多 sheet 时，单个候选表也可能展开出大量表头。
- FK 扩展引入的是“可能有关”的表，但 prompt 里没有强制区分“必需表”和“背景表”。
- Decompose 为了避免漏表又设置链式分组与单表重拆，LLM 调用次数和等待时间进一步上升。

代码里已经记录过类似现象：链式任务候选 12+ 表时，单 prompt 可能超时或空响应；分组 prompt 虽然缓解长度，但会切断全局依赖视野，导致 produces/consumes 链接质量下降。

### 3. Step1 职责过宽

Step1 现在不只是“理解输入并输出计划”，还承担：

- 分段覆盖对账。
- 候选表收集与表选择。
- schema 读取和 prompt 拼装。
- produces/consumes 推断。
- placeholder graph 检查。
- semantic plan 编译。
- plan completeness 审计。
- fallback 到 splitter baseline。

这些能力都重要，但放在同一阶段会让 Step1 成为重型混合系统。结果是 Step2/3 虽然看似职责清晰，却仍依赖 Step1 产物质量。一旦 Step1 输出半结构化、漏 intent、错表或过度候选，后续只能补救。

### 4. 性能指标缺少精细归因

现在 Step1/Step3 会记录 `llm_calls`、`dur_ms` 等指标，但还不够回答“慢在哪里”：

- 候选表数量、FK 扩展数量、schema 字符数、prompt 字符数、每次 LLM 耗时没有统一进入最终报告。
- codemaker serve 长时间等待时，前端能提示疑似 hang，但后端缺少按阶段的 prompt size 与 response size 归档。
- Decompose 有 trace 开关，但更像调试工具，还没有成为稳定可比对的性能账本。

## Step2 观察与问题

Step2 在 `step2_validate_subagent.py`，主要复用 legacy 的 `validate_intents`，再补结构检查、Step1 quality hard issue、semantic compile issue、plan completeness issue。

目前 Step2 的设计方向是对的：它负责字段层、FK 层、PK 冲突和 ask 修正，不做输入解析，也不写盘。但仍有几个问题：

- Step2 依赖 Step1 的语义计划和 locator artifact。如果 Step1 候选错或 plan graph 不完整，Step2 只能报错，难以主动重构计划。
- `CODEMAKER_PLAN_COMPLETENESS_GATE` 默认不开 hard gate，完整性问题可能以 warning 形式继续流向 Step3。
- legacy validate 仍通过 `ExcelAgentServices` 委托旧 agent 私有方法，说明 Step2 还没有完全服务化，边界仍是过渡态。
- hard/soft 分类复杂，部分问题可能为了保基线被降级，导致用户看到的是后续执行失败，而不是更早的计划错误。

优化重点：Step2 应成为“确定性 schema/FK 校验器”，少做补救，多产出可机器处理的修正建议。对于缺 producer、悬空 FK、字段不存在、类型不匹配，应优先在 Step2 阻断或回到 Step1 replan，而不是让 Step3 带病执行。

## Step3 观察与问题

Step3 在 `step3_execute_subagent.py`，目标是零 LLM 执行。它会对 Step2 validated intents 做拓扑排序、placeholder 替换、逐条 `run_single(no_llm=True)`，并收集 produced ID、partial write、needs_confirm、failures。

已经做得比较好的点：

- `no_llm=True` 透传到 `_run_single`，避免 Step3 再触发 plan/validate/repair LLM。
- placeholder 未解析会显式跳过，避免写入缺 FK 的残缺行。
- 全部子任务失败时会升级 hard error。
- needs_confirm 和 partial_write 有独立记录。

剩余风险：

- 执行仍复用 legacy `_run_single`，真实事务边界不强。跨表多 intent 写入时，如果前几条成功、后几条失败，仍可能留下部分变更。
- `_backfill_forward_refs` 是事后补引用，若补写失败，容易出现“主写成功但引用不完整”的状态。
- Step3 的 ok 依赖 subtask 状态聚合，但 partial、pending、skipped、failed 的用户语义还需要更清楚地区分。
- Excel 写盘不是事务式操作，缺少统一 dry-run/preflight 阶段来保证所有依赖可满足后再真正写。

优化重点：Step3 应从“逐条执行 + 尽量补救”升级为“两阶段提交感”的执行器。先 dry-run 解析所有行、列、PK、FK 和写入位置，全部通过后再写；写前创建文件快照，失败可恢复。

## Step4 观察与问题

Step4 在 `step4_conclude_subagent.py`，负责汇总结果、合并前序 failures、生成自然语言 summary，并在失败时调用 anti-pattern 归纳。

当前已修过一些关键问题，例如不再只看 Step3，而是把 Step1/Step2 的错误纳入最终成功判断；对漏解析段也会避免误报全成功。

剩余风险：

- 总结仍偏模板化，能告诉用户失败在哪里，但还不能稳定给出“下一步最小修复动作”。
- anti-pattern 归纳会在 failures 非空时触发 LLM，可能把执行失败后的收尾阶段也变慢。
- 经验沉淀如果缺少严格门禁，长期会把一次性失败或错误修正沉淀成规则噪音。
- Step4 对性能诊断帮助有限，缺少“本次慢在候选、schema、LLM 还是执行”的明确摘要。

优化重点：Step4 应输出面向开发者和用户两层摘要：用户看到成功/失败/待确认；开发者看到阶段耗时、prompt 字符数、候选表数、FK 边数、重试次数和失败归因。

## 优化方向

## 更优目标架构

后续优化不建议继续沿着“再补一个规则、再加一个 fallback、再调一个 timeout”的方向走。更稳的方向是把 Step1 从重型混合解析器改造成“轻量检索 + LLM 语义规划 + 确定性编译”的结构。

目标链路：

```text
用户输入
  -> Query Understanding：识别动作、实体、约束、显式 ID、引用关系
  -> Table Retrieval：检索少量高相关 table cards，不展开完整 schema
  -> Semantic Planner：LLM 输出实体图，而不是直接输出 Excel 字段
  -> Schema Grounding：只对被选中的表拉完整 schema
  -> Intent Compiler：确定性编译为 column-level intents
  -> Step2 Validate：确定性校验和 replan hint
  -> Step3 Dry-run + Commit：先预演，后写盘，可回滚
  -> Step4 Diagnose：输出用户摘要 + 开发者诊断
```

这个架构的关键是：LLM 负责理解复杂输入和抽象计划，系统负责检索、schema 约束、字段编译和执行安全。不要让 LLM 在超长 schema prompt 里同时做“找表、选列、拆任务、推 FK、补默认值、判断完整性”六件事。

## Step1 推荐重构方案

### 1. 从直接生成 SplitIntent 改为先生成 Semantic Plan

当前 DecomposeAgent 让 LLM 直接输出 `{table, sheet, action, fields, produces, consumes}`。这要求 LLM 一次性知道表、sheet、列名、依赖和字段值，prompt 必然越来越长。

建议改成第一阶段只输出语义实体图：

```json
{
  "entities": [
    {
      "id": "npc_1",
      "type": "npc",
      "name": "引导长老",
      "action": "add",
      "attributes": {
        "dialogue": "欢迎加入门派",
        "reward": "新手礼包"
      }
    }
  ],
  "relations": [
    {"from": "npc_1", "type": "opens_dialogue", "to": "dialogue_1"},
    {"from": "dialogue_1", "type": "grants_reward", "to": "reward_1"}
  ]
}
```

这一步不要求 LLM 输出真实 Excel 列名。它只做自然语言理解，复杂输入越复杂，LLM 的优势越明显。

第二阶段再由系统根据 table cards、关系图、schema mapper 把实体图编译成真实 `SplitIntent`。只有编译遇到歧义时，才让 LLM 读取局部 schema 补充。

### 2. 建立 Table Card 索引，替代全量 schema 注入

为每个 sheet 离线或启动时生成轻量 table card：

```yaml
stem: interaction
sheet: InteractionConv
purpose: 对话节点
primary_key: conv_id
required_columns: [conv_id, prompt_text]
fk_columns:
  - column: option_1
    target: InteractionConvOption.option_id
aliases: [对话, 对话节点, prompt, 台词]
typical_actions: [add, set, delete]
```

检索阶段只把 top cards 给 LLM。完整 row1/row2 schema 只有在编译某张表时读取。这样可以把“十几张表完整表头”的 prompt 降成“几张表的摘要卡片”。

### 3. 候选分层和预算控制

候选表不要再用一个 `candidates` 列表承载所有含义。建议改为：

```json
{
  "required": ["entity_prefab", "interaction"],
  "dependency": ["reward", "mail"],
  "context": ["quest"],
  "rejected": [
    {"stem": "school", "reason": "only weak column alias matched"}
  ]
}
```

预算规则：

- `required` 最多 5 张，允许完整 schema。
- `dependency` 最多 8 张，只给 PK/FK/必填列摘要。
- `context` 默认不进入 Decompose prompt，只在 Step2 replan 时使用。
- prompt 超过预算时，不延长 timeout，而是降级 schema 粒度。

这比单纯调大 `CODEMAKER_DECOMPOSE_TIMEOUT` 更可靠。timeout 只能掩盖慢，不能减少慢。

### 4. FK 扩展从“扩大候选池”改为“解释依赖”

现在 FK 扩展会把更多表拉进候选池，增加 prompt 长度。更好的做法是：FK 图不直接扩 candidate，而是给 planner 一个可查询工具或摘要。

例如：

```text
已选 required 表：interaction
相关 FK：
- InteractionConv.option_1 -> InteractionConvOption.option_id
- InteractionConvOption.reward_id -> Reward.id
- InteractionConvOption.mail_id -> Mail.id
```

LLM 只需要判断“这条用户输入是否真的提到了 reward/mail”。没有提到就不把 reward/mail 升级为 required。这样能避免“可能有关”表吞掉 token。

### 5. 保留硬编码模板，但改变角色

短期不要删除 `cross_table_splitter.py`，因为它是当前通过率的保护网。更好的迁移方式：

- 主路径：Semantic Planner。
- 影子路径：旧 splitter 同时跑，但不直接决定输出。
- 对比：记录 planner 与 splitter 的 intent 差异。
- 兜底：planner 失败或 Step2 hard fail 时才使用 splitter。
- 删除条件：某类模板连续 N 次评测中 planner 覆盖率和正确率都达标，再迁移为 declarative macro 或下线。

这样优化成功概率更高，因为不会一次性拔掉已有能力。

## 保证优化成功的关键机制

### 1. 先做可观测性，不靠猜

每次请求必须生成一份 `step_trace`，至少包含：

- Step1：分段数、候选数、required/dependency/context 数、FK 边数。
- Schema：读取 sheet 数、schema 字符数、table card 字符数。
- LLM：每次调用的 stage、prompt_chars、response_chars、dur_ms、timeout、error_type。
- Planner：semantic entity 数、relation 数、编译后 intent 数、缺失 producer 数。
- Step2：字段命中率、类型错误数、FK 悬空数、replan hint 数。
- Step3：dry-run 通过数、实际写入数、回滚次数、partial 数。

没有这套账本，后续任何“优化”都容易变成调参。

### 2. 建立金标用例集

把当前 hardcoded splitter 支持的场景整理成金标：

- 单表新增、修改、删除、查询。
- NPC + 对话 + 选项。
- NPC + 奖励 + 邮件。
- 任务 + 刷新实体。
- 灵兽 + 进化。
- 门派神通 + 法术 + 技能组。
- 战斗 + 奖励。
- 洞府建筑链。
- 超长混合输入。
- 用户表达不完整，需要 ask 的输入。

每条金标不只检查最终是否 ok，还要检查：

- Step1 semantic entities 是否完整。
- 编译后的 intent 是否命中正确表和 sheet。
- produces/consumes 是否闭环。
- Step2 是否提前发现字段和 FK 问题。
- Step3 是否没有 LLM 调用。
- 失败时是否没有写入残缺行。

### 3. 使用影子评测灰度迁移

新 planner 上线前不要直接替换主链路。建议同时跑：

```text
old_path = current Step1 output
new_path = semantic planner output
compare(old_path, new_path)
only_execute(old_path)
record_diff(new_path)
```

当新路径在金标集和真实请求影子流量中连续达标，再切换：

```text
execute(new_path)
fallback(old_path on hard fail)
record_fallback_reason
```

最后再逐步降低 fallback 频率。

### 4. 用“错误预算”管理风险

给每阶段设硬指标：

- P0 观测阶段：不能改变执行结果。
- P1 候选分层阶段：Step1 timeout 下降，正确率不能下降超过 2%。
- P2 semantic planner 阶段：只影子运行，不写盘。
- P3 主路径切换阶段：fallback 率必须低于 10%，且无新增残缺写入。
- P4 模板下线阶段：对应场景连续通过率达到 95% 以上。

只有满足上一阶段指标，才进入下一阶段。

## 推荐技术路线

### 路线 A：最稳，适合当前项目

先不大改 Step1 对外契约，只在内部加一层 `SemanticPlanner`。

实现顺序：

1. 新增 `table_card_index.py`，从 resources 的 header、type row、已有 skills 派生 table cards。
2. 新增 `step_trace.py`，统一收集候选、prompt、LLM、schema、错误指标。
3. 在 LocatorAgent 输出中增加 `candidate_groups`，保留原 `candidates` 兼容字段。
4. 新增 `SemanticPlanner`，只输出 entities/relations，不输出真实列名。
5. 新增 `IntentCompiler`，把 semantic plan 编译成当前 `SplitIntent`。
6. Step2 增加 `replan_hints`，把缺表、缺字段、缺 producer 反馈给 Step1。
7. 新 planner 先 shadow，再灰度主用。

优点：风险低，现有 Step2/3/4 基本不用大动。

### 路线 B：更彻底，适合后续重构

直接把 Step1 拆成三个明确子步骤：

```text
Step1A Retrieve：检索表卡和关系卡
Step1B Plan：LLM 生成 semantic plan
Step1C Compile：schema grounding + intent compile
```

然后 Step2 只接受 `CompiledPlan`，不再接受松散 `NLIntent`。

优点：边界最清晰，长期维护成本最低。

缺点：改动较大，需要较完整测试保护。

当前建议采用路线 A，等稳定后再演进到路线 B。

## 关键设计思路

### LLM 不应该背 schema

LLM 应该理解“用户想做什么”和“实体之间什么关系”。真实列名、sheet、主键、FK、默认值应尽量由系统根据结构化知识确定。把 schema 全塞给 LLM，会让模型既慢又容易在字段上幻觉。

### 候选召回和候选决策要分开

召回可以宽，决策必须窄。ColumnExtractor 和 FK 图可以召回很多可能相关表，但进入完整 prompt 的只能是少数 required 表。否则召回越强，性能越差。

### fallback 应该是安全网，不是主能力

硬编码模板可以保底，但不能继续承担主路径。每个 fallback 触发都应该记录原因：planner 漏实体、schema 编译失败、Step2 FK 不闭环、LLM timeout。只有知道 fallback 为什么发生，才能逐步减少它。

### 优化目标不是更长 timeout，而是更小问题

codemaker serve 时间长，核心不是简单网络慢，而是发送给模型的问题太大。正确方向是缩小 prompt、减少候选、拆成两阶段，而不是把 timeout 从 40s 调到 90s 或 150s。

### 执行成功要以前置确定性为基础

Step3 不应该边写边发现问题。所有字段、行、主键、外键和占位符都应在 dry-run 中确定。写盘只是提交已经验证过的计划。

### P0：先解决 Step1 性能和职责边界

1. 建立 Step1 性能账本。

   每次请求记录并在 Step4 汇总：segment_count、candidate_count、rule_candidate_count、column_candidate_count、fk_expanded_count、fk_edge_count、schema_table_count、schema_sheet_count、schema_chars、prompt_chars、llm_calls、每次 LLM dur_ms、timeout_count。

2. 将候选表分为三级，而不是一个平铺 candidates。

   建议结构：

   - required：用户文本明确命中或高置信度必须写的表。
   - dependency：由 FK 推导出来的依赖表，只用于检查和补引用。
   - context：弱相关背景表，默认不进入完整 schema prompt。

   Decompose prompt 只注入 required 的完整 schema；dependency 只注入表名、主键、被引用列和少量关键列；context 默认不注入 schema。

3. 对 schema 做摘要化。

   不要把所有 row1/row2 表头整块塞给 LLM。先生成 table card：

   - 表用途一句话。
   - 主键列。
   - 必填列。
   - FK 列。
   - 与用户输入命中的列。
   - 常用字段别名。

   只有当 LLM 选择某张表进入具体写入计划时，再拉完整 schema。

4. 把 Step1 拆成 Planner 两阶段。

   第一阶段：LLM 只读用户输入和轻量 table cards，输出“需要哪些实体/表/关系”的 semantic plan。

   第二阶段：系统按 semantic plan 拉取必要 schema，LLM 或确定性 mapper 输出具体 column-level intents。

   这样可以让 LLM 先做它擅长的复杂语义理解，避免一上来被几十张表头淹没。

### P1：减少硬编码模板，保留为评测基线

1. 将 `cross_table_splitter.py` 中的领域模板降级为 benchmark oracle 或 fallback，不再作为主路径。

2. 把 `_try_domain_expander` 改为“可解释的 plan macro”，由 LLM 决定是否调用，而不是代码通过关键词直接命中。

3. 对每个硬编码模板建立对应复杂输入测试集。新 planner 必须在无模板情况下达到同等或更好结果，模板才允许删除。

4. 用数据驱动替代代码分支：把表关系、必填字段、默认值、常用链型写成 YAML/JSON skill/card，让 LLM 读取声明式知识，而不是把业务链写死在 Python 正则里。

### P2：强化 Step2 为确定性门禁

1. 默认开启 plan completeness hard gate 的灰度评估，统计误杀率。

2. Step2 产出标准化 correction request，而不是直接依赖 legacy ask/failure 形状。

3. 对字段不存在、类型不匹配、FK 悬空、producer 缺失建立统一错误码和可恢复策略。

4. Step2 发现 Step1 计划不完整时，应返回 replan hints：缺哪张表、哪条 FK、哪个 producer label，而不是仅报 validation issue。

### P3：让 Step3 更接近事务执行

1. 增加 dry-run：所有 intent 先解析目标文件、sheet、行定位、列映射、PK 分配、FK 替换，不写盘。

2. dry-run 全通过后再执行写入。

3. 写入前做 workbook 快照，任一子任务失败时可回滚整批文件。

4. 将 `_backfill_forward_refs` 前置到 dry-run 计划里，减少事后补写。

### P4：Step4 输出可行动诊断

1. Step4 总结增加“慢因摘要”：候选过多、schema 过长、LLM 超时、执行失败、等待确认分别归因。

2. anti-pattern 归纳默认异步或延后，不阻塞用户主链路。

3. 经验沉淀必须带来源样本、命中次数、回归结果和 promote 状态。

4. 最终消息区分用户视角和开发者视角，避免用户只看到“失败”，开发者却看不到足够定位信息。

## 建议落地顺序

第一阶段：加观测，不改主逻辑。把 candidate/schema/prompt/LLM 耗时指标打通到 Step4 和测试报告，先证明慢因。这个阶段只允许新增 trace，不允许改变 Step1 输出。

第二阶段：改 Step1 候选分层。限制完整 schema prompt，只让 required 表进入详细 schema，dependency 表只给 FK 摘要。这个阶段必须保留原 candidates 兼容字段，避免 Step2/3 同时大改。

第三阶段：引入两阶段 planner。先 semantic plan，再 column-level plan。新 planner 先 shadow，不直接写盘；和旧 Step1 输出做 diff，累计通过率。

第四阶段：Step2 hard gate 灰度。把计划不完整、悬空引用、字段幻觉提前阻断，并生成 replan hints。hard gate 先只对 shadow planner 生效，确认误杀率后再切主链路。

第五阶段：Step3 dry-run 和快照回滚。解决跨表部分写入风险。先实现 dry-run 报告，再打开真实回滚。

第六阶段：Step4 诊断产品化。让每次失败都能沉淀成可复现、可验证、可 promote 的优化输入。anti-pattern 归纳从同步链路移到异步或后台任务。

第七阶段：逐步下线硬编码模板。只有某类模板在金标和真实影子评测中都稳定被 semantic planner 覆盖，才把它从主 fallback 降级为测试 oracle。

## 验收指标

- 复杂输入 Step1 平均耗时下降 50% 以上。
- 单次 Decompose prompt 字符数下降 60% 以上。
- 候选表进入完整 schema prompt 的数量稳定控制在 3-5 张。
- LLM timeout 率下降到 5% 以下，且不靠提高 timeout 实现。
- Step1 不依赖硬编码模板时，复杂链路测试通过率不低于当前模板 fallback。
- 新 planner shadow diff 中，漏 intent、错表、错 FK 三类问题都有结构化归因。
- Step3 `llm_calls` 始终为 0。
- 跨表任务失败时无残缺写入，或可自动回滚到执行前状态。
- Step4 能明确给出慢因和失败归因，而不是只返回泛化失败文案。

## 最小可行改造清单

如果希望最快看到收益，建议先做这 6 个小闭环：

1. `StepTrace`：记录候选数、schema_chars、prompt_chars、LLM dur_ms，并在 Step4 展示。
2. `TableCardIndex`：为每个 sheet 生成轻量 card，先只用于日志和 shadow prompt。
3. `candidate_groups`：在 LocatorResult 中增加 required/dependency/context，不移除原 candidates。
4. `schema_budget`：Decompose prompt 增加硬预算，超预算时 dependency 只给 FK 摘要。
5. `SemanticPlanner shadow`：并行产 semantic plan，与旧 intents 对比，不参与执行。
6. `Step2 replan_hints`：把缺表、缺 producer、悬空 FK 变成结构化提示，供 planner 下一轮使用。

这 6 个点做完，就能判断新方向是否有效：如果 prompt 变短、timeout 下降、shadow plan 的实体图更完整，就继续推进；如果不达标，也能从 trace 里看出是召回、规划、schema grounding 还是编译的问题。

# Excel-Agent × Merge PPT 页面规划（适配当前答辩优化版）

> **目标 PPT**：`网易互娱通用PPT模板（含保密）_ExcelAgent答辩优化版.pptx`  
> **文档用途**：作为新建/重排 PPTX 的页面规划稿。不是把性能指标集中放一页，而是把“模块内容 → 优化方法 → 使用方式 → before/after 指标”分散进各模块页。  
> **适配原则**：沿用现有 PPT 的红黑白风格、`章节封面 → 模块解释 → 技术增强 → 指标框 → 验证结论` 结构。现有 PPT 已有 33 页内容页 + 后续模板页，本规划**第二版**扩充为 45 页内容页：新增 Skill 系统独立章节（3 页）+ 本轮代码优化亮点章节（5 页），并修复第一版“第 12 页”重复编号问题。

> **2026-09-01 修订说明**：本轮代码有一批未提交的真实优化改动（decompose_agent/validator_agent/step3_execute_subagent/operation_orchestrator/step4_conclude_subagent/agent_service 等），已逐一分析并用真实代码/构造 fixture 跑出模拟 before/after 数据（`tools/simulate_validator_forwardref_norm.py`、`simulate_placeholder_nested_backfill.py`、`simulate_agent_service_noise_filter.py`、`simulate_decompose_chain_cost.py`、`simulate_step4_allok_drift.py`，产出见 `bench/ppt_validator_forwardref_norm.md` 等 5 份报告），已并入第 22-26 页。Skill 系统（第 19-21 页）补齐机制讲解，避免只贴数字不讲原理。

---

## 0. 总体结构

| 章节 | 页码 | 内容 | 对应现有 PPT |
|---|---:|---|---|
| 封面与目录 | 1-2 | 项目定位、目录 | slide1-slide2 |
| 业务背景 | 3-5 | 痛点、目标、双引擎总览 | slide3-slide5 |
| Excel-Agent | 6-29 | 4-Step、Prompt、定位、rapidfuzz、解析、校验、执行、自学习、**Skill 系统**、**本轮优化亮点**、指标 | slide6-slide19 增强重排 + 新增 8 页 |
| Merge | 30-41 | 三方合并、merge-base、Compare、性能、写回、AI 建议、指标 | slide20-slide31 增强重排 |
| 验证与总结 | 42-45 | 回归、边界、总结、Q&A | slide32-slide33 + 结束页 |

> **页码策略**：性能优化数据嵌入每个模块，不新增“性能大杂烩”集中块。每个技术模块都包含：**讲什么 / 怎么做 / 用在哪里 / 优化指标 / 证据**。
> **指标口径**：真实端到端指标只在相关汇总页出现一次；Prompt/日志展示等缺少历史 A/B 的模块，使用 `tools/simulate_ppt_perf_metrics.py` 等系列脚本生成的“模拟压测/静态估算”补充，不与真实准确率混用。

---

# 第一章：封面与业务背景

## 第 1 页｜封面

**标题**：AI 配表助手  
**副标题**：自然语言驱动配表 + 三方合并 Merge  
**信息**：吴植贤｜2026 年 9 月 4 日

### 版式

- 使用现有 slide1 封面版式。
- 标题黑色大字，关键词 `AI` / `Merge` 用网易红强调。

---

## 第 2 页｜目录

| 章节 | 标题 | 一句话 |
|---|---|---|
| 01 | 业务背景与任务目标 | 为什么要做 |
| 02 | Excel-Agent 4-Step | 怎么把自然语言变成可执行配表 |
| 03 | Merge 三方合并 | 怎么把多人分支改动安全合并 |
| 04 | 验证、边界与总结 | 做到什么程度、哪里还有边界 |

### 版式

- 沿用现有 slide2 目录宫格。
- 目录中将“成果数据”改为“验证与性能”，因为性能分散到各模块。

---

## 第 3 页｜业务背景：真实配表复杂性

### 讲什么

复杂 Excel 配表不是单表编辑，而是跨表、跨目录、跨版本的一致性工程。

### 内容

| 问题 | 现状 | 后果 |
|---|---|---|
| 表多字段多 | 60+ Excel 表、跨表外键密集 | 人工查表慢、易漏 |
| 自然语言不稳定 | 策划说“新增活动、绑定奖励、配置技能” | 机器难定位表/列/行 |
| 多人并行 | SVN 目录分支、多人改同表 | 冲突漏合、覆盖数据 |
| 大表性能 | 10w 行 Excel 读写 | openpyxl 全量读写慢 |
| AI 误写风险 | LLM 可能幻觉表名/字段 | 写错后难回滚 |

### 讲法

> 难点不在“写一个单元格”，而在“知道应该写哪张表、哪列、哪行，并保证外键和合并后仍一致”。

---

## 第 4 页｜任务目标：改表 + 合表闭环

### 内容

| 引擎 | 目标 | 关键约束 |
|---|---|---|
| Excel-Agent | 自然语言 → 结构化意图 → 确定性写表 | 可校验、可回滚、可追踪 |
| Merge | SVN 分支 Excel 三方合并 | 保护公式、ID 引用、冲突可解释 |

### 核心主张

> 模型负责理解，规则负责约束，代码负责确定性。

### 版式

- 沿用现有 slide5 表格版式。
- 页面底部放一句“两个引擎共享同一套表结构知识”。

---

## 第 5 页｜双引擎总览：共同技术路线

### 内容

```text
用户自然语言 ──▶ Excel-Agent ──▶ 改表结果
                         │
                         ▼
                表结构 / 外键 / 约束知识
                         ▲
                         │
SVN From/To ───▶ Merge 三方合并 ──▶ 合并结果
```

### 共同方法论

| 方法 | Agent 中体现 | Merge 中体现 |
|---|---|---|
| 少问模型 | 规则定位、schema 编译、Step3 零 LLM | AI 只做冲突建议，不进核心策略 |
| 少读少写 | 表头缓存、索引定位 | calamine、sparse diff、fast_apply |
| 先校验后执行 | L0/L1/L2 校验 | 类型约束、引用完整性、公式保护 |
| 失败可恢复 | checkpoint、审计、回滚 | 临时目录、互斥锁、安全写回 |

---

# 第二章：Excel-Agent 4-Step

## 第 6 页｜章节封面：Excel-Agent 4-Step

**大字**：Excel-Agent：自然语言到多表配表  
**副题**：Parse → Validate → Execute → Conclude

### 版式

- 沿用现有 slide6 章节封面。

---

## 第 7 页｜Excel-Agent 总体架构

### 讲什么

Excel-Agent 不是让 LLM 直接改 Excel，而是把自然语言先编译成结构化 intent，再通过规则执行。

### 流程

```text
用户自然语言
   │
   ▼
Step1 Parse：拆意图、定位候选表、生成 JSON intent
   │
   ▼
Step2 Validate：schema / 类型 / 枚举 / FK / 必填校验
   │
   ▼
Step3 Execute：拓扑排序、占位符替换、确定性写盘
   │
   ▼
Step4 Conclude：汇总结果、失败归因、反模式沉淀
```

### 优化指标嵌入

| 架构指标 | 早期 | 当前/目标 | 变化 |
|---|---:|---:|---:|
| 一次配表 LLM 调用 | 6 步线性 23+ 次 | 4-Step 3-5 次设计目标 | **-80%** |
| 高复杂 6 样例 | 早期全失败/不稳定 | 6/6 OK，失败数 0 | 稳定通过 |

> 证据：`PERF_DATA_SUMMARY.md`、`bench/bench_iter_results.md`

---

## 第 8 页｜Prompt 工程：不是写一段提示词，而是约束输出协议

### 为什么新增这页

当前代码页没有专门讲 Prompt，但 Agent 成败核心之一就是：**Prompt 如何让 LLM 输出可执行 JSON，而不是自然语言解释**。

### Prompt 设计内容

| 组件 | 内容 | 作用 |
|---|---|---|
| 角色设定 | 你是 Excel 配表拆解专家 | 限定任务边界 |
| 真实 schema 注入 | 表名、sheet、列名、类型、FK、枚举 | 防止幻觉字段 |
| 输出协议 | JSON array：`action/table/sheet/fields/produces/consumes` | 后续可执行 |
| 反例约束 | 不输出不存在列；不把叙述碎片当字段值 | 降低污染 |
| few-shot 示例 | 给 add/modify/delete/跨表引用例子 | 提升结构稳定性 |
| 截断策略 | 只注入候选表 schema，不塞全库 | 降 token、降超时 |

### 使用位置

- `DecomposeAgent`：自然语言 → intent JSON。
- `LocatorAgent`：候选表歧义时裁决。
- `StepAIEnhancer` / AI 判官：仅在规则无法判断时启用。
- `Merge AI Suggest`：冲突建议，输出建议来源、理由、置信度。

### 优化指标

| 优化点 | 优化前 | 优化后 | 直接收益 |
|---|---:|---:|---|
| Prompt 上下文瘦身（模拟估算） | 全量表索引 **76.11MB / 约1995万 token** | 单任务聚焦 schema **510 字符 / 约159 token** | 避免全量上下文爆炸 |
| 候选表裁剪（真实日志） | 12 张候选表直接进拆解 | 只保留 TopK + 分组拆解 | prompt 规模可控 |
| 输出格式约束 | 自由文本，解析不稳 | 固定 JSON 协议 | 可执行、可校验 |
| LLM 调用收敛（历史目标） | 23+ 次 | 3-5 次 | 调用量 **-80%** |
| 规则前置校验 | 多处靠 LLM 判断 | L0 规则先判，Step3 零 LLM | 执行更稳定 |

### 讲法

> Prompt 不是“让模型更聪明”，而是“把模型关进真实 schema 和 JSON 协议里”。

---

## 第 9 页｜Step1 Parse：自然语言拆解成可执行计划

### 讲什么

Step1 是全链路核心：自然语言一旦拆错，后面校验和执行都会被拖下水。

### 技术内容

| 步骤 | 技术 | 说明 |
|---|---|---|
| 多意图切分 | 规则粗分段 + 低置信 LLM 重切 | 先把长输入切小，但不把规则结果当最终真相 |
| 候选表定位 | 规则 + 模糊匹配 + 关系扩展 | 先零 LLM 找候选 |
| schema 注入 | 真实表头、类型、FK | 让 LLM 对着真实结构拆 |
| JSON 拆解 | DecomposeAgent | 输出结构化 intents |
| produces 推断 | FK 图 + 主键规则 | 给跨表依赖建立契约 |

### 优化方法

- 用 schema 限制字段选择，降低幻觉列。
- 用关系图扩展候选表，解决用户不说表名的问题。
- 用 produces/consumes 描述跨表依赖，不靠模型记忆 ID。
- 对空返回、重复影子行、字段错表做 schema-aware 编译修复。

### 指标框

| 指标 | before | after | 变化 |
|---|---:|---:|---:|
| 定位功能（8 条 NPC/interaction 用例） | 0.6667 | 0.8056 | **+20.8%** |
| 覆盖度（8 条 NPC/interaction 用例） | 0.6111 | 0.7500 | **+22.7%** |
| Step1/Step2 展示日志（模拟压测） | 51 行 / 2340 字 | 5 行 / 205 字 | **行数 -90.2%** |

> 证据：桌面 `优化全过程.md` R1；`bench/ppt_module_perf_simulated.md`

---

## 第 10 页｜LocatorAgent：三级定位与 RAG 思路

### 讲什么

用户不会准确说 `pet.xlsx/Pet`，系统要把“灵兽、宠物、饕餮”映射到真实表。

### 技术内容

| 层级 | 方法 | 作用 |
|---|---|---|
| L1 规则通道 | 表名、中文表头、别名、字段关键词 | 零成本直接命中 |
| L2 模糊通道 | rapidfuzz 混合分数 | 处理错别字/简称 |
| L3 关系扩展 | FK 邻接表、依赖表扩展 | 把主表相关配置表带出来 |
| L4 模型裁决 | 候选冲突时 LLM 判断 | 只在歧义时付费 |

### Prompt 使用

- 输入：用户原句 + TopK 候选表 + 每表简要 schema。
- 输出：候选表排序 + 理由。
- 防幻觉：只能从候选表中选，不能生成新表名。

### 优化指标

| 优化项 | before | after |
|---|---:|---:|
| skill 定位成功率（30 样例） | 93.3% | **100%** |
| skill 平均耗时 | 533ms | 321ms |
| rapidfuzz 编辑距离算子（模拟压测） | 2781.54ms | 192.16ms | **14.5×** |

### 讲法

> 定位层像“查地图”：先查别名，再查模糊词，最后顺着外键关系找相邻表。

---

## 第 11 页｜rapidfuzz 混合指标：列名模糊匹配加速

### 讲什么

用户输入经常不是表头原文，比如“目标类型”要命中“任务目标类型”，“模型id”要接近“模板ID / model_id”。rapidfuzz 负责把这些口语化字段名快速拉进候选集。

### 混合打分公式

```text
score = WRatio × 0.5 + token_set_ratio × 0.3 + partial_ratio × 0.2
```

| 指标 | 作用 |
|---|---|
| `WRatio` | 综合编辑距离、长度差、部分匹配，适合通用相似度 |
| `token_set_ratio` | 忽略词序和重复词，适合“任务目标类型 / 目标类型” |
| `partial_ratio` | 长字段里包含短查询时仍能命中 |

### 代码落点

| 模块 | 使用方式 |
|---|---|
| `fuzzy_matcher.py` | 值候选搜索：子串 0.5 + 编辑距离 0.35 + 字符重叠 0.15；rapidfuzz 不可用则回退纯 Python |
| `column_matcher.py` | 列名匹配：`WRatio×0.5 + token_set×0.3 + partial×0.2`，命中后进入 TopK 候选 |
| `table_locator.py` | 表/Sheet 定位中调用模糊候选，减少 LLM 裁决次数 |

### 性能增强（模拟压测）

> 真实 `_table_index.json`：1065 个列名；14 个中文/中英混合查询词；重复 20 轮，合计 298200 次编辑距离计算。

| 指标 | 纯 Python Levenshtein | rapidfuzz C++ 算子 | 收益 |
|---|---:|---:|---:|
| 编辑距离计算 | 2781.54ms | 192.16ms | **14.5×** |
| 单次匹配平均 | 0.0093ms | 0.0006ms | **约 14.5×** |
| Top5 命中 | 14/14 | 14/14 | 保持召回 |

### 业务收益

- **快**：候选表/字段过滤更快，减少 Step1 前置等待。
- **稳**：错别字、简称、长字段包含短词时仍能进候选集。
- **省 LLM**：模糊候选足够明确时，不必把歧义交给模型。

> 证据：`tools/simulate_rapidfuzz_metrics.py`、`bench/ppt_rapidfuzz_metrics.md`。该页为“模拟压测”口径，用来说明 rapidfuzz 算子性能，不替代端到端准确率。

---

## 第 12 页｜DecomposeAgent：schema 注入拆解 JSON

### 讲什么

DecomposeAgent 是 Prompt 工程核心页：把自然语言变成可执行 JSON。

### 输入内容

```text
用户原句
+ 候选表真实 schema
+ 多级表头 row1/row2
+ 字段类型 / 枚举 / FK
+ 输出 JSON 协议
+ few-shot 示例
```

### 输出协议

```json
{
  "action": "add",
  "table": "activity.xlsx",
  "sheet": "Activity",
  "fields": {
    "activity_id": 3001,
    "name": "焚天赤龙"
  },
  "produces": ["<activity_id>"],
  "consumes": []
}
```

### 优化方法

| 问题 | 方法 |
|---|---|
| LLM 编不存在字段 | schema 注入 + 字段白名单 |
| LLM 输出自然语言解释 | 强制 JSON array |
| 跨表 ID 记不住 | produces/consumes 占位符协议 |
| 长输入 prompt 太大 | 候选表裁剪 + schema 摘要 |

### 指标

| 指标 | before | after |
|---|---:|---:|
| 字段精准率（8 条 NPC/interaction 用例） | 0.5185 | 0.6917 |
| 严格通过率（8 条 NPC/interaction 用例） | 0.1667 | 0.3333 |

---

## 第 13 页｜produces/consumes：跨表依赖契约

### 讲什么

跨表新增时，后续表依赖前面新建行的 ID。不能让模型“记住”，要让代码契约传递。

### 示例

```text
新增活动 activity_id=3001
新增奖励 reward_id=30010，绑定 activity_id=3001
新增邮件 mail_id=5001，附件 reward_id=30010
```

### 契约

| 字段 | 含义 | 用途 |
|---|---|---|
| `produces` | 当前 intent 产生的主键 | 供后续引用 |
| `consumes` | 当前 intent 依赖的占位符 | 决定执行顺序 |
| placeholder | `<activity_id>` / `<reward_id>` | 执行时替换真实值 |

### 优化方法

- Step1 推断依赖。
- Step3 拓扑排序。
- 执行前扫描 unresolved placeholder，避免孤儿行。

### 指标

| 指标 | before | after |
|---|---:|---:|
| quest_npc 引用一致 | 0.62 | **1.00** |
| quest_npc 耗时 | 134s | 107s |

---

## 第 14 页｜Step1 质量门禁：schema-aware 编译

### 讲什么

LLM 输出后不能直接执行，要再编译一遍。

### 检查内容

| 检查 | 修复方式 |
|---|---|
| 字段不在 schema | 删除/映射到真实列 |
| sheet/name 混用 | 统一到真实 sheet |
| 同 sheet 稀疏影子重复 | 合并/删除重复 intent |
| 孤立 add intent | 删除空壳 |
| placeholder 悬空 | 标记 hard error |
| 自产自消 / 依赖环 | 阻断执行 |

### 优化指标

| 优化项 | before | after |
|---|---:|---:|
| type_mismatch | 2 | 1 |
| 失败 trace 归纳反模式 | 0 | 4 类 |

---

## 第 15 页｜Step2 Validate：L0/L1/L2 三层校验

### 讲什么

写盘前校验。大多数错误不需要 LLM。

### 三层校验

| 层级 | 方法 | 处理内容 |
|---|---|---|
| L0 规则闸 | 纯代码 | 类型、枚举、必填、PK、FK、占位符 |
| L1 AI 判官 | 1 次 LLM | NL 覆盖度、值合理性 |
| L2 人工 gate | 交互卡片 | 用户改字段/值后续跑 |

### 优化方法

- 把“校验靠模型”改成“规则先判”。
- 硬错误阻断，软错误展示但不全盘失败。
- 可编辑卡片让用户修正字段后重新跑 Step2。

### 指标

| 指标 | before | after | 变化 |
|---|---:|---:|---:|
| 精准程度（8 条 NPC/interaction 用例） | 0.5185 | 0.6917 | **+33.4%** |
| 严格通过率（8 条 NPC/interaction 用例） | 0.1667 | 0.3333 | **+100%** |
| L0 规则覆盖 | — | 约 80% 关切 | 少调用 LLM |

---

## 第 16 页｜Step2 交互修正：从报错到可恢复

### 讲什么

校验失败不等于流程失败，而是进入可恢复编辑。

### 使用内容

| 用户能改 | 说明 |
|---|---|
| 列名 | 选真实 schema 列 |
| 字段值 | 修正类型/枚举 |
| 删除字段 | 去掉误抽取内容 |
| 删除 intent | 去掉不该执行任务 |
| 重新校验 | POST `/api/agent/reply` 续跑 |

### 优化方法

- 后端把用户编辑回写原 intent。
- 重新跑 Step2，而不是绕过错误。
- 保留审计记录，方便复盘。

### 指标口径

- 本页主要讲可靠性，不强行写性能百分比。
- 可引用“回归 180 项全过、单测 323+ 零回归”。

---

## 第 17 页｜Step3 Execute：确定性写盘，执行层零 LLM

### 讲什么

执行不是推理任务，必须确定性。

### 执行内容

| 动作 | 技术 |
|---|---|
| 拓扑排序 | producer 先于 consumer |
| ID 分配 | 主键自动分配/冲突检测 |
| 占位符替换 | `<reward_id>` → 真实 ID |
| 原子写盘 | 二段提交/批量原子 |
| 失败归因 | `type_mismatch` / `pk_conflict` / `field_fail_ratio` |

### 优化方法

- `no_llm=True`，执行层短路所有 LLM 路径。
- thread-local 隔离，避免并发请求污染环境变量。
- 悬空 placeholder 执行前扫描，跳过孤儿行。

### 指标

| 用例 | before | after | 变化 |
|---|---:|---:|---:|
| 复杂级联 case106 | 204s | 112s | **-45%** |
| LLM 往返 | 5 次左右 | 2-3 次 | 减少调用 |

---

## 第 18 页｜Step4 Conclude：结果汇总与反模式自学习

### 讲什么

Step4 不只是总结，它把失败转成可复用经验。

### 内容

| 功能 | 说明 |
|---|---|
| 汇总结果 | 成功数、失败数、写入行、预览 |
| 失败归因 | 定位失败发生在 parse/validate/execute 哪层 |
| anti_patterns | 从失败 trace 归纳反模式 |
| mini_regression | 失败样例进入小回归集 |

### 优化方法

- 失败不是一次性日志，而是沉淀规则。
- 后续 Prompt 注入反模式提醒，避免重复犯错。

### 指标（对比实验）

> 口径：`tools/simulate_step4_self_learning_ab.py` 离线回放历史 8 条失败 trace。衡量“同类失败能否在下次写盘前被反模式规则命中/预警”，不替代端到端成功率。

| 阶段 | 反模式状态 | 同类失败预警覆盖 | 说明 |
|---|---|---:|---|
| Before | 无反模式 | 0/8（0%） | 失败只汇总，不复用 |
| Pending | pending_review | 0/8（0%） | 候选不生效，防止学坏 |
| After | active | 8/8（100%） | 同类输入写盘前预警/确认 |

| 归纳效率 | 数值 |
|---|---:|
| 输入失败 trace | 8 条 |
| 归纳反模式 | 4 类 |
| 压缩率 | 50% |
| 预警覆盖提升 | +100pts |

### 归纳出的 4 类反模式

| 反模式 | 动作 | 覆盖问题 |
|---|---|---|
| `NPC,新增,space_id,model_id,坐标,放在` | require_confirm | NPC 场景新增时 PK / seed 冲突 |
| `修改,删除,prefab_id,interaction_id` | warn_only | 按 ID 修改/删除时 seed 缺失 |
| `conv_id,对话` | warn_only | conv_id 行定位失败 |
| `NPC,对话,选项` | block_dry_run | 对话选项类型转换失败 |

> 证据：`bench/ppt_step4_self_learning_ab.md`、`docs/archive/优化全过程.md` R2.6。

---

## 第 19 页｜Skill 系统①：表结构知识底座是什么

### 讲什么

Excel-Agent 不是每次都让 LLM 从零猜表结构，而是维护一套持续学习的“表结构知识底座”：把列叫什么、行怎么定位、哪些操作历史上出过错，沉淀成可查询规则，Agent 与 Merge 共用同一份，避免元数据漂移。

### 四层结构

| 层 | 目录 | 内容 | 生成方式 |
|---|---|---|---|
| L0 人工/根目录 | `skills/*.yaml` | sheet 别名、引导动词、列短形式 | 人工 + 自动混合 |
| L1 自动派生 | `L1_derived/` | 列别名、行定位规则、多 sheet 消歧、类型约束、级联/枚举/必填 | 扫描 `resources/*.xlsx` 结构自动生成 |
| L2 运行时 | `L2_runtime/` | 运行时学到的列别名（带 hits/confidence）、跨表关系权重 | 从 evidence promote 写入 |
| L3 反模式 | `L3_anti_patterns/` | 失败模式规则：歧义列/类型约束/失败操作/语义模式 | 规则频次升级 + AI 归纳 |

### 真实样例

```yaml
# L1_derived/column_aliases.yaml
名字: 名称
名: 名称
编号: id

# L3_anti_patterns/anti_patterns.yaml
- id: pet_name_ambiguous
  type: ambiguous_column
  table_stem: pet
  column: 名称
  action: force_exact
  status: active
```

### 讲法

> Skill 不是缓存，是“会犯错也会改错”的知识库：人工策展优先，自动学习靠阈值和回归门禁把关，不会脱缰。

> 证据：`server/agent/excel/core/skill_loader.py`、`skills/L3_anti_patterns/anti_patterns.yaml`

---

## 第 20 页｜Skill 系统②：生成与审核机制——学错了能撤，坏了不扩散

### 数据流

```text
每次操作 evidence
     │
     ▼
ingest_evidence()──┬─▶ 列别名候选 → _pending/column_alias_candidates.jsonl
                    ├─▶ 反模式信号 → _pending/anti_pattern_signals.jsonl
                    └─▶ 跨表关系 co_occur++
     │（每 20 次批量触发）
     ▼
try_promote() / promote_pending_anti_patterns()
     │
     ▼
promote_with_guard：快照 → 写盘 → mini 回归(30 样本 on/off) → lift≥0.05 达标
     │                                            │
     ▼ 通过                                       ▼ 不达标
   active（正式生效）                          回滚 + 隔离区 quarantine（30天/7天冷却）
```

### 关键阈值（真实代码取值）

| 判定 | 阈值 |
|---|---|
| 列别名候选转正 | 同 (table,sheet,query→resolved) 命中 ≥3 次 / 7 天窗口 / 置信度 [0.3,0.75) |
| 反模式升级 active | ambiguous ≥3 次 / 7 天 → force_exact |
| 反模式升级 pending_review | failed_operation ≥2 次 / 30 天 → require_confirm |
| AI 归纳反模式转正 | 需额外命中 3 次（比确定性规则更严，因无 ground-truth） |
| 冲突消解 | 同 query 指向 ≥2 个不同 resolved → 不入库，人工 review |
| 衰减 | 60 天无新命中移除；权重 <0.3 降 dormant |

### 讲法

> AI 归纳的反模式永远先进 `pending_review`，不直接生效；只有跑过小回归、确认没把已有能力改坏，才能转正。这是 Skill 系统敢“自动学习”的前提。

> 证据：`server/agent/excel/core/skill_updater.py`（D7.3 促升阈值 / D8.2 反模式升级 / T9 衰减 / T10 promote_with_guard 安全阀）

---

## 第 21 页｜Skill 系统③：效果数据

### 内容

| 数据点 | 指标 | off/before | on/after | 来源 |
|---|---|---:|---:|---|
| skill 简版 30 样例 | 定位成功率 | 0.933 | **1.000** | `PERF_DATA_SUMMARY.md` §1.6 |
| skill 简版 30 样例 | 精确命中 | 0 | **1.00** | 同上 |
| skill 简版 30 样例 | 严格综合 | 0.704 | **0.959** | 同上 |
| skill 简版 30 样例 | 平均耗时 | 533ms | **321ms** | 同上 |
| skill 真写盘 8 样例 | 定位 | 0.472 | 0.639 | `PERF_DATA_SUMMARY.md` §1.7 |
| skill 真写盘 8 样例 | 覆盖 | 0.417 | 0.583 | 同上 |
| skill 真写盘 8 样例 | 精准 | 0.497 | 0.590 | 同上 |
| Step4 反模式沉淀（模拟回放） | 同类失败预警覆盖 | 0/8（0%） | **8/8（100%）** | `bench/ppt_step4_self_learning_ab.md` |
| Step4 反模式归纳（真实 e2e） | 8 条失败 trace → 4 类反模式 | — | 命中 PK 冲突/seed 缺失/conv_id 定位/类型转换 4 类 | `docs/archive/优化全过程.md` R2.6 |

### 诚实边界

> 早期版本 skill 曾出现负收益（jj 08-03 locate -0.024），后续 R1-R9 系统性优化后转正，勿混淆版本；`L2_runtime/column_aliases.runtime.yaml` 当前样例库仍是空骨架——机制已建成，尚未跑出规模化运行数据，如实说明比硬凑数字更可信。

---

## 第 22 页｜本轮优化亮点①：命名风格漂移不再误报悬空引用

### 讲什么

`validator_agent.py` 本次改动新增 `_norm_name` 归一化，修复"produces/consumes 命名风格不一致（大小写/驼峰下划线/首尾空白）时被误判为悬空引用 FORWARD_REF_BROKEN"的假阳性问题。

### 模拟压测（真实调用 `_norm_name`）

| 指标 | Before（精确字符串匹配） | After（`_norm_name` 归一化） |
|---|---:|---:|
| 假阳性 FORWARD_REF_BROKEN（12 组样例中应匹配却误报） | 5/6 | **2/6** |
| 漏检悬空引用（不该匹配却匹配，正确性无退化） | 0/6 | 0/6 |

### 典型场景

| produces | consumes | 应匹配 | before | after |
|---|---|---|---|---|
| `New_Quest_Id` | `<new_quest_id>` | ✅ | ❌ 误报悬空 | ✅ |
| ` new_npc_id ` | `<new_npc_id>` | ✅ | ❌ 误报悬空 | ✅ |
| `new_pet_id` | `<new_reward_id>` | ❌ | ❌ 正确判悬空 | ❌ 正确判悬空 |

> 证据：`tools/simulate_validator_forwardref_norm.py`、`bench/ppt_validator_forwardref_norm.md`（模拟压测口径，构造 12 组命名漂移场景，真实调用仓库函数，不代表端到端准确率）

---

## 第 23 页｜本轮优化亮点②：嵌套占位符扫描 + 循环依赖回填闭环

### 讲什么

对话树场景（NPC 对话 ↔ 对话选项互相引用）里，占位符常年被塞进嵌套 `dict/list` 字段（如 `Quest.target.data.npc_id`），旧逻辑只扫顶层字符串，嵌套字段里的悬空占位符完全"看不见"。本次把 `step3_execute_subagent.py` 的占位符扫描、`operation_orchestrator.py` 的依赖图构建都改成递归扫描，并新增循环结束后的 `_backfill_forward_refs` 回填机制，解决循环依赖对话树里"先跑的因为后面才产出而失败"的问题。

### 模拟压测（真实调用 `_find_unresolved_placeholders`）

| 指标 | Before（只扫顶层字符串） | After（递归扫描 dict/list/tuple） |
|---|---:|---:|
| 8 条混合样例中漏检的嵌套占位符 | **7** | **0** |

### 循环依赖回填（简化状态机模拟，非直接调用生产代码）

| 指标 | Before（无 backfill） | After（跑一轮回填） |
|---|---:|---:|
| 5 节点链式依赖，可执行节点数 | 1/5 | **5/5** |

> 证据：`tools/simulate_placeholder_nested_backfill.py`、`bench/ppt_placeholder_nested_backfill.md`

---

## 第 24 页｜本轮优化亮点③：聊天区降噪——从"看日志"到"看结果"

### 讲什么

`agent_service.py` 新增 `_is_user_visible_progress`（黑名单正则过滤内部调试文案）+ `_intent_list_summary`（把 intent 列表转成"✅已解析N个子任务…命中表/Sheet…"的结构化摘要），并把展示行数上限收到 8 行。这是最容易让人"看到优化"的一页——直接对比同一段真实会话过滤前后的样子。

### 模拟压测（真实调用生产函数源码）

| 指标 | Before（全展示） | After（过滤 + 摘要 + 截断 8 行） |
|---|---:|---:|
| 展示行数（25 条混合会话） | 24 | **8** |
| 噪音行数 | 16 | **0** |
| 噪音占比 | 66.7% | **0.0%** |

### 直观对比

```text
Before：DecomposeAgent 单prompt主路径(12表,阈值3,timeout=120s) / LocatorAgent 开始探测候选表 /
       疑似 serve hang（LLM调用计数长时间未变化）… （24 行内部调试文案混杂结果）

After：✅ 已解析 5 个子任务（add4条、set1条）
      命中表/Sheet：entity_prefab/Base、interaction/InteractionConv…
      已成功新增 NPC「铁匠老张」到 entity_prefab 表 …（8 行，只留用户能懂的内容）
```

> 证据：`tools/simulate_agent_service_noise_filter.py`、`bench/ppt_agent_service_noise_filter.md`

---

## 第 25 页｜本轮优化亮点④：DecomposeAgent 调用成本控制 + dict 列误清空修复

### 讲什么

两个独立修复：① 链式分组默认组数 4→3、新增硬上限 `CODEMAKER_DECOMPOSE_CHAIN_GROUP_MAX=2`，控制 LLM 子调用次数；② 修复"schema 里真实 dict/map/json 类型列（如 `Quest.target.data`）被一刀切清空"的正确性 bug，新增类型判断保留合法嵌套值。

### 模拟压测

| 指标 | Before | After |
|---|---:|---:|
| 12 候选分组场景：实际跑的分组数/预估 LLM 子调用 | 3 | **2** |
| 12 候选分组场景：交给规则兜底而非 LLM 的候选数 | 0 | 6 |
| dict 类型列误清空率（真实调用 lint 函数） | 100%（3/3 误清空） | **0%（0/3）** |

### 讲法

> 省调用次数和保数据正确性是两件事：前者是"少问模型"的成本工程，后者是"模型输出后再校验一遍"的正确性工程，本次一起修。

> 证据：`tools/simulate_decompose_chain_cost.py`、`bench/ppt_decompose_chain_and_dict_lint.md`

---

## 第 26 页｜本轮优化亮点⑤：Step4 口径修复 + 配套修复清单

### Step4 all_ok 口径漂移修复

`step4_conclude_subagent.py` 的 `all_ok` 曾经"仅镜像 s3.ok"，本次改为"s1/s2/s3 全部 ok 才算 ok"。用 2³=8 种组合穷举真值表，真实调用 `Step4ConcludeSubAgent.execute()`：

| s1.ok | s2.ok | s3.ok | Before all_ok | After all_ok | 本次修复覆盖 |
|---|---|---|---|---|---|
| True | False | True | True | **False** | ⚠️ 是 |
| False | True | True | True | **False** | ⚠️ 是 |
| False | False | True | True | **False** | ⚠️ 是 |

> 8 种组合中 **3 种**曾被旧逻辑误报为"全部成功"（实际 Step1/Step2 有失败），本次全部纠正。

### 配套修复清单（一句话带过，不单独展开）

| 模块 | 修复内容 |
|---|---|
| `parse_agent.py` | 多段指令的 LocatorResult 合并逻辑：候选/FK 边取最高置信度并去重，不再只保留第一段 |
| `step2_validate_subagent.py` | `modify` 动作补入白名单，不再被误判 `invalid_action` |
| `core/agent.py` | dict 值写 Excel 前序列化为 JSON 字符串，避免崩溃/写坏；跳过的子任务不再静默消失 |
| `schema_bundle.py` | 表头解析兼容全角冒号「：」/换行，修复列名错位 |

> 证据：`tools/simulate_step4_allok_drift.py`、`bench/ppt_step4_allok_drift.md`；配套修复见对应源码文件 git diff

---

## 第 27 页｜Excel-Agent 指标汇总

### 内容

| 指标 | before | after | 说明 |
|---|---:|---:|---|
| LLM 调用 | 23+ 次 | 3-5 次设计目标 | 4-Step 架构 |
| 定位功能 | 0.6667 | 0.8056 | 8 条 NPC/interaction 用例 |
| 覆盖度 | 0.6111 | 0.7500 | 8 条 NPC/interaction 用例 |
| 精准程度 | 0.5185 | 0.6917 | 8 条 NPC/interaction 用例 |
| 严格通过率 | 0.1667 | 0.3333 | 8 条 NPC/interaction 用例 |
| quest_npc 引用一致 | 0.62 | 1.00 | 单样例深化 |
| 复杂 case106 耗时 | 204s | 112s | S-B 复杂集 |
| Step1/Step2 展示日志 | 51 行 | 5 行 | 模拟压测，行数 -90.2% |
| 6 高复杂样例 | — | 6/6 OK | `bench_iter_results.md` |
| skill 简版定位成功率 | 0.933 | 1.000 | 30 样例，确定性零波动 |
| FORWARD_REF_BROKEN 假阳性 | 5/6 | 2/6 | 命名归一化模拟压测 |
| 嵌套占位符漏检 | 7/8 | 0/8 | 递归扫描模拟压测 |
| 聊天区展示行数/噪音占比 | 24 行/66.7% | 8 行/0% | 降噪模拟压测 |
| Step4 all_ok 误报场景 | 3/8 | 0/8 | 口径修复真值表 |

### 边界

> Agent `<90s` 是架构目标，不作为复杂跨表实测指标；复杂链路受外部 `codemaker serve` 阻塞影响。本页新增 4 项为本轮代码优化的模拟压测数据，口径为"构造 fixture + 真实函数调用"，不与真实端到端准确率混用。

---

## 第 28 页｜Excel-Agent 使用演示页

### 使用流程

```text
1. 用户输入自然语言
2. 前端展示 Step1 拆解 intent
3. Step2 若有问题，展示可编辑修正卡
4. 用户确认后 Step3 写盘
5. Step4 返回结果、失败归因、可回滚证据
```

### 演示案例

“新增焚天赤龙世界 BOSS 活动，配置首杀奖励、掉落池、战斗模型、NPC 对话引导。”

### 展示重点

- 多表意图自动拆解。
- produces/consumes 依赖排序。
- 修正卡片可人工纠偏。
- 写盘后有审计/回滚。

---

## 第 29 页｜Excel-Agent 小结

### 一屏五 ★

| # | 技术 | 价值 |
|---|---|---|
| 1 | Prompt + schema 注入 | LLM 不再自由发挥 |
| 2 | LocatorAgent 三级定位 | 用户不懂表名也能命中 |
| 3 | produces/consumes | 跨表依赖可执行 |
| 4 | L0/L1/L2 校验 | 大部分错误写盘前拦截 |
| 5 | Step3 零 LLM | 执行确定性、可回滚 |

---

# 第三章：Merge 三方合并

## 第 30 页｜章节封面：Merge 三方合并

**大字**：Merge：面向 SVN 目录分支的 Excel 三方合并  
**副题**：可信 Base + 稀疏 Diff + 引用保护 + 安全写回

---

## 第 31 页｜三方合并模型

### 讲什么

没有 base，就无法判断谁改了什么。

```text
        merge-base
        /        \
     source     target
        \        /
      merge result
```

### 内容

| 概念 | 含义 |
|---|---|
| base | 两个分支共同祖先 |
| source | 要合入的一侧 |
| target | 被合入的一侧 |
| result | 合并结果 |

### 使用场景

- 跨分支合并：dev → trunk / dev1 → dev2。
- 目录合并：子目录合回父目录。

---

## 第 32 页｜SVN merge-base：copyfrom LCA 反查

### 讲什么

SVN 没有 Git 那样的 merge-base，需要从 `copyfrom` 历史自己找共同祖先。

### 方法

| 步骤 | 技术 |
|---|---|
| 查出生点 | `svn log -v --stop-on-copy --xml` |
| 构建祖先链 | 沿 copyfrom 一层层上溯 |
| 求交叉点 | 两条链求 LCA |
| 降级 | inferred rev / 手工 override |

### 优化/可靠性

- 不依赖不可靠的 `svn mergeinfo`。
- 找不到就报错，不瞎猜 base。

### 指标

| 优化 | before | after |
|---|---:|---:|
| SVN rev 查询 | N 次 `svn log` ~190s | `svn info -R --xml` ~0.2s |

---

## 第 33 页｜Compare 全流程：四层比较

### 内容

| 层级 | 比较内容 |
|---|---|
| 文件层 | 新增、删除、重命名、移动 |
| Sheet 层 | sheet 新增/删除、表头变化 |
| 行列层 | 主键匹配、行增删、字段变化 |
| 语义层 | 公式、FK 引用、ID 冲突、可合并性 |

### 优化方法

- 第一列主键对齐。
- 行分类：matched / inserted / deleted / missing_row。
- 单元格分类：unchanged / changed / conflict / formula。

---

## 第 34 页｜Compare 性能：哈希索引 + 稀疏化 + 向量化

### 讲什么

10w 行不能逐格 Python 循环。

### 技术

| 技术 | 解决瓶颈 | 效果 |
|---|---|---|
| 哈希索引 | `list.index` O(n) 线性扫 | 行定位 O(1) |
| sparse diff | 全表物化 100 列 | 只保留变化格 |
| numpy 向量化 | Python 逐格 `_semantic_key` | 批量比较 |
| 公式/批注回退 | 向量化不懂公式 | 正确性兜底 |

### 指标

| 指标 | before | after |
|---|---:|---:|
| 大表 compare | ~48s | ~6.4s |
| 10 万行全链 | 卡死 | ~10.4s |
| 提交数据量 | 46.4MB | 0.1MB |

---

## 第 35 页｜语义归一：假冲突归零

### 讲什么

Excel 中 `100`、`100.0`、`"100"` 看起来不同，但语义相同。

### 规则

```python
def _semantic_key(v):
    if v is None: return ('none', '')
    if isinstance(v, bool): return ('bool', v)
    if isinstance(v, (int, float)): return ('num', float(v))
    s = str(v).strip()
    try: return ('num', float(s))
    except: return ('str', s)
```

### 优化方法

- 判断相等时归一化。
- 不改变原始值。
- bool 单独处理，避免 `True == 1` 误判。

### 指标

| 指标 | before | after |
|---|---:|---:|
| 假冲突率 | 0.5 | 0 |

---

## 第 36 页｜ID 重映射与引用完整性

### 讲什么

多分支同时新增同一 ID，要拆行重映射，并同步更新引用列。

### 内容

| 问题 | 解法 |
|---|---|
| 同主键不同分支新增 | 后到者分配新 ID |
| 外键引用旧 ID | 同步更新引用列 |
| 悬空引用 | 引用完整性校验 |
| 主表改 ID、引用表未同步 | 按 FK 图级联更新 |

### 指标

| 指标 | before | after |
|---|---:|---:|
| 多分支新增同主键 | 可能覆盖/冲突混乱 | 自动拆行 + 新 ID |
| 引用列同步 | 只改主键，外键可能悬空 | 主键与 FK 一起更新 |
| 悬空引用 | 事后人工发现 | 写回前校验拦截 |

---

## 第 37 页｜公式缓存保护：zip 快扫 + LibreOffice 重算

### 讲什么

Excel 公式和缓存值分离，直接写盘可能造成缓存丢失。

### 方法

```text
zip 扫描 xlsx XML 找 <f> 公式标签
    ↓
保存前后公式快照对比
    ↓
发现 lost/changed
    ↓
LibreOffice 临时目录重算
    ↓
校验后替换源文件
```

### 优化方法

- 无公式表直接跳过，零开销。
- 有公式表才走保护链。
- 互斥锁 + 临时文件，避免写坏。

### 指标

| 指标 | before | after |
|---|---:|---:|
| 10w 行公式检测 | openpyxl 全量读 ~6s | zip 扫描 ~0.05s |

---

## 第 38 页｜fast_apply：大表写回快路径

### 讲什么

写回不应全量 load/save workbook；只 patch 变化 sheet XML。

### 技术内容

| 环节 | 优化 |
|---|---|
| 变更收集 | 只保留 changed cells |
| XML 修改 | 直接 patch `sheet*.xml` |
| 样式/公式 | 不动未变区域 |
| 安全写回 | 临时文件 + 原子替换 |

### 指标

| 指标 | before | after |
|---|---:|---:|
| apply 处理 | ~19.6s | ~4.3s |
| 上传 payload | 46.4MB | 0.1MB |
| 序列化 | 4.9s | 0.3s |

---

## 第 39 页｜AI 辅助冲突建议：AI 只做建议，不做裁决

### 讲什么

Merge 核心策略是确定性代码，AI 只帮助用户理解冲突。

### Prompt 内容

| 输入 | 输出 |
|---|---|
| base/source/target 值 | 建议保留哪侧 |
| SVN rev/date | 理由 |
| 列名语义 | 置信度 |
| 值域约束 | 风险提示 |

### 优化方法

- 冲突最多前 5 sheet 后台预取。
- 多格冲突合并成一次批量 prompt。
- 输出 JSON，前端展示“建议来源 + 理由 + 置信度”。

### 指标

| 指标 | before | after |
|---|---|---|
| 冲突建议调用 | 每格 N 次网络往返 | 1 次批量调用 |

---

## 第 40 页｜Merge 指标汇总

### 内容

| 指标 | before | after | 效果 |
|---|---:|---:|---:|
| 大表 compare | ~48s | ~6.4s | **7.5×** |
| apply big_data | ~19.6s | ~4.3s | **3.8×** |
| payload | 46.4MB | 0.1MB | **-99.8%** |
| subdir_compare | 21.8s | 9.3s | **2.33×** |
| branch_compare | 99.5s | 89.5s | 1.11× |
| calamine 文件打开 | 17.5s | 0.8s | 21.9× |
| svn info 批量 | ~190s | ~0.2s | ~950× |
| 公式扫描 | ~6s | ~0.05s | ~120× |
| 假冲突率 | 0.5 | 0 | 归零 |
| 假删除 | 69 | 0 | 归零 |

---

## 第 41 页｜Merge 使用演示页

### 使用流程

```text
1. 用户选择 From / To
2. 系统定位 merge-base
3. compare 输出文件 / sheet / 行列 / 语义差异
4. 自动合并安全项
5. 冲突项展示 AI 建议
6. 用户确认 apply
7. 公式重算 + 安全写回
```

### 演示重点

- 三方合并不是覆盖文件。
- 冲突可解释，AI 不越权。
- 大表走 fast path，公式有保护。

---

# 第四章：验证、边界与总结

## 第 42 页｜验证结论：回归与单测

### 内容

| 指标 | 数值 |
|---|---:|
| Agent 回归 | 109 项通过 |
| Merge 核心回归 | 71 项通过 |
| 合计回归 | 180 项全过 |
| 单测 | 323+ 零回归 |
| 复杂样例 | 6/6 OK |

### 讲法

> 优化不只看速度，也看没把已有能力改坏。

---

## 第 43 页｜指标口径与诚实边界

### 内容

| 边界 | 说明 |
|---|---|
| 不同样例集不可横比 | 所有 before/after 只在同集内比较 |
| Agent `<90s` | 是设计目标，不是复杂跨表实测 |
| Serve 阻塞 | `codemaker serve` 外部问题会影响复杂链路耗时 |
| ProcessPool | 样例可 2.0×，demo_svn 当前仍有 worker 隔离边界 |
| RAG | 可讲已接入召回思路，未验证数字不讲百分比 |

### 讲法

> 主动说明边界比硬凑数字更可信。

---

## 第 44 页｜总结：两条引擎一套工程哲学

### 内容

| 维度 | Excel-Agent | Merge |
|---|---|---|
| 核心问题 | 自然语言如何可靠改表 | 多分支如何安全合表 |
| 技术路线 | Prompt + schema + 校验 + 确定执行 | base + diff + 引用保护 + 安全写回 |
| 性能武器 | 少问模型、缓存、零 LLM 执行 | 少读少写、向量化、稀疏化 |
| 安全机制 | 审计、回滚、交互修正 | 临时文件、公式保护、人工确认 |

### 结论句

> Excel-Agent 解决“怎么改”，Merge 解决“怎么合”，共同目标是让复杂 Excel 配表从人工经验变成可验证工程流程。

---

## 第 45 页｜THANK YOU / Q&A

**大字**：THANK YOU  
**副题**：模型负责理解，规则负责约束，代码负责确定性

---

# 附录：页面制作建议

## A. 与现有 PPTX 适配

| 现有页 | 建议处理 |
|---|---|
| slide1-slide5 | 保留框架，替换为本规划第 1-5 页内容 |
| slide6-slide19 | 重排成本规划第 6-20 页，新增 Prompt 页 |
| slide20-slide31 | 重排成本规划第 21-32 页，性能数据分散进 Compare/公式/写回/AI 页 |
| slide32-slide33 | 替换为本规划第 33-35 页 |
| slide71/72 | 可用作第 36 页结束页 |

## B. 图表风格

- 标题：微软雅黑加粗 28-32。
- 正文：微软雅黑 14-18。
- 指标数字：网易红 `#C00000`。
- 技术关键词：黑色加粗；before/after 数字用红色箭头。
- 每页最多 1 个大表，超过 8 行拆页。

## C. 数据来源索引

| 数据 | 来源 |
|---|---|
| Agent R1 指标 | 桌面 `优化全过程.md`、`性能优化指标整合文档.md` |
| Agent 4-Step 6 样例 | `bench/bench_iter_results.md` |
| Merge 总指标 | `PERF_DATA_SUMMARY.md`、`merge/scripts/benchmark_report.md` |
| Merge 路由 A/B | `docs/archive/优化全过程.md` R3 |
| 公式快扫 | `docs/Merge部分详细萃取.md`、`PERF_DATA_SUMMARY.md` |
| 当前 PPT 结构 | `网易互娱通用PPT模板（含保密）_ExcelAgent答辩优化版.pptx` |

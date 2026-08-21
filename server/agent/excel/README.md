# Excel 配置表智能操作工具集

自然语言驱动的游戏数据配置管理工具，支持对 `resources/` 下 60+ 个 Excel 配置表的增删查改操作。

代码位于 `server/agent/excel/`，由 LangGraph 图编排层（`server/agent/`）调用。

---

## 目录结构

```
server/agent/excel/
├── __init__.py            # 包导出（公共 API 透传）
├── _table_index.json      # 自动生成的表索引（60表，含 md5 + 前3行样本）
├── alias_mapping.json     # 语义别名 → 文件映射（如 灵兽→pet.xlsx）
├── table_relations.json   # 表间外键关系图谱
│
├── agent.py               # TableAgent：自然语言 → Excel CRUD（+ run_as_writer 管道Step6执行器）
├── operation_orchestrator.py  # 多操作编排（拓扑排序 + 符号校验 + sub_tasks 装配）
├── task_chain_adapter.py  # task_chain.json 结构 → 内部 NLIntent 适配层
├── llm_context.py         # LLM上下文构建器（Token感知 + 5 Skill注入 + 系统提示词）
├── skill_loader.py        # YAML skill 配置加载器
├── skill_context.py       # Skill 运行期上下文
├── skill_updater.py       # Skill 动态更新（反模式归纳等）
├── step_ai_enhancer.py    # 步骤 AI 增强
├── confidence_config.py   # 置信度档位配置
├── semantic_gate.py       # 语义闸门
├── checkpoint.py          # 管道断点管理器（原子写 + 续跑点判定）
├── table_relations.py     # 关系图谱（外键关联 + 跨表上下文扩展）
├── cross_table_splitter.py # 跨表指令拆分
├── enum_resolver.py       # 枚举值解析
├── analyze_enum_columns.py # 枚举列分析
├── date_normalizer.py     # 日期归一化
├── evidence_logger.py     # 证据日志
├── dialog_logger.py       # 对话日志
├── backup_audit.py        # 操作安全（备份/审计日志/回滚）
├── file_watcher.py        # 文件监听（watchdog 防抖自动刷新）
├── style_utils.py         # 单元格样式工具
│
├── xlsx_tool.py           # CLI 入口（手工精确操作）
├── cli_interface.py       # CodeMakerCLI 抽象接口 & openpyxl stub 实现
├── cli_instrument.py      # CLI 装饰层 → ToolRecord → SSE tool 事件
├── real_cli.py            # 真实 CLI 实现
├── codemaker_parser.py    # CodeMaker 自然语言解析器
│
├── nl_parser.py           # 自然语言意图解析（动作/字段/值提取）
├── schema_infer.py        # 表结构推断（表头/类型/枚举）
├── segmenter.py           # 文本分段
├── multi_intent_splitter.py # 多意图拆分
├── produces_inference.py  # produces 占位符引用推断
│
├── table_locator.py       # 表格定位器（5级递进定位 + 歧义消解）
├── table_resolver.py      # 表格检索器（旧版，关键词+余弦；新代码用 table_locator）
├── table_index.py         # 表格注册中心（扫描/MD5/样本/增量刷新）
├── alias_mapping.py       # 语义别名管理（alias_mapping.json 反查）
├── column_matcher.py      # 列名映射（余弦相似度）
├── column_name_resolver.py # 列名解析器
├── fuzzy_matcher.py       # 模糊匹配引擎（子串+编辑距离+字符重叠）
├── index_builder.py       # 索引构建器 standalone 脚本
│
├── repair_context.py      # 修复上下文
├── repair_playbook.py     # 修复策略手册
├── error_classifier.py    # 错误分类器（8 类 ErrorType）
├── cascade_planner.py     # 级联计划器
├── cascade_resolver.py    # 级联解析器
│
├── formula_cache_validator.py  # 公式缓存校验
├── formula_ref_shifter.py # 公式引用偏移
├── formula_semantics.py   # 公式语义
│
├── pipeline/              # 7 步管道编排层
│   ├── pipeline.py        # 7 步编排器 + 模式判定
│   ├── types.py           # PipelineContext/Result/AgentFragment/DocIntent/StepCard
│   └── verifier.py        # Step5 规则终检器
│
├── subagent/              # SubAgent 派发层（并行 LLM 子 Agent）
│   ├── base.py            # SubAgent 抽象基类 + thinking_sink 注入
│   ├── dispatcher.py      # asyncio.gather 并行派发 + 隔离失败 + 超时
│   ├── decompose_agent.py # 拆解子 Agent
│   ├── llm_agent.py       # LLM 子 Agent
│   ├── locator_agent.py   # 定位子 Agent
│   ├── validator_agent.py # 校验子 Agent
│   └── roles.py           # 角色定义
│
├── parser/                # 通用文件解析器（多模态→DocIntent）
│   └── file_parser.py     # .md/.xlsx/.csv/.txt 解析
│
├── skills/                # Skill 配置文件 + 派生产物
│   ├── column_short_form.yaml       # 列名缩写
│   ├── sheet_aliases.yaml           # sheet 别名
│   ├── parser_config.yaml           # 解析器配置
│   ├── index_builder_hints.yaml     # 索引构建提示
│   ├── table_constraints.md         # 表约束
│   ├── cross_table_chain_principles.md  # 跨表链原则
│   ├── formula_agent.md             # 公式 agent
│   ├── case_quest_npc_optimization.md   # NPC 优化案例
│   ├── L1_derived/      # 自动派生（value_constraints/cascade_rules/column_aliases，.svnignore）
│   ├── L2_runtime/      # 运行期产物（.runtime.json/.yaml，.svnignore）
│   ├── L3_anti_patterns/ # 反模式
│   └── _pending/        # 待定
│
├── evidence/              # 证据 JSONL（.svnignore）
├── dialogs/               # 对话记录（.svnignore）
├── dialog_examples/       # 对话示例（.svnignore）
└── dialog_failures/       # 对话失败案例（.svnignore）
```

> 注：顶层公共 API 通过 `__init__.py` 透传，`from agent.excel.<name> import X` 路径保持兼容。子目录划分：
> - `formula/` — 公式相关
> - `locator/` — 表格定位层
> - `parser/` — 解析层
> - `repair/` — 修复+错误+级联
> - `cli/` — CLI 入口+接口
> - `core/` — agent + orchestrator + skill + 上下文构建等核心编排
> - `pipeline/` `subagent/` `skills/` — 原有子目录

---

## 管道模式（7 步固定工作流）

复杂任务（文件输入/多表/多步）走 7 步管道，对齐配表实战案例：

```
Step0 断点检查  → Step1 拆解(parse_file→DocIntent+符号映射表)
   ↓
Step2 分区(查_table_index.json+table_resolver定位目标表/sheet)
   ↓
Step3 并行填表(asyncio.gather 派发 N 个 SubAgent,产出 AgentFragment)
   ↓
Step4 汇总(复用 OperationOrchestrator:拓扑排序+符号校验+sub_tasks 装配)
   ↓
Step5 验证(复用 skill 体系:value_constraints/cascade/anti_patterns+符号闭环校验)
   ↓
Step6 写库(cli_interface 包 instrument + _verify_write_back + 符号引用回查)
   ↓
Step7 清理中间文件 + 生成汇总报告(做了什么 + 提醒)
```

**触发规则**：输入含文件路径（`.md/.csv/.txt/.xlsx`）或多表关键词（"跨表/多表/流程/并行/配表/剧本"）→ 管道模式；否则走旧 CRUD 路径。可配置 `CODEMAKER_PIPELINE_MODE=auto|off|on`（默认 auto）。

**思考/工具调用可见性**：管道全程 thinking 块 + tool 块（name/desc/cmd/result）推 SSE，前端可折叠展示。`CODEMAKER_CLI_INSTRUMENT=on|off`（管道默认 on）。

**断点续跑**：每步写 `{output_dir}/_checkpoint.json`，中断后从断点恢复。

---

## 分层解耦架构

系统按职责拆分为四大核心层 + 三大运行期机制：

### 层 1：表格注册中心（`table_index.py` + `alias_mapping.py`）

- **启动扫描**：递归扫描 `resources/` 下所有 `.xlsx`，提取每个 Sheet 的列名、行数、前 3 行样本数据，生成元数据索引持久化到 `_table_index.json`。
- **变更检测**：为每个文件计算 MD5 哈希；外部修改后调用 `refresh_if_changed(workspace)` 做增量刷新（仅重扫变更/新增/删除的文件），保持 AI 认知与实际文件同步。
- **别名管理**：`alias_mapping.json` 维护语义别名 → 文件映射（如 `灵兽`/`宠物` → `pet.xlsx`），支持自然语言反查。
- **关键 API**：`build_index`、`load_index`、`refresh_if_changed`、`compute_md5`、`AliasMapping.load/lookup_in_text`。

### 层 2：模糊匹配引擎（`fuzzy_matcher.py`）

查询值无法精确命中时，在候选集合中返回最相似项：

- **子串匹配**：前缀（0.9）> 包含（0.8）> 反向前缀（0.7）> 反向包含（0.6）。
- **编辑距离**：Levenshtein 相似比率 `1 - dist/max(len)`。
- **字符重叠度**：字符集合 Jaccard 交并比。
- **融合评分**：三路加权（子串 0.5 + 编辑 0.35 + 重叠 0.15），子串无命中时权重转移至编辑距离。
- **置信度档位**：score ≥ 0.80 高，≥ 0.55 中，≥ 0.30 低，低于 0.30 丢弃。
- **交互确认**：`FuzzyMatcher.format_candidates` 输出带置信度的候选列表供用户确认后再操作。

### 层 3：表格定位器（`table_locator.py`）

自然语言描述 → 文件路径 + Sheet 名，5 级递进定位：

| 级别 | 策略 | 置信度 | 示例 |
|------|------|--------|------|
| 1 | 精确文件名匹配（stem 或 文件名） | 100% | `pet.xlsx` → pet |
| 2 | 别名匹配（含已注册别名） | 90% | `灵兽` → pet.xlsx |
| 3 | 文件名模糊匹配（stem 子串/超串） | 80% | `pet` → pet.xlsx |
| 4 | Sheet 名匹配（输入含某 sheet 名） | 75% | `Pet sheet` |
| 5 | 列名语义匹配（输入含某列名） | 60% | `skill_id` |

- **歧义消解**：同级别多匹配或次优与最优置信度差 < 0.05 时，返回候选列表（`LocateOutcome.ambiguous`）供上层交互确认，不自动猜测。
- **关键 API**：`TableLocator.locate(text) -> LocateOutcome`、`locate_all(text)`、`locate_best(text)`。

> 旧版 `table_resolver.py`（关键词+余弦）保留向后兼容，新代码应优先使用 `table_locator.py`。

### 层 4：LLM 上下文构建器与 Skill 注入（`llm_context.py`）

每次调用 LLM 前动态构建不超过 Token 限制的结构化上下文，并注入 Skill 定义。

- **Token 感知策略**：
  - 估算：中文 ≈ 2 token/字，ASCII ≈ 0.5 token/字符，其它 ≈ 1 token/字符（`estimate_tokens`）。
  - 上限 12000 token，预留 2000 token 安全余量（有效预算 10000）。
  - 增量加载：聚焦表格注入完整结构（列名+行数+前3行样本），剩余空间补充其他表格简要摘要（文件名+Sheet名）。
  - 降级机制：全量摘要超限时，自动降级为简要摘要模式（聚焦表也降为摘要），仍超限则按序省略并标记 `omitted`。
- **Skill 定义**（`SKILL_DEFINITIONS`，5 个结构化操作能力）：

  | Skill | 作用 | 底层引擎 |
  |-------|------|----------|
  | `locate_table` | 自然语言定位表格 | 层3 `TableLocator` |
  | `fuzzy_search_value` | 模糊搜索值返回候选 | 层2 `FuzzyMatcher` |
  | `get_table_structure` | 获取完整表结构 | 层1 注册中心 |
  | `add_column` | 向指定 Sheet 添加列 | `CodeMakerCLI.insert_column` |
  | `list_all_tables` | 列出所有已注册表格 | 层1 注册中心 |

- **系统提示词**：组装 表格结构摘要 + Skill 定义 + 操作规则（定位优先/模糊匹配/结构感知/交互确认/路径规范）为完整 system prompt。
- **关键 API**：
  - `LLMContextBuilder.build_context(focused=[...]) -> ContextResult`：构建上下文，`focused` 接受 path/stem/`LocateResult` 列表。
  - `SkillExecutor.call(skill, **kwargs)`：分派 Skill 调用到底层引擎（读类直接返回，写类 `add_column` 需配置 `cli` 且用户确认后执行）。
  - `format_skills()` / `OPERATION_RULES`：分别输出 Skill 块与操作规则文本。

> `SkillExecutor` 的写类 Skill（`add_column`）需注入 `CodeMakerCLI` 实例；未配置时返回 `ok=False` 错误，不执行写入。

---

## 运行期机制

### 文件监听与自动刷新（`file_watcher.py`）

基于 watchdog 监听 `resources/` 目录变更，自动刷新注册中心元数据。

- **防抖机制**：默认 1 秒延迟，避免 Excel 保存时连续触发多次回调（`threading.Timer` 重置）。
- **自动刷新**：文件变更触发 `table_index.refresh_if_changed`，刷新后通过 `on_refresh` 回调通知上层上下文已更新。
- **降级运行**：watchdog 未安装时 `start()` 抛 `RuntimeError` 提示安装，仍可调用 `refresh_now()` 手动触发增量刷新。
- **关键 API**：`TableFileWatcher(workspace, on_refresh, debounce=1.0).start()`、`has_watchdog()`、`refresh_now()`。
- 依赖：`watchdog>=4.0.0`。

### 操作安全与回滚（`backup_audit.py`）

每次表格修改前自动备份，并记录审计日志，支持按日志/备份回滚。

- **备份策略**：操作前把原文件复制到 `server/backups/`，文件名 `{stem}_{YYYYMMDD_HHMMSS_ffffff}.xlsx`（微秒时间戳避免冲突）。
- **审计日志**：JSONL 追加写入 `server/backups/audit_log.jsonl`，记录操作类型、目标文件、Sheet、列、操作时间、操作前后值、备份文件路径。
- **回滚**：`rollback_to_backup(backup_file)` 还原指定备份；`rollback_by_index(i)` 按审计日志索引回滚。回滚前会再次备份当前版本并记录审计（operation=`rollback`），保证可追溯。
- **关键 API**：`BackupAuditor.backup_and_record(op, path, **kwargs)`、`log_entries()`、`rollback_to_backup()`、`rollback_by_index()`。

### 多表关联查询支持（`table_relations.py`）

维护 `table_relations.json` 关系图谱，记录表间外键关联，跨表查询时把关联表结构一并注入上下文。

- **关系结构**：`{from_path, from_sheet, from_column, to_path, to_sheet, to_column, relation_type, description}`，例：`pet_evolve.xlsx.PetEvolveData.宠物id → pet.xlsx.Pet.灵兽id`。
- **查询**：`relations_from(path)` 出向、`relations_to(path)` 入向、`related_paths(path)` 直接关联表、`relations_between(a,b)` 两表间关系。
- **上下文扩展**：`RelationGraph.expand(paths)` 返回原表 + 直接关联表；`LLMContextBuilder` 接受 `relation_graph` 参数，构建上下文时自动把聚焦表的关联表纳入完整结构注入（结果 `ContextResult.relation_expanded` 记录扩展进来的表）。
- **关键 API**：`RelationGraph.load()`、`expand(paths)`、`LLMContextBuilder(relation_graph=g).build_context(focused=[...])`。

---

## 架构概览

```
用户输入（自然语言 / CLI 命令）
        │
        ▼
┌─────────────────┐    ┌─────────────────┐
│   xlsx_tool.py  │    │    agent.py     │
│  (CLI 精确模式)  │  (NL 智能模式)  │
└───────┬─────────┘    └───────┬─────────┘
        │                      │
        │    ┌─────────────────┤
        │    │  nl_parser.py   │  ← 意图解析：动作/字段/值
        │    │  table_resolver │  ← 表+sheet 定位
        │    │  column_matcher │  ← 列名映射
        │    └─────────────────┘
        │                      │
        ▼                      ▼
┌─────────────────────────────────────┐
│         cli_interface.py            │
│   CodeMakerCLI (抽象) / Stub (实现) │
│         openpyxl 读写 Excel          │
└─────────────────────────────────────┘
        │
        ▼
   resources/*.xlsx  (60+ 配置表)
```

两条路径可独立使用：
- **CLI 精确模式**：`python -m agent.excel.xlsx_tool add pet Pet --灵兽名 朱雀`
- **NL 智能模式**：通过 TableAgent（`agent.excel.agent.TableAgent`）

---

## 使用方式

### CLI 精确模式

```bash
# 搜索
python -m agent.excel.xlsx_tool search 朱雀

# 添加
python -m agent.excel.xlsx_tool add pet Pet --灵兽id 1100 --灵兽名 朱雀 --灵兽品质 3 --成长率 1.5

# 删除（含级联确认）
python -m agent.excel.xlsx_tool delete pet Pet --name 朱雀
python -m agent.excel.xlsx_tool delete pet Pet --name 朱雀 --yes        # 跳过确认
python -m agent.excel.xlsx_tool delete pet Pet --name 朱雀 --no-cascade # 跳过级联

# 修改
python -m agent.excel.xlsx_tool set --row 5 pet Pet --成长率 1.6

# 查询
python -m agent.excel.xlsx_tool get pet Pet --name 朱雀
```

### 索引维护

```bash
# 构建索引
python -m agent.excel.index_builder

# 验证一致性
python -m agent.excel.index_builder --verify

# 预览
python -m agent.excel.index_builder --print
```

---

## 特性亮点

### 级联操作
- **级联删除**：删除一行时自动扫描关联表（同目录+同前缀），删除共享键匹配的行，操作前展示并询问确认
- **级联添加**：新增行时自动在关联表中创建共享键行
- **级联更新**：修改共享键值时同步更新关联表

### 别名系统
- 三层优先级：表精确 → 表通配 → 全局通配
- 解析失败回退到原始别名
- 自然语言列名和表头列名双向映射

### 公式处理
- `_read_cell_value` 自动检测公式单元格（`=` 开头）
- 回退到 `data_only=True` 模式读取计算值
- `_detect_formula` 可检测并返回公式文本

### 边界条件

| 场景 | 处理 |
|------|------|
| 公式单元格 | data_only 模式回退求值 |
| 缺失必填字段 | 告警但不阻断 |
| 多匹配删除 | 展示列表，删除首条 |
| Sheet 不存在 | 自动扫描所有 sheet |
| 临时文件(~$) | 自动跳过 |
| 空行/空表 | 正确识别并跳过 |
| 括号注释表头 | 自动清除 `（注释）` |
| 类型后缀 | 自动剥离 `:int` `:string` |
| 含换行表头 | 取首行匹配 |

---

## 依赖

- Python 3.10+
- `openpyxl` — Excel 读写
- `pyyaml` — YAML 配置解析
- `watchdog` — 文件监听
- `jieba` — 中文分词
- `rapidfuzz` — 模糊匹配加速
- `numpy` — 向量计算
- `langchain` / `langgraph` — LLM 编排

## 项目规模

- 配置表：60+ 个 Excel 文件
- Sheet 总数：150+
- Python 代码：`server/agent/excel/` 下 50+ 模块

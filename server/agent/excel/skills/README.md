# Skills 目录说明

本目录是 Excel-Agent 的**内部配置与知识体系**（自动派生 + 运行时学习 + 反模式）。

> 用户手写的业务规则已迁移到项目根 `rules/` 目录，与本目录隔离。
> 填表规则 → `rules/fill/`，校验规则 → `rules/validate/`。

## 目录结构

```
skills/
├── README.md                 # 本说明
├── parser_config.yaml        # 自然语言解析配置（lead_verbs/non_business_markers 等）
├── sheet_aliases.yaml        # sheet 别名（自然语言 → 真实 sheet 名）
├── column_short_form.yaml    # 列名短形式（committed 别名，skill_updater 写入）
├── index_builder_hints.yaml  # 索引构建提示（stem→域来源，locator 权威别名）
├── docs/                     # 文档（复盘/原则/公式，不参与运行时加载）
│   ├── cross_table_chain_principles.md
│   ├── formula_agent.md
│   └── case_quest_npc_optimization.md
├── scripts/                  # 工具脚本（独立运行）
│   └── derive_required_fields.py   # 从 _table_index.json 派生必填列
├── L1_derived/               # L1 自动派生（schema_infer.regenerate_skills 生成）
│   ├── column_aliases.yaml   # 列别名映射
│   ├── row_aliases.yaml      # 行定位规则
│   ├── table_context.yaml    # 表/sheet 上下文关键词
│   ├── value_constraints.yaml# 值约束（type/required/unique/min/max）
│   ├── merge_strategies.yaml # 合并策略
│   ├── cascade_rules.yaml    # 级联规则
│   ├── enum_mappings.yaml    # 枚举映射
│   └── required_fields.yaml  # 必填字段（derive_required_fields.py 派生）
├── L2_runtime/               # L2 运行时学习（skill_updater promote 写入）
│   ├── column_aliases.runtime.yaml
│   └── table_relations.runtime.json
├── L3_anti_patterns/         # L3 反模式库（失败模式，定位/写盘前查）
│   └── anti_patterns.yaml
└── _pending/                 # 候选池（待门禁合并，不直接生效）
```

## 三层体系

| 层 | 目录 | 来源 | 作用 |
|---|---|---|---|
| L1 自动派生 | `L1_derived/` | 扫描 resources/*.xlsx 表结构生成 | 类型/别名/枚举/级联等基础配置 |
| L2 运行时 | `L2_runtime/` | 运行中从 evidence promote | 学习到的别名/关系 |
| L3 反模式 | `L3_anti_patterns/` | 失败案例归纳 | 定位/写盘前拦截已知错误 |

## 与 rules/ 的关系

- `rules/` = **用户手写强约束**，优先级最高。
- `skills/L1_derived/` = 自动派生基础约束，rules 冲突时被 rules 覆盖。
- 加载器：
  - 填表规则 → `codemaker_parser._build_prompt_with_skills` / `decompose_agent._build_prompt`
  - 校验规则 → `_load_value_constraints` / `_load_required_fields` / `build_data_getter`（enum_set）
  - 详细说明见 `rules/README.md`。

## 维护约定

- `L1_derived/`、`L2_runtime/` 由代码自动生成/更新，**不要手改**（除非明确知道后果）。
- 手写知识一律放 `rules/`，不放本目录。
- `_pending/` 候选经门禁后才合并进 L1/L2，直接改不生效。

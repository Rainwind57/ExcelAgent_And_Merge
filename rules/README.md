# 规则目录规范

本目录承载用户手打的**业务规则**，与 `server/agent/excel/skills/` 隔离。
规则是强约束，AI 填表和 Step2 校验都必须遵守。

## 目录结构

```
rules/
├── README.md           # 本规范
├── fill/               # 填表规则（Step1：AI 理解输入 → 转 JSON 时注入的知识）
│   ├── _global.md      # 全局填表硬约束（每次注入 prompt，保持精简）
│   ├── _reference.md   # 详细参考（不自动注入，供人类查阅）
│   └── <表名>.md       # 每表填表知识（可选，按表名 stem 命名）
└── validate/           # 校验规则（Step2：JSON 形成后的强约束）
    ├── _global.md      # 全局校验约束（所有表生效）
    └── <表名>.md       # 每表校验约束（可选）
```

## 一、填表规则（fill/*.md）

- **纯 Markdown 自然语言**，写给 AI 看，注入 Step1 解析 prompt。
- 文件名 = 表名 stem（如 `item.md` 对应 item 表），`_global.md` 全局生效。
- 内容建议：列含义、枚举值归一化、ID 分段、必填列、默认值、填表示例。

示例（`fill/item.md`）：

```markdown
# item 表填表知识

- 新增道具时 `item_id` 必须取当前段最大 ID +1，禁止硬编码。
- `quality` 品质列只能填 1-5：1凡品/2良品/3上品/4珍品/5绝品。
- `item_type` 决定子表：1资源/2礼包→Chest/3药品→Potion/4宝石→Gem/6装备→Equipment。
- 列表列（如 `spell_ids`）用逗号分隔串，禁止写成 JSON 数组。
```

## 二、校验规则（validate/*.md）

- Markdown 文档，**约束写在 yaml 代码块里**（机器解析执行，强约束）。
- yaml 根节点固定为 `tables`，结构：

```markdown
# item 表校验约束

```yaml
tables:
  item:                       # 表名 stem
    ItemBase:                 # sheet 名
      primary_key: [item_id]  # sheet 级主键（单列或复合，见下）
      columns:
        quality:
          type: int           # 类型：int/float/str/bool/list[...]
          required: true      # 必填
          enum: [1, 2, 3, 4, 5]   # 枚举白名单（只允许这些值）
        item_id:
          type: int
          unique: true        # 唯一（主键）
          min: 10000          # 最小值
          max: 29999          # 最大值
```
```

### 支持字段

| 字段 | 含义 | 校验位置 |
|---|---|---|
| `type` | 列类型 | Step2 类型 coerce |
| `required` | 必填（`true`）/ 显式声明可留空以摘除误判（`false`） | Step2 必填检查 |
| `enum` | 枚举白名单 | Step2 枚举检查 |
| `min` / `max` | 数值范围 | Step2 范围检查 |
| `unique` | 单列唯一约束 | Step2 唯一性检查 |
| `primary_key` | sheet 级主键（单列或复合）| Step2/Step3 唯一性 + 冲突检测 |
| `regex` | 正则匹配 | 预留 |

### `primary_key`（复合主键）

在 **sheet 级**（与 `columns` 同层）声明，取值为列名数组：

```yaml
tables:
  fabao:
    FabaoLevel:
      primary_key: [法宝id, 法宝等级]   # 组合唯一
      columns: { ... }
```

- 单元素 `primary_key: [id]` 等价于列级 `unique: true`，两条路收敛到同一检测。
- 复合键（≥2 列）按"组合值"判唯一/冲突，避免误把同一实体的多个等级行
  （如 `fabao.FabaoLevel` 的 `(法宝id, 法宝等级)`）判成主键冲突。
- 优先级：`primary_key` 声明 > `table_relations` FK 列推断 > 表头首列兜底。
- 通配：表名/sheet 名可用 `*`，`_global.md` 里 `id: {unique: true}` 仍是单列兜底。

### 纠正误判：`primary_key: []` 与 `required: false`

未声明 `primary_key` 的 sheet，Step2 会用表头启发式（列名像不像 id/编号）
**猜**一个主键出来；启发式会猜错——某些列名带 id/编号字样，业务上其实可以
留空（如"关联ID""备注编号"），被误猜成主键后，缺失会硬阻断写盘。发现误判
时，两种纠正方式：

1. **该 sheet 根本没有主键**：显式声明空列表，让 Step2 别再猜——

   ```yaml
   tables:
     some_table:
       SomeSheet:
         primary_key: []   # 明确声明：本 sheet 无主键，不做主键缺失硬阻断
   ```

2. **某一列被误判必填/主键，其他列没问题**：在该列上显式写 `required: false`
   摘除，不用整体改主键声明——

   ```yaml
   tables:
     some_table:
       SomeSheet:
         columns:
           备注编号:
             required: false   # 显式声明可留空，覆盖启发式/自动派生的必填判断
   ```

两者都是"显式声明 > 自动派生/启发式"，优先级最高、立即生效，不需要碰
`skills/L1_derived/*.yaml` 或改代码。

即便没有写这两条覆盖规则，Step2 现在对**未声明**的启发式主键猜测也会先看
现有数据实证——如果这一列在已有行里本身就出现过空值，会自动判定"不是真主键"
而不阻断（不需要用户手动纠正）；实证不足（如全新表）时，可选开启环境变量
`CODEMAKER_VALIDATOR_LLM_PK_JUDGE=1` 让 LLM 结合表结构和现有样例数据做二次
判断，仍拿不准则维持原来的硬阻断（宁可多问一次，不漏拦真正的主键缺失）。

### 通配

- 表名、sheet 名可用 `*` 通配：`*` 表 = 所有表，`*` sheet = 所有 sheet。
- 多个 `.md` 文件 / 多个 yaml 块按「后加载覆盖先加载」深合并。

## 三、与 skills 的关系

- `rules/` 是**用户显式手打**的业务规则，优先级最高。
- `skills/` 是 agent 内部配置（L1 自动派生 / L2 运行时 / L3 反模式），
  规则冲突时以 `rules/` 为准。
- 填表规则拼进 Step1 prompt（`codemaker_parser._build_prompt_with_skills`）；
  校验规则合并进 Step2 的 value_constraints / required_fields / enum_set。

## 四、热更新

规则文件改动后重启进程生效。缓存失效由进程重启完成。

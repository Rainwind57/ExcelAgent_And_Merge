# interaction 表填表知识

## 表结构（resources/interaction.xlsx，4 个 sheet）

### sheet: Interaction（交互主表）
- `编号`（id:int）主键，取段内 max+1（10001+）。
- `交互效果编号`（effect.key:int）枚举：
  - 3001 = 战斗 → 配 `3001: 战斗ID` 列填 combat_id
  - 3002 = 奖励 → 配 `3002: reward ID` 列填 reward_id
  - 3003 = 传送(space) → 配 `3003: 目标space ID`
  - 3004 = 传送(space+spawn) → 配 `3004: 目标space ID` + `3004: spawn ID`
  - 3005 = 传送(坐标) → 配 `3005: 目标space ID` + `3005: 目标位置` + `3005: 目标朝向`
  - 3006 = 对话 → 配 `3006: 对话ID` 列填 conv_id
  - 4003 = 洞府 → 配 `4003: 洞府ID`
  - 4004 = 分身 → 配 `4004: 分身Prefab ID`
- `3001: 战斗ID`（effect.data.3001.combat_id:int）effect.key=3001 时填 combat_id，FK→combat。
- `3002: reward ID`（effect.data.3002.reward_id:int）effect.key=3002 时填 reward_id，FK→reward。
- `3006: 对话ID`（effect.data.3006.conv_id:int）effect.key=3006 时填 conv_id，FK→InteractionConv。
- 其余 effect.data.* 列按 effect.key 选择性填。

### sheet: InteractionConv（对话内容表）
- `编号`（id:int）主键，取段内 max+1（1+）。
- `对话内容`（prompt_text:string）NPC 说的话，中文直接存。
- `选项1`~`选项6`（options[0..5]:int）选项 ID，FK→InteractionConvOption.编号。
- 一个对话节点可挂 1-6 个选项，未挂的留空。

### sheet: InteractionConvOption（选项表）
- `编号`（id:int）主键，取段内 max+1（1+）。
- `选项内容`（option_text:string）选项显示文本（如「我这就去讨伐」）。
- `选项功能`（option_function.function_type:int）选项行为类型：
  - 1 = 跳转新对话 → 配 `1:新对话ID` 列填目标 conv_id
  - 其他类型见 Row2 规范名注释
- `1:新对话ID`（option_function.data.1.conv_id:int）function_type=1 时填跳转目标 conv_id，FK→InteractionConv。
- 选项「什么都不做结束对话」→ option_function 留空或填 0（无后续行为）。

## 填值规则（对话树链型，重点）

### 同表多行互相引用 → produces 必须用唯一标签

对话树典型结构：NPC 点击 → 根对话 → 选项 1（跳第二段对话）→ 结束 / 选项 2（跳第三段对话）→ 选项「知道了」→ 结束。

每行 produces 用**唯一**标签，不要都叫 `new_interaction_id`：
- 根对话 conv_root_id
- 第二段对话 conv_intro_id
- 第三段对话 conv_reward_id
- 选项 1 opt_go_id
- 选项 2 opt_ask_id
- 选项「知道了」opt_done_id

### consumes 精确写目标标签

- 根对话的 options[0] 引用 opt_go_id → `选项1: <opt_go_id>`
- 根对话的 options[1] 引用 opt_ask_id → `选项2: <opt_ask_id>`
- opt_go_id 的 `1:新对话ID` 引用 conv_intro_id → `<conv_intro_id>`
- opt_ask_id 的 `1:新对话ID` 引用 conv_reward_id → `<conv_reward_id>`
- conv_reward_id 的 options[0] 引用 opt_done_id → `选项1: <opt_done_id>`
- opt_done_id「知道了」选完什么都不做 → option_function 留空

### effect.key 选择

- 引导 NPC 点击弹出对话 → Interaction 行 effect.key=3006，`3006: 对话ID` 填根对话 conv_id。
- 选项「跳到新对话」→ InteractionConvOption 行 option_function.function_type=1，`1:新对话ID` 填目标 conv_id。

### 允许前向引用

先声明的对话行可以引用后声明的对话/选项（系统按依赖自动排序），produces 标签唯一即可。

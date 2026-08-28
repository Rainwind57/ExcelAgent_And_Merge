# entity_prefab 表填表知识

## 表结构（resources/entity_prefab.xlsx，sheet: Base）

- `prefab_id`（prefab_id:int）主键，按序递增、不要分段（表头约束）。
- `model_id`（model_id:int）模型 ID，FK→model_prefab。用户说「model_id 1020」→ 1020。
- `entity_name`（entity_name:string）实体名字，中文直接存（如「赤龙指引人」「焚天赤龙」）。
- `entity_class`（entity_class:string）实体类型枚举：
  - `WorldNonPlayer` = 普通 NPC
  - `WorldInteractiveObject` = 可交互物件
  - `WorldMonster` = 怪物 / 世界 BOSS
  - 用户说「引导 NPC」→ `WorldNonPlayer`；「世界 BOSS」→ `WorldMonster`。
- `interaction_id`（interaction_id:int）交互 ID，FK→interaction.Interaction.编号。引用本批新产出的交互用 `<new_interaction_id>` 占位符 + consumes。
- 其余列（scale/collider 等）可选，未提及留空。

## 填值规则

- prefab_id 必须按序递增，取当前 max+1，禁止分段、禁止硬编码。
- entity_class 是 string 枚举，填英文类名（如 `WorldNonPlayer`），不要填中文。
- interaction_id 引用本批新产出的 Interaction 行时用 consumes 占位符。
- 引导 NPC（可点击对话）需建 entity_prefab + interaction + conv + option 完整链。

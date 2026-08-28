# 表格操作约束与校验 Skill（P0-2）

> 让 AI Agent 执行表格增删改时遵守真实表结构约束：列名映射、类型规范、跨表外键、ID 分段、必填默认。
> 数据来源：`resources/` 下各 `.xlsx` 的 Row1(中文表头) / Row2(英文字段名:类型) / Row3(约束) / Row4(默认值)。

## 0. 表格行约定（全局）

所有业务表遵循统一行布局：

| 行 | 含义 | 示例 |
|---|---|---|
| Row1 | 中文表头（人类阅读，含 `\n` 尾注/单位说明） | `编号\n（按序递增，不要分段）` |
| Row2 | 英文字段名:类型（程序导出键 + 类型标注） | `prefab_id:int` / `effect.data.3006.conv_id:int` |
| Row3 | 约束行（`required:1` / `export_type` / `alias` / `ref_col`） | `required:1` |
| Row4 | 默认值行（新增时未指定则取此值） | `trigger_radius=5` |
| Row5+ | 业务数据行 | `10001 \| 擂台npc \| ...` |

**关键陷阱**：
- Row1 表头常含 `\n` 换行尾注（如 `编号\n（按序递增，不要分段）`），匹配列名时必须取 `\n` 之前的主名。
- Row2 字段名有点分嵌套路径（如 `effect.data.3006.conv_id` / `option_function.function_type` / `attribute_base_slopes.HPMaxCon[1]`），不是真实列名，是结构化字段键，需点分翻译。
- `(不导出)` / `[不导出]` / `（不导表）` 标记的列不参与程序导出，但 Agent 仍需写入（人类阅读用）。

## 1. 类型系统规范

### 1.1 基础类型

| 类型 | 格式 | 示例 | 校验规则 |
|---|---|---|---|
| `int` | 整数 | `1015` | 必须为整数，不接受小数/字符串 |
| `float` | 浮点 | `6.0` / `0.3` | 数值，接受 int 隐式转 float |
| `str` / `string` | 字符串 | `'铁匠老张'` | 任意文本，中文直接存储 |
| `bool` | 布尔 | `True` / `False` | Python 布尔或 `0/1` |

### 1.2 容器类型（注意：Excel 内存为逗号分隔串，非 JSON）

| 类型 | Excel 存储格式 | Row2 标注 | 示例 |
|---|---|---|---|
| `list[int]` | 逗号分隔串 | `spell_ids:list[int]` | `'1001,1002,1003'` |
| `list[float]` | 逗号分隔串 | `enter_pos:list[float]` | `'70, 0, 78'` |
| `list[string]` | 逗号分隔串 | `target_filter.relation.include: list[string]` | `'enemy'` |
| `tuple[float,float,float]` | 括号元组 | `effect.data.3005.to_pos:tuple[float,float,float]` | `(100, 0, 200)` |
| `list[tuple[float,float,float]]` | 元组逗号分隔 | `pos_list: list[tuple[float,float,float]]` | `'(64, 0, 64), (64, 0, 60)'` |
| `list` (混合) | 逗号分隔 | `equip_base_attrs[0]: list` | `'WorldAttackCon, 50,70'` |

**校验原则**：
- list/tuple 在 Excel 中是**逗号分隔字符串**，不是 JSON 数组。写入时用 `,` 分隔，不要用 `[]`。
- 元组用 `()` 包裹，列表不用。
- 空列表留空（不写 `[]`），导出时程序处理为空 list。

### 1.3 嵌套点分路径类型

Row2 字段名含 `.` 表示嵌套结构字段键，不是真实 Excel 列：

| 点分路径模式 | 真实列（Row1） | 写入位置 |
|---|---|---|
| `effect.key` | `交互效果编号` | 选效果类型（3001/3002/.../3006/4003/4004） |
| `effect.data.3006.conv_id` | `3006: 对话ID` | effect.key=3006 时填对话 ID |
| `effect.data.3001.combat_id` | `3001: 战斗ID` | effect.key=3001 时填战斗 ID |
| `option_function.function_type` | `选项功能` | 选项行为类型 |
| `option_function.data.1.conv_id` | `1:新对话ID` | function_type=1 时填跳转对话 ID |
| `attribute_base_slopes.HPMaxCon[1]` | `气血斜率` | 成长公式斜率 |
| `attribute_base_slopes.HPMaxCon[0]` | `气血基础` | 成长公式基础值 |
| `equip_base_attrs[0]` | `基础属性1` | 装备基础属性组 |
| `performance[0].effect_name` | `表演配置0-特效` | 技能表演特效 |

**翻译规则**（Agent 写入前必须执行）：
1. `effect.key` → 找含 `效果编号` 的 Row1 列
2. `effect.data.N.*` → 提取 N，找 `_clean_header == N` 的列（如 `3006: 对话ID` → `3006`）
3. `xxx[N].yyy` → 找 Row1 含 `基础属性N` / `表演配置N` 的列
4. `attribute_base_slopes.属性名[0/1]` → 找 Row1 含 `属性基础/属性斜率` 的列

## 2. 跨表外键关系

新增/修改任何主表行时，必须同步维护关联子表。删除时必须级联清理。

### 2.1 NPC 完整链（entity_prefab → interaction → conv → option → spawn）

```
entity_prefab.Base
  ├─ prefab_id (主键)
  ├─ model_id → model_prefab 表
  ├─ entity_name
  └─ interaction_id ──→ interaction.Interaction.编号
                          ├─ effect.key=3006 + effect.data.3006.conv_id ──→ InteractionConv.编号
                          │                                                   ├─ prompt_text (对话内容)
                          │                                                   └─ options[0-5] ──→ InteractionConvOption.编号
                          │                                                                        ├─ option_text (选项内容)
                          │                                                                        └─ option_function.data.1.conv_id ──→ InteractionConv.编号 (跳转)
                          ├─ effect.key=3001 + effect.data.3001.combat_id ──→ combat.combat_id
                          ├─ effect.key=3002 + effect.data.3002.reward_id ──→ reward.reward_id
                          ├─ effect.key=3003 + effect.data.3003.to_space_id ──→ space.space_id
                          └─ effect.key=4004 + effect.data.4004.prefab_id ──→ entity_prefab.prefab_id (分身)

spawn_world_entity.SpawnWorldEntity
  ├─ spawn_id (主键)
  ├─ space_id ──→ space.space_id
  ├─ entity_prefab_id ──→ entity_prefab.prefab_id
  └─ pos_list: list[tuple[float,float,float]]  (场景坐标)
```

### 2.2 道具链（item.ItemBase ← 子表继承主键）

```
item.ItemBase (主表，item_id 主键)
  ├─ item_type 决定子表：
  │   1资源 / 2礼包→Chest / 3药品→Potion / 4ao
  └─ item_id ←── 子表共享主键

item.Equipment (item_id FK→ItemBase)
  ├─ equip_slot (1武器/2帽子/3衣服/4腰带/5鞋子/6首饰)
  ├─ equip_sch_list: list[int]  (可用门派)
  ├─ rand_ga_pool → GameplayAbilityChoicePool.pool_id
  └─ salvage_item_id → ItemBase.item_id (分解产物)

item.Chest.chest_reward_id → reward.reward_id
item.Gem.upgrade_gem_id → ItemBase.item_id (自关联升级)
item.Potion.feature_builder.args.spell_id → spell.id
item.Fabao.fabao_spell_id → spell.id
item.GameplayAbilityChoicePool.spell_id → spell.id
```

### 2.3 战斗链（combat → pve_npc → spell）

```
combat.combat_data
  ├─ combat_id (主键)
  ├─ space_id → space.space_id
  ├─ win_reward/lose_reward/draw_reward → reward.reward_id
  └─ npc_ids[0-11] → pve_combat_npc.npc_id (最多12个位置)

pve_combat_npc.PveCombatNpc
  ├─ npc_id (主键)
  ├─ model_id → model_prefab
  ├─ spell_groups: list[int] → spell_group.group_id
  ├─ spell_ids: list[int] → spell.id
  └─ combat_ai_name (AI行为树: hello_world / aggressive_ai / ...)

spell_group.SpellGroup.spell_ids: list[int] → spell.id
spell.spell_data.performance[].effect_name → effect_actor.effect_actor_key
```

## 3. ID 分段约定

新增时必须选正确 ID 段，不可越界：

| 实体 | ID 字段 | 段范围 | 用途 |
|---|---|---|---|
| 道具 | `item_id` | 10000-10999 | 资源 |
| | | 11000-11999 | 礼包(Chest) |
| | | 12000-12999 | 药品(Potion) |
| | | 13000-13999 | 宝石(Gem) |
| | | 14000-14999 | 任务道具 |
| | | 15000-19999 | 装备(Equipment) |
| | | 20000-20999 | 灵兽蛋 |
| | | 28000-28499 | 加速道具 |
| | | 28500-29999 | 法宝(Fabao) |
| 交互 | `interaction_id` | 10001+ | Interaction 主键 |
| 对话 | `conv_id` | 1+ | InteractionConv 主键 |
| 选项 | `option_id` | 1+ | InteractionConvOption 主键 |
| 奖励 | `reward_id` | 10001+ | Reward 主键 |
| 实体 | `prefab_id` | 5000+/8000+/9000+ | entity_prefab 主键 |
| 刷新 | `spawn_id` | 50001+ | spawn_world_entity 主键 |
| 场景 | `space_id` | 10001+ | space 主键 |
| 战斗 | `combat_id` | 1+/101+/25001xxx+ | combat 主键 |
| NPC | `npc_id` | 3000+ | pve_combat_npc 主键 |
| 技能 | `id` | 101+ (common_spell) | spell 主键 |
| 技能组 | `group_id` | 1+ | spell_group 主键 |
| 词条池 | `entry_id` | 100001+ | GameplayAbilityChoicePool 主键 |
| 特效 | `effect_actor_key` | 字符串 (如 `FireBurst`) | effect_actor 主键（非数字） |

**新增 ID 生成原则**：
- 优先取该段最大现有值 +1。
- 跨表级联新增时，主表 ID 先生成，子表用主表 ID 或独立段。
- `prefab_id` 必须按序递增、不要分段（表头明示约束）。

## 4. 列约束标记（Row3）

### 4.1 `required:1` — 必填

新增时必须提供值，不可留空：

| 表 | 必填列 |
|---|---|
| spawn_world_entity | `space_id`, `entity_prefab_id`, `spawn_timing_type`, `entity_num_max`, `req_unique_pos` |
| ItemBase | `quality`, `item_type` |
| Equipment | `equip_slot`, `equip_type` |
| GameplayAbilityChoicePool | `pool_id`, `rand_weight`, `ability_type` |

### 4.2 `export_type: ONLY_SERVER` — 仅服务端导出

`entity_prefab.Base.交互id` 标记 `export_type: ONLY_SERVER`，表示该列只服务端使用，客户端导表不包含。

### 4.3 `alias:X` — 列别名

`Potion` 表 Row3：
- `药品数值范围(小)` → `alias:c`
- `药品数值范围(高)` → `alias:d`

`道具描述` 用 `${a}` / `${c}` / `${d}` 引用这些别名（如 `回复${a}血量` → 实际读 `usage_effect.args.hp`）。

### 4.4 `ref_col:True` — 引用其他列

`Potion.道具描述` 标记 `ref_col:True`，值含 `${变量}` 占位符，引用同表其他列运行时替换。

### 4.5 不导出标记

| 标记 | 含义 | 出现表 |
|---|---|---|
| `填表说明(不导出)` | 人类备注，程序跳过 | interaction / entity_prefab / space 等通用 |
| `区域说明[不导出]` | 区域备注 | spawn_world_entity |
| `名字（不导表）` | 名称冗余列（主名在 ItemBase.name） | Equipment / Gem / Fabao |
| `描述(不导表)` | 描述冗余 | GameplayAbilityChoicePool |
| `部位（不导表）` | 部位冗余 | GameplayAbilityChoicePool |

## 5. 默认值（Row4）

新增时未指定字段自动取默认值：

| 表.列 | 默认值 |
|---|---|
| Interaction.`trigger_height` | 30 |
| Interaction.`trigger_radius` | 5 |
| InteractionConvOption.`option_condition` | 0 |
| common_spell.`icon` | `'spell_default'` |
| spawn_world_entity.`autofill_interval` | 0 |
| effect_actor.`is_per_actor` | False |
| effect_actor.`fly_time` | 0.5 |
| effect_actor.`offset_vector3` | `'0, 20, 0'` |
| space.`enter_pos` | `'0, 0, 0'` |
| space.`camera.pitch` | 45 |
| space.`camera.arm_length` | 27 |
| space.`camera.fov` | 30 |
| space.`is_load_navmesh` | 1 |
| space.`enter_yaw` | 0 |
| space.`client_gamemode_instance_id` | 1 |

## 6. 枚举字段取值

### 6.1 交互效果编号（Interaction.`effect.key`）

| 值 | 含义 | 配套 effect.data 字段 |
|---|---|---|
| 3001 | 战斗 | `effect.data.3001.combat_id` → combat_id |
| 3002 | 奖励 | `effect.data.3002.reward_id` → reward_id |
| 3003 | 传送(目标space) | `effect.data.3003.to_space_id` → space_id |
| 3004 | 传送(space+spawn) | `effect.data.3004.to_space_id` + `to_spawn_id` |
| 3005 | 传送(坐标) | `effect.data.3005.to_space_id` + `to_pos` + `to_dir` |
| 3006 | 对话 | `effect.data.3006.conv_id` → conv_id |
| 4003 | 洞府 | `effect.data.4003.residence_id` |
| 4004 | 分身 | `effect.data.4004.prefab_id` → prefab_id |

### 6.2 道具类型（ItemBase.`item_type`）

| 值 | 类型 | 对应子表 |
|---|---|---|
| 1 | 资源 | 无 |
| 2 | 礼包 | Chest |
| 3 | 药品 | Potion |
| 4 | 宝石 | Gem |
| 5 | 任务 | 无 |
| 6 | 装备 | Equipment |
| 7 | 灵兽蛋 | 无 |
| 15 | 法宝 | Fabao |

### 6.3 品质（ItemBase.`quality`）

| 值 | 名称 |
|---|---|
| 1 | 凡品 |
| 2 | 良品 |
| 3 | 上品 |
| 4 | 珍品 |
| 5 | 绝品 |

### 6.4 装备部位（Equipment.`equip_slot`）

| 值 | 部位 |
|---|---|
| 1 | 武器 |
| 2 | 帽子 |
| 3 | 衣服 |
| 4 | 腰带 |
| 5 | 鞋子 |
| 6 | 首饰 |

### 6.5 实体类型（entity_prefab.`entity_class`）

| 值 | 含义 |
|---|---|
| `WorldNonPlayer` | 普通NPC |
| `WorldInteractiveObject` | 可交互物件 |
| `WorldSiegeCart` | 攻城车 |
| `ResidenceEntity` / `ResidenceEntryEntity` / `ResidenceWorldEntryEntity` | 洞府实体 |
| `CandidateResidenceEntryEntity` | 洞府入口候选 |
| `EffectActor` | 特效Actor |
| `WorldMonster` / 游荡怪物 | 怪物 |

### 6.6 技能相关枚举（spell_data）

| 字段 | 取值示例 |
|---|---|
| `spell_type` | `attack` / `buff` / `passive` ... |
| `spell_effect_type` | `attack_damage` / `heal` / `add_buff` ... |
| `spell_element` | `fire` / `water` / `wind` / `earth` / `light` / `dark` ... |
| `target_filter.relation.include` | `enemy` / `friend` / `all` |
| `target_filter.alive_status.include` | `alive` / `dead` |

## 7. Agent 操作校验清单

执行任何表格增删改前，Agent 必须逐项校验：

### 7.1 新增（add）校验

1. **ID 段**：新 ID 是否在正确段范围？是否按序递增？
2. **必填列**：所有 `required:1` 列是否提供值？
3. **类型匹配**：值是否符合 Row2 类型标注？
   - int 列不接受字符串（枚举值需先转 int）
   - list 列用逗号分隔串，非 JSON
   - bool 列用 True/False 或 0/1
4. **枚举值**：枚举列取值是否在允许集合内？（如 effect.key 必须 3001-3006/4003/4004）
5. **跨表级联**：
   - 新增 NPC → 必须同步建 interaction +（若对话）conv + option + spawn 行
   - 新增装备 → 必须同步建 ItemBase + Equipment 两行
   - 新增 NPC interaction_id → interaction.Interaction 必须存在该 ID
6. **外键存在性**：引用的 FK 目标行是否已存在？（如 space_id / model_id / reward_id）
7. **默认值**：未指定列是否可用 Row4 默认值？

### 7.2 修改（modify）校验

1. **行定位**：row_key 目标行是否真实存在？（pristine 查不到 = 测试夹具问题，非 Agent 错）
2. **字段匹配**：列名能否命中表头？（注意 `\n` 尾注 + 点分路径翻译）
3. **类型保持**：新值类型是否与列类型一致？
4. **枚举约束**：新枚举值是否合法？
5. **FK 影响面**：修改主键 ID → 所有引用该 ID 的子表行需同步更新？

### 7.3 删除（delete）校验

1. **级联清理**：
   - 删 NPC prefab → 同步删 interaction + spawn_world_entity 中引用该 prefab_id 的行
   - 删 interaction → 同步删 InteractionConv / InteractionConvOption 子行
   - 删 item → 同步删对应子表（Equipment/Potion/Gem/Chest/Fabao）行
2. **反向引用检查**：被删行是否被其他表引用？（如删 reward 前查 Chest/reward 引用）

## 8. 常见错误模式（Anti-Patterns）

| 错误 | 现象 | 修正 |
|---|---|---|
| 列名带 `\n` 尾注 | `编号\n（...）` 匹配失败 | 取 `\n` 前主名匹配 |
| `effect.key` 直送 matcher | 点分键非真实列，三阶段全败 | 先点分翻译为 `交互效果编号` |
| list 写成 JSON | `'["a","b"]' 列写字符串枚举 | `quality='凡品'` 类型错 | 先枚举映射 `quality=1` |
| 主键冲突 | 新增 ID 已存在 | 取 max+1，不硬编码 |
| 漏子表级联 | 只写主表，子表 missing | 按 §2 外键图补全 |
| `model_id` 当列名 | 不是真实列，是 splitter 产 | 映射到 `model_prefab` 列 |
| 修改未存在行 | row_key 查不到 | 先查 pristine 确认行存在 |

## 9. 与 skill_loader 配置的关系

本 md 是**人类可读约束文档**。机器可读配置在：

| 配置文件 | 作用 | 与本 md 关系 |
|---|---|---|
| `L1_derived/column_aliases.yaml` | 列别名映射 | §1.3 点分翻译应补充进此 |
| `L1_derived/row_aliases.yaml` | 行定位规则 | §3 ID 段约束辅助行定位 |
| `L1_derived/cascade_rules.yaml` | 级联规则 | §2 外键图应同步进此 |
| `L1_derived/value_constraints.yaml` | 取值约束 | §4 必填/§6 枚举应进此 |
| `L1_derived/enum_mappings.yaml` | 枚举映射 | §6 枚举值表应进此 |
| `table_relations.json` | 外键关系 | §2 外键图数据源 |

**维护原则**：本 md 描述理想约束，yaml/json 是执行配置。约束变更时**先改 md（文档）再同步 yaml（配置）**，避免配置漂移。

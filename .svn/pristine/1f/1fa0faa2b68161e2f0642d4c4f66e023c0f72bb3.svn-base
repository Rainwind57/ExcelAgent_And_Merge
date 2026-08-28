# pve_combat_npc 表填表知识

## 表结构（resources/combat/pve_combat_npc.xlsx，sheet: PveCombatNpc）

- `npc_id`（id:int）主键，取段内 max+1（3000+）。
- `model_id`（model_id:int）怪物模型 ID，FK→model_prefab。用户说「model_id 1200」→ 1200。
- `name`（name:string）怪物名字，中文直接存（如「焚天赤龙」）。
- `spell_ids`（spell_ids:list[int]）技能列表，逗号分隔串（如 `9101,9102,9103,9104`），禁止 JSON 数组。
- `combat_ai_name`（combat_ai_name:string）AI 行为树名，用户说「AI 用 boss_ai」→ `boss_ai`。
- `attribute_base_slopes.HPMaxCon[1]`（气血斜率:int）气血成长公式斜率，用户说「气血斜率 60」→ 60。
- `attribute_base_slopes.HPMaxCon[0]`（气血基础:int）气血成长公式基础值，用户说「气血基础 500000」→ 500000。
- `attribute_base_slopes.PhyAtkCon[1]`（物攻斜率:int）物攻成长斜率，用户说「物攻斜率 40」→ 40。
- `attribute_base_slopes.PhyAtkCon[0]`（物攻基础:int）物攻基础值，用户说「物攻基础 30000」→ 30000。
- `level_formula`（level_formula:int）等级公式，用户说「等级公式 80」→ 80。
- 其余属性列（MagAtk/Crit/Def 等）可选，用户未提及留空。

## 填值规则

- 嵌套点分键（如 `attribute_base_slopes.HPMaxCon[1]`）是 Row2 规范名，fields 键用点分规范键，不要用中文显示名。
- `[0]` 表示基础值，`[1]` 表示斜率，不要搞反。
- spell_ids 是 list[int]，用逗号分隔串写入，禁止 `[]`。
- npc_id 若本批产出（新增 BOSS），用 `<new_pve_combat_npc_id>` produces 标签。

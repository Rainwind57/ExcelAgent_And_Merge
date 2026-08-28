# gameplay_ability_choice_pool 表填表知识（词条池 / 保底掉落池）

## 表结构（resources/ability.xlsx，sheet: GameplayAbilityChoicePool）

- `entry_id`（entry_id:int）主键，取段内 max+1（100001+）。用户说「pool_id 3001」时直接用 3001。
- `attribute`（attribute:string）属性修改键，填英文规范名（如 `PhyAtkCon` / `MagAtkCon` / `CritCon`）。
- `value_min`（value_min:int）属性范围下限。用户说「范围 100-200」→ 100。
- `value_max`（value_max:int）属性范围上限。用户说「范围 100-200」→ 200。
- `weight`（weight:int）权重。用户说「权重 20」→ 20。
- `ability_type`（ability_type:int）能力类型。用户说「能力类型都填 2」→ 2。

## 填值规则

- 一个 pool_id 下多个属性条目（PhyAtkCon/MagAtkCon/CritCon）→ 每条属性一行，pool_id 相同。
- entry_id 是主键，每行递增；pool_id 是分组键，多条同 pool_id。
- 用户说「属性修改含 PhyAtkCon 范围 100-200 权重 20、MagAtkCon 范围 100-200 权重 20、CritCon 范围 5-15 权重 10，能力类型都填 2」→ 产 3 条 intent（同 pool_id，不同 attribute/value_min/value_max/weight，ability_type 都=2）。
- attribute 填英文规范名，不要填中文。
- value_min/value_max 是 int，填整数。
- weight 是 int，填整数。

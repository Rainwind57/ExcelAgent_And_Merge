# spawn_world_entity 表填表知识

## 表结构（resources/spawn_world_entity.xlsx，sheet: SpawnWorldEntity）

- `spawn_id`（id:int）主键，取段内 max+1（50001+）。
- `space_id`（space_id:int）战场 ID，FK→space.space_id。用户说「战场 10050」→ 10050。
- `entity_prefab_id`（entity_prefab_id:int）实体 prefab ID，FK→entity_prefab.prefab_id。引用本批新产出的 NPC prefab 用 `<new_entity_prefab_id>` 占位符 + consumes。
- `pos_list`（pos_list:list[tuple[float,float,float]]）场景坐标，元组逗号分隔串。用户说「坐标 (200,0,150)」→ `200, 0, 150`。
- `entity_class`（entity_class:string）实体类型，PVE 世界 BOSS 一般填 `WorldMonster`。
- `name`（name:string）实体名字，可选。
- 其余列（autofill/trigger 等）可选，未提及留空。

## 填值规则

- pos_list 是 list[tuple[float,float,float]]，写入格式 `(x, y, z)` 元组逗号分隔（如 `(200, 0, 150)`），多坐标用 `,` 分隔。
- entity_prefab_id 若引用本批新产出的引导 NPC / BOSS prefab，用 consumes 占位符，不要硬编码未知 ID。
- spawn_id 用户显式给时直接用，未给时取段内 max+1。

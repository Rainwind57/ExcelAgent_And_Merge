# spawn_world_entity 表校验约束

# 原则：空字段除非是主键，否则不校验必填性。

```yaml
tables:
  spawn_world_entity:
    SpawnWorldEntity:
      columns:
        spawn_id:
          type: int
          required: true
          unique: true
          min: 50001
        space_id:
          type: int
        entity_prefab_id:
          type: int
        pos_list:
          type: list
```

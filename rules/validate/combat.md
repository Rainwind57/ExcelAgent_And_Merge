# combat 表校验约束

# 原则：空字段除非是主键，否则不校验必填性。

```yaml
tables:
  combat:
    CombatData:
      columns:
        combat_id:
          type: int
          required: true
          unique: true
        space_id:
          type: int
        win_reward:
          type: int
        lose_reward:
          type: int
        draw_reward:
          type: int
```

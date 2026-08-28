# pve_combat_npc 表校验约束

# 原则：空字段除非是主键，否则不校验必填性。

```yaml
tables:
  pve_combat_npc:
    PveCombatNpc:
      columns:
        npc_id:
          type: int
          required: true
          unique: true
          min: 3000
        model_id:
          type: int
        name:
          type: string
        spell_ids:
          type: list
        combat_ai_name:
          type: string
```

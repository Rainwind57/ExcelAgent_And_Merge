# entity_prefab 表校验约束

# 原则：空字段除非是主键，否则不校验必填性。

```yaml
tables:
  entity_prefab:
    Base:
      columns:
        prefab_id:
          type: int
          required: true
          unique: true
        model_id:
          type: int
        entity_name:
          type: string
        entity_class:
          type: string
          enum:
            - WorldNonPlayer
            - WorldInteractiveObject
            - WorldSiegeCart
            - ResidenceEntity
            - ResidenceEntryEntity
            - ResidenceWorldEntryEntity
            - CandidateResidenceEntryEntity
            - EffectActor
            - WorldMonster
```

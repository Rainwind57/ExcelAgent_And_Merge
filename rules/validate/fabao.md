# fabao 表校验约束

```yaml
tables:
  fabao:
    FabaoLevel:
      primary_key: [法宝id, 法宝等级]
      columns:
        法宝id:
          type: int
          required: true
        法宝等级:
          type: int
          required: true
        技能id:
          type: int
        技能等级:
          type: int
    Fabao:
      primary_key: [法宝id]
      columns:
        法宝id:
          type: int
          required: true
          unique: true
    FabaoUpgradeCost:
      primary_key: [法宝等级]
      columns:
        法宝等级:
          type: int
          required: true
          unique: true
```

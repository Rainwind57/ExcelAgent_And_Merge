# school 目录表校验约束

说明：school.School 的主键列 row1 表头为「门派」（row2 规范名 `school`），不含 "id"
字样，导致默认 PK 推断（取所有含 id/编号 列）漏掉真实主键，反而把
`model_id/combat_model_id/school_ability_id` 等误当复合主键 → 多门派共享同一
`model_id`（如 1027）时误报 unique_violation。此处显式声明真实单列主键收敛误报。
仅声明 primary_key（不加 required），避免对自增产出主键（新增门派走 produces
占位，无字面主键值）误报 missing_required。

```yaml
tables:
  school:
    School:
      primary_key: [school]
```

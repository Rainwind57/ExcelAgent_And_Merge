# item 表校验约束

```yaml
tables:
  item:
    ItemBase:
      columns:
        quality:
          type: int
          required: true
          enum: [1, 2, 3, 4, 5]
        item_id:
          type: int
          required: true
          unique: true
          min: 10000
          max: 29999
        item_type:
          type: int
          required: true
          enum: [1, 2, 3, 4, 5, 6, 7, 15]
```

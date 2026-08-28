# activity 表校验约束

# 原则：空字段除非是主键，否则不校验必填性。
# 只有主键列标 required: true，其他列只约束类型/枚举/范围（值非空时才校验）。

```yaml
tables:
  activity:
    Activity:
      columns:
        活动id:
          type: int
          required: true
          unique: true
          min: 1000
        活动类型:
          type: int
          enum: [1, 2, 3, 4, 5, 6]
        活动名称:
          type: string
        活动描述:
          type: string
        活动图标:
          type: string
        活动开始时间:
          type: string
        活动结束时间:
          type: string
        活动展示开始时间:
          type: string
        活动展示结束时间:
          type: string
        开启条件:
          type: string
```

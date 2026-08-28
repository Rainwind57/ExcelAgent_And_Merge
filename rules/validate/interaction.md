# interaction 表校验约束

# 原则：空字段除非是主键，否则不校验必填性。

```yaml
tables:
  interaction:
    Interaction:
      columns:
        编号:
          type: int
          required: true
          unique: true
          min: 10001
        交互效果编号:
          type: int
          enum: [3001, 3002, 3003, 3004, 3005, 3006, 4003, 4004]
    InteractionConv:
      columns:
        编号:
          type: int
          required: true
          unique: true
        对话内容:
          type: string
        选项1:
          type: int
        选项2:
          type: int
        选项3:
          type: int
        选项4:
          type: int
        选项5:
          type: int
        选项6:
          type: int
    InteractionConvOption:
      columns:
        编号:
          type: int
          required: true
          unique: true
        选项内容:
          type: string
        选项功能:
          type: int
        "1:新对话ID":
          type: int
```

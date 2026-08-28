# reward 表校验约束

# 原则：空字段除非是主键，否则不校验必填性。

```yaml
tables:
  reward:
    Reward:
      columns:
        reward_id:
          type: int
          required: true
          unique: true
          min: 10001
        名称:
          type: string
        每日领取上限:
          type: int
        经验概率:
          type: int
          min: 0
          max: 100
        金币概率:
          type: int
          min: 0
          max: 100
        银币概率:
          type: int
          min: 0
          max: 100
        必得道具1:
          type: tuple
        道具1数量:
          type: int
        必得物品2:
          type: tuple
        道具2数量:
          type: int
```

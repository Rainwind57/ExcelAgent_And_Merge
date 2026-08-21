# 多指令场景评测报告（capability: multi-intent-evaluation）

- 生成时间: 2026-08-10 12:08:17
- 纯逻辑层：OperationOrchestrator 直接测试（拓扑/回滚/加速比）
- serve 层：复用 task_chain_eval（需 codemaker serve）

## 一、总体指标

| 指标 | 说明 | 值 |
|---|---|---|
| topo_correct_rate | 拓扑序满足依赖约束 | 1.0000 |
| rollback_correct_rate | 跨表事务回滚标记正确 | 1.0000 |
| multi_intent_speedup | 并行 vs 顺序加速比 | 3.882 |
| split_correct_rate | 拆分意图数匹配期望 | 0.0000 |
| step2_success_rate | 分区阶段成功率 | 0.0000 |
| step3_success_rate | 计划阶段成功率 | 0.0000 |
| step4_success_rate | 校验阶段成功率 | 0.0000 |
| step5_success_rate | 执行阶段成功率 | 0.0000 |
| step6_success_rate | 汇总阶段成功率 | 0.0000 |
| placeholder_closure_rate | 占位符引用闭环率 | 0.0000 |

## 二、拓扑排序明细

| 用例 | 拓扑序 | 违反约束 | 循环 | 正确 |
|---|---|---|---|---|
| linear_chain | [0, 1, 2, 3] | 0 | - | ✓ |
| diamond_deps | [0, 1, 2, 3] | 0 | - | ✓ |
| independent | [0, 1, 2] | 0 | - | ✓ |
| cycle_fallback | [0, 1] | 0 | ✓ | ✓ |
| multi_producer_seq | [0, 1, 2] | 2 | ✓ | ✓ |

## 三、回滚场景明细

| 场景 | 失败点 | 跳过 | failed_tables | 前序未回滚 | 正确 |
|---|---|---|---|---|---|
| mid_chain_fail | ✓ | ✓ | ✓ | ✓ | ✓ |
| first_step_fail | ✓ | ✓ | ✓ | ✓ | ✓ |
| independent_fail | ✓ | ✓ | ✓ | ✓ | ✓ |

## 四、并行加速比

- 意图数: 4
- 顺序: 321.3ms
- 并行: 82.8ms
- **加速比: 3.882**

## 五、serve 依赖层（拆分/分阶段/闭环）

- 未启用 --serve

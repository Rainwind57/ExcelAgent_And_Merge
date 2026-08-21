# merge 大表正确性 + 性能评测报告（capability: merge-evaluation）

- 生成时间: 2026-08-10 12:08:14
- 执行方式: 进程内直接调 server/engine/ 引擎函数（不走 HTTP）
- 种子: server/tests/merge_fixtures/seeds（小种子）+ bigdata（10w 行）

## 一、正确性指标（小种子）

| 指标 | 说明 | 值 |
|---|---|---|
| merge_success_rate | 自动解决冲突 / 总冲突 | 1.0000 |
| false_conflict_rate | 假冲突 / 总冲突 | 0.5000 |
| id_remap_accuracy | 重映射正确 / 全部重映射 | 1.0000 |
| ref_integrity_pass_rate | 无 dangling sheet / 总 sheet | 1.0000 |
| 总冲突单元格 | | 2 |
| 自动合并数 | | 2 |
| 假冲突数 | | 1 |
| ID 重映射数 | | 1 |
| ID 重映射正确 | | 1 |

### 各 sheet 明细

| sheet | 行数 | 冲突 | 变更 | 自动合并 | 假冲突 | 重映射 | dangling |
|---|---|---|---|---|---|---|---|
| TestData | 10 | 2 | 2 | 2 | 1 | 1 | 0 |

## 二、大表性能（10w 行）

| 阶段 | 耗时(ms) |
|---|---|
| compare | 3943.1 |
| resolve(auto_merge) | 130.4 |
| apply(fast_xml) | 6335.8 |
| **total** | **10409.2** |

- 总行数: 100000（4 sheet）
- apply 快路径: ✓

## 三、并行比对加速比

| 模式 | 耗时(ms) |
|---|---|
| 串行 | 4141.5 |
| 并行(4 worker) | 3758.3 |

- **加速比: 1.102**

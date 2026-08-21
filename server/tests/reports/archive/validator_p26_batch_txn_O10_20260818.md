# O10 validator 第三批 P26 — 批级事务/部分回滚

> 轮次：O10（2026-08-18）
> 范围：validator 第三批 P26（批级事务/部分回滚）。**P27**（4-step NL 路径 checkpoint）留 follow-up（NLIntent 序列化 + 续跑协议，改动面更广）。见 `docs/OPTIMIZATION_LEDGER.md` §1 + §4 + `docs/validator_audit.md` §3。
> LLM e2e（`table_case_eval.py`）被 R7 阻断（ledger §2.1），本轮证据为确定性单测。

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| P26 属性 | `server/agent/excel/core/agent.py:582` `__init__` | 加 `self.batch_transactional = os.getenv("CODEMAKER_BATCH_TRANSACTIONAL", "0") == "1"`。默认 off。 |
| P26 方法 | `server/agent/excel/core/agent.py:707` `_compute_rollback_targets` staticmethod | 新增。计算硬失败时回滚目标集。strict（batch_transactional=True）→ 回滚整批前序已 commit op（不限直接依赖）；默认 → G8 链回滚（仅 _deps_map[orig_idx] 直接依赖 producer）；无依赖 → 空。返回 (mode, targets)。 |
| P26 接入 | `server/agent/excel/core/agent.py:4465` run() hard-failure 分支 | 改用 `self._compute_rollback_targets(orig_idx, partitions, _deps_map, self.batch_transactional, has_dependencies)`，替换原内联 G8 逻辑。thinking 标注 mode（P26-batch-transactional / G8-chain / none）。 |
| 测试 | `server/tests/test_agent_p26_batch_txn.py`（新增 9） | strict 回滚全部前序 / 排除已 rolled_back / 排除无 backup / G8 默认仅直接依赖 / 无依赖不回滚 / strict 无依赖也回滚 / G8 空 deps / 属性默认 off / env=1 开启。 |

## 确定性验证

```
python -m pytest server/tests/test_agent_p26_batch_txn.py -v
=> 9 passed in 2.61s

python -m pytest server/tests/test_agent_p26_batch_txn.py \
               server/tests/test_optimizations_e2e.py \
               server/tests/test_execute_agent.py \
               server/tests/test_multi_table_orchestration.py \
               server/tests/test_validate_agent_two_layer.py \
               server/tests/test_parse_agent.py \
               server/tests/test_decompose_agent.py \
               server/tests/test_conclude_agent.py \
               server/tests/test_agent_p19_mutex.py \
               server/tests/test_validator_tips_to_failures_p23.py \
               server/tests/test_validator_unified_entry_p21.py -q
=> 178 passed in 22.46s   # 零红
```

## 量化

| 指标 | before | after | delta |
|---|---|---|---|
| P26 直接单测 | 0 | 9 | +9 |
| 全相关回归 PASS | 204（O9 后） | 178 | *（测试集子集不同,非回归；均零红） |
| 全相关回归红测 | 0 | 0 | 0（零新红） |
| 批级事务能力（任一失败全回滚） | 无（仅 G8 直接依赖） | opt-in strict | 新增 |
| district 成功+combat 失败留半成品→重跑 UNIQUE_VIOLATION | 是（默认 G8 不回滚独立 op） | opt-in strict 回滚 | 可选消除 |

## 根因归因

| 修复项 | 贡献 | 证据 |
|---|---|---|
| P26 strict 批级事务 | G8 链回滚仅回滚失败步直接依赖 producer，独立 op（如 district 不被 combat 直接依赖）不回滚 → 留半成品 → 重跑 UNIQUE_VIOLATION。strict 模式回滚整批前序已 commit op，批级原子。opt-in 避免牵连无关独立 op（默认 G8 保留）。 | `test_batch_transactional_rolls_back_all_prior`（strict 回滚 A+B vs G8 仅 B）+ `test_g8_default_rolls_back_only_direct_deps`（默认 G8 行为不变） |

## 注意事项

- P26 设计决策：opt-in（`CODEMAKER_BATCH_TRANSACTIONAL=1` 默认 off）。保留 G8 链回滚为默认（避免 strict 牵连无关独立 op 在复杂链下误伤）。strict 场景：用户显式开启批级原子，接受「任一失败全回滚」语义，换取消半成品残留 + 重跑 UNIQUE_VIOLATION。
- P26 回滚目标集基于 `partitions[pi].get("backup")`（成功 commit 后才有 backup，line 4450）+ `not rolled_back`。real 执行中，失败 op 之后的 op 未执行（无 backup）→ 不会被误回滚。单测用 `has_backup=False` 模拟未执行 op。
- P27（4-step NL 路径 checkpoint：parse/validate 后拍中间态 NLIntent 序列化，stall 可续跑免 Step1 重 LLM decompose）留 follow-up。涉及 NLIntent 序列化格式（dataclass → JSON）+ 续跑恢复 + skip 已成功项）+ agent_service._session_checkpoints 扩展，改动面更广，需独立提案。
- LLM e2e `table_case_eval.py` 未跑：R7 阻断（serve agentic LLM 慢/贵，非 excel-agent code 可修，ledger §2.1）。
- validator 第三批收尾：P26 ✅；P27 留 follow-up。validator 全 P 项状态：P10/P11/P12/P13/P17 ✅(O5/O6)，P22/P23/P19/P21/P24/P25 ✅(O8/O9)，P26 ✅(O10)；剩 P9（降级，4-step 不触发）/P14（opt-in 默认 off）/P27（follow-up）。

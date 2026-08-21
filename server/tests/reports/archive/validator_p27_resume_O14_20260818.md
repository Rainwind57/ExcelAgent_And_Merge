# O14 P27 resume 全闭环

> 轮次：O14（2026-08-18）
> 范围：P27 4-step NL 路径 resume follow-up 收尾 — stall 检测 + per-op 成功跟踪 + Step5 增量 save + produced 重算。见 `docs/OPTIMIZATION_LEDGER.md` §1 + §4 + `docs/validator_audit.md` §3。
> LLM e2e（`table_case_eval.py`）被 R7 阻断（ledger §2.1），本轮证据为确定性单测。

## 设计决策（4 项 ask_user_question 定）

| # | 决策 | 选项 | 理由 |
|---|---|---|---|
| Q1 | stall 检测方式 | 显式 env `CODEMAKER_4STEP_RESUME=<session_id>` 触发（非后台 heartbeat） | 免竞态 + 可确定性单测；heartbeat auto-trigger 留 serve 修复后 |
| Q2 | per-op 成功跟踪字段 | `completed_op_keys` 集合（orig_idx list） | 粒度=op 级，key 稳定（orig_idx = partition index = intent index） |
| Q3 | resume 接线点 | run() 入口检测 + Step5 循环跳过 + 增量 save | 完整闭环；不增量 save 则 resume 后 crash 仍从 Step5 头跑（重复 UNIQUE_VIOLATION） |
| Q4 | produced 重建 | 从 checkpoint execution 重算（`_capture_produced` 重放 result_rows） | 免查库；复用主循环同一捕获逻辑，key 命名一致 |

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| P27 save 扩展 | `server/agent/excel/core/agent.py` `_save_nl_checkpoint` | 加可选参数 `completed_op_keys: Optional[list] = None`；checkpoint dict 加 `completed_op_keys` 字段（默认空 list，parse/validate 后无 op 完成）。 |
| P27 resume 三元组 | `server/agent/excel/core/agent.py` `_resume_from_checkpoint` | 返 `(intents, stage, completed_op_keys)` 三元组（原二元组）。优先 post_validate 回退 post_parse，从 ckpt dict 读 `completed_op_keys`。 |
| O14 增量 save | `server/agent/excel/core/agent.py` `_save_nl_progress`（新方法） | Step5 loop 成功 op 后增量回写：覆盖 post_validate/回退 post_parse stage 的 intents + completed_op_keys。opt-in，失败静默 False。 |
| O14 run() 入口 | `server/agent/excel/core/agent.py` run() ~line 3997 | env `CODEMAKER_4STEP_RESUME` + session_id 非空 → `_resume_from_checkpoint` → 取 `_resumed_intents` + `_resumed_completed` set + `_resumed_stage`；置 `_4step_parsed=True` + `cross_intents_nl` + `cross_action="4step_resume"`；thinking 推 "续跑" 阶段。 |
| O14 skip Step1 parse | `server/agent/excel/core/agent.py` run() ~line 4017 | `if _4step_parsed:` → `_4step_nl = cross_intents_nl`（skip ParseAgent）；`if _4step_nl and not _resumed_intents:` → skip validate_two_layer + post_parse/post_validate save（checkpoint 已是 post_validate）。 |
| O14 produced 重算 + Step5 filter | `server/agent/excel/core/agent.py` run() ~line 4499 | `if _resumed_completed:` → 逐个调 `_capture_produced` 重放 result_rows（从 checkpoint execution 回填 res.result_rows）→ produced dict；filter `ordered_idx = [i for i in ordered_idx if i not in _resumed_completed]`。 |
| O14 Step5 增量 save | `server/agent/excel/core/agent.py` run() Step5 else 分支 ~line 4672 | 成功 op 后（`_out_ok is not False`）→ execution 回填到 intent + 累积 completed_op_keys（resume_completed + 本轮已成功 op）→ `_save_nl_progress`。 |
| 测试 | `server/tests/test_agent_p27_checkpoint.py` | 旧 13 测更新为三元组签名（`intents, stage, completed`）；新增 `TestO14CompletedOpKeys` 7 测。 |

## 确定性验证

```
python -m pytest server/tests/test_agent_p27_checkpoint.py -q
=> 20 passed in 1.70s   # 13 旧 + 7 O14 新

python -m pytest server/tests/test_agent_p27_checkpoint.py \
               server/tests/test_validator_p9_p14_p27.py \
               server/tests/test_produces_inference_p0.py \
               server/tests/test_validator_forward_refs_p13.py \
               server/tests/test_validator_tips_to_failures_p23.py \
               server/tests/test_agent_p19_mutex.py \
               server/tests/test_validator_unified_entry_p21.py \
               server/tests/test_agent_p26_batch_txn.py \
               server/tests/test_decompose_agent.py -q
=> 113 passed in 26.11s   # validator 线零红

python -m pytest server/tests/ -q
=> 942 passed, 1 failed, 1 skipped in 217.88s
   # 1 预存红：test_column_matcher_semantic::TestRapidFuzzMatching::test_fuzzy_simplified_colname_hits
   #   断言 r.source in ("rapidfuzz","similarity") 但得 "exact_substr"（列匹配器优先级）
   #   与 O14 无关（未触及 column_matcher.py，O14 仅动 agent.py checkpoint + Step5）
```

## 残留 follow-up

- **heartbeat auto-trigger**：显式 env 触发需用户/调度器在 crash 后设 env。真 auto-trigger 需后台线程监控 last_heartbeat 时间戳 → 竞态 + 难确定性单测，留 serve 修复后（R7 阻断 e2e，auto-trigger 无验证场景）。
- **e2e**：4-step resume 全链 e2e 阻于 R7（serve 侧 LLM 不可达），证据仍用确定性单测。
- **P24/P25 真 partial 态**：需反转 O3 非阻断 design 决策（O8 选 soft-failure 通道 P23 而非 partial-skip），非接线，留 design follow-up。

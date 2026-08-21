# O12 validator follow-up — P27 接线 + P24/P25 design 评估

> 轮次：O12（2026-08-18）
> 范围：P27 4-step 路径 save 接线 + `_resume_from_checkpoint` 方法；P24/P25 真 partial 态 design 评估（需反转 O3 非阻断，文档化）。见 `docs/OPTIMIZATION_LEDGER.md` §1 + §4 + `docs/validator_audit.md` §3。
> LLM e2e（`table_case_eval.py`）被 R7 阻断（ledger §2.1），本轮证据为确定性单测。

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| P27 属性 | `server/agent/excel/core/agent.py:613` `__init__` | 加 `self._nl_checkpoints: dict = {}`（TableAgent 实例级，agent_service 复用 self.agent → 跨 run() 持久；session_id 隔离多会话）。 |
| P27 方法 | `server/agent/excel/core/agent.py:723` | 加 `_save_nl_checkpoint` / `_load_nl_checkpoint` / `_resume_from_checkpoint` 三方法。save opt-in `CODEMAKER_4STEP_CHECKPOINT=1`，序列化 NLIntent[]（to_checkpoint_dict）到 `_nl_checkpoints[session_id][stage]`。resume 优先 post_validate 回退 post_parse。 |
| P27 save 接线 | `server/agent/excel/core/agent.py:3913`（post_parse）+ `:4042`（post_validate） | 4-step 路径两 save 调用接线：ParseAgent 产出后 save "post_parse"；validate_two_layer 后 save "post_validate"。opt-in，默认 off→no-op。 |
| P24/P25 评估 | （文档化，无代码改动） | 真 partial 态（重激活 `_mark_validation_skipped` + `_phase_execute:5512` skip 分支）需反转 O3 非阻断设计。O8 已选 soft-failure 通道（P23）而非 partial-skip → 重激活 partial-skip 是 design 决策非接线，留 design follow-up。 |
| 测试 | `server/tests/test_agent_p27_checkpoint.py`（新增 13） | save off/on/round-trip/missing/empty/failure/overwrite + resume off/prefers_post_validate/falls_back/none/empty_session/isolation 13 态。 |

## 确定性验证

```
python -m pytest server/tests/test_agent_p27_checkpoint.py -v
=> 13 passed in 1.80s

python -m pytest server/tests/test_agent_p27_checkpoint.py \
               server/tests/test_validator_p9_p14_p27.py \
               server/tests/test_validate_agent_two_layer.py \
               server/tests/test_parse_agent.py \
               server/tests/test_decompose_agent.py \
               server/tests/test_produces_inference_p0.py \
               server/tests/test_validator_forward_refs_p13.py \
               server/tests/test_validator_tips_to_failures_p23.py \
               server/tests/test_validator_unified_entry_p21.py \
               server/tests/test_agent_p19_mutex.py \
               server/tests/test_agent_p26_batch_txn.py \
               server/tests/test_optimizations_e2e.py \
               server/tests/test_execute_agent.py \
               server/tests/test_multi_table_orchestration.py \
               server/tests/test_conclude_agent.py \
               server/tests/test_subagent_roles.py -q
=> 247 passed in 29.09s   # 零红
```

## 量化

| 指标 | before | after | delta |
|---|---|---|---|
| P27 接线直接单测 | 0 | 13 | +13 |
| 全相关回归 PASS | 234（O11 后） | 247 | +13 |
| 全相关回归红测 | 0 | 0 | 0（零新红） |
| P27 save 接线（4-step 两处） | 无 | post_parse + post_validate 接线 | 新增 |
| P27 resume 方法 | 无 | _resume_from_checkpoint（优先 post_validate） | 新增 |

## 根因归因

| 修复项 | 贡献 | 证据 |
|---|---|---|
| P27 save 接线 | 4-step NL 路径 stall/放弃 → 从 Step1 重 LLM decompose（N 次 LLM 成本白花）。save 接线拍 parse/validate 后中间态，供 resume 续跑免重 decompose。opt-in 默认 off→no-op，零回归风险。 | `test_save_on_writes_checkpoint` + `test_save_load_round_trip` + 4-step 路径两 save 调用 |
| P27 resume 方法 | `_resume_from_checkpoint` 优先 post_validate（最远中间态）回退 post_parse，session_id 隔离。供未来 resume 自动跳过 parse 接线。 | `test_resume_prefers_post_validate` + `test_resume_falls_back_post_parse` + `test_resume_isolation_per_session` |
| P24/P25 design 评估 | 真 partial 态需反转 O3 非阻断（O8 选 soft-failure P23 而非 partial-skip）。文档化为 design follow-up，避免无设计决策的接线引入回归。 | validator_audit.md §3 P24/P25 标注 O12 design 评估 |

## 注意事项

- P27 save 接线 opt-in（`CODEMAKER_4STEP_CHECKPOINT=1` 默认 off）。默认 off→save no-op，零回归。生产开启需评估序列化开销 + `_nl_checkpoints` 内存增长（可加 TTL/LRU，留 follow-up）。
- P27 resume 自动跳过 parse + skip已成功 Step5 留 follow-up：需 (a) stall 检测（如何判定「上一次 run() stall 了」——超时？异常中断？显式 resume flag？）；(b) per-op 成功跟踪（checkpoint 存已成功 Step5 的 stem/sheet，resume 时 skip）；(c) e2e 验证。当前 save + resume 方法 + round-trip 单测已落地，自动跳过接线需独立提案。
- P24/P25 真 partial 态：O8 选 soft-failure 通道（P23 tips→failures）而非 partial-skip，故重激活 partial-skip 与 O3/O8 设计相悖。需 design 决策（是否为某子集场景引入 partial-skip 语义），非单纯接线。当前 dormant skip 代码保留（O9 注释），design follow-up。
- LLM e2e `table_case_eval.py` 未跑：R7 阻断（serve agentic LLM 慢/贵，非 excel-agent code 可修，ledger §2.1）。
- **validator 线状态**：P9–P27 全部消解（O5/O6/O8/O9/O10/O11/O12）。残留 follow-up：P27 resume 自动跳过 + P24/P25 真 partial 态（design 决策）。validator 线主体完成，可转向其他线（R7 / §3 八方法 / §5 merge / §6 跨模块）。

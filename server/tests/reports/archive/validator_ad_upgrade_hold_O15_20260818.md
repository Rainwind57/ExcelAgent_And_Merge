# O15 方法 A/D 升级接 pre_commit_hold 通道

> 轮次：O15（2026-08-18）
> 范围：AD1/AD2/A2/D4 闭环 — 方法 A（公式）+ 方法 D（批注）升级接 `pre_commit_hold` 通道 + agent 层消费 + D4 audit。见 `docs/OPTIMIZATION_LEDGER.md` §1 + §3 + `docs/archive/事前预防优化TODO.md` 第二波 AD。
> 前置：方法 B（`precommit_hold.py` 通道）已落地（R9-B1），`PreCommitHoldEvent` + `record_hold_audit` + `emit_hold_sse` 就位。

## 设计

CLI 层（`_save_with_cache_check`）已有公式 needs_manual_fix + 批注回写做差逻辑，缺口在 hold 事件不产 + agent 不消费。本轮：
1. CLI 构造 `PreCommitHoldEvent`（kind=formula_loss/comment_loss）+ `record_hold_audit` 留痕 + 附返回 dict（`hold_events` 字段）。
2. `CLICallResult` 加 `hold_events` 字段，9 写库点透传。
3. agent 层 `_handle_cli_hold_events` 消费 → #40 软失败追加 `res.failures`（保 D6 上报）+ SSE 推送。
4. CLI 无 SSE task 上下文 → 仅 audit + 附 dict，agent 层触发 SSE（CLI 构造/agent 上报分工）。

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| AD1/AD2 hold 事件构造 | `server/agent/excel/cli/cli_interface.py` `_save_with_cache_check` | 重构：公式分支 + hold 事件构造统一在 return 前。AD1 gate=hold + needs=True → `PreCommitHoldEvent(kind=formula_loss)` + `record_hold_audit`；AD2 still_lost>0 → `kind=comment_loss` + `record_hold_audit`。两 return（fast-path + 公式路径）均含 `hold_events`。 |
| D4 audit | `server/agent/excel/cli/cli_interface.py` `_save_with_cache_check` 批注二次做差分支 | still_lost>0 时 `auditor.record(operation="comment_replay_partial", extra={still_lost, lost_coords})`（原仅 warning）。 |
| CLICallResult 字段 | `server/agent/excel/cli/cli_interface.py` `CLICallResult` dataclass | 加 `hold_events: list = field(default_factory=list)`。 |
| 9 写库点透传 | `server/agent/excel/cli/cli_interface.py` write_cell/append_row/sort/shift×6/insert_column/delete_column/rename_column | 所有 `_save_with_cache_check` 后构造 `CLICallResult` 处加 `hold_events=cache_info.get("hold_events", [])`。 |
| merge_into 保留 | `server/agent/excel/formula/formula_ref_shifter.py` `merge_into` | 保留 `comment_replay` + `hold_events`（原重建 dict 丢）。 |
| A2 agent 消费 | `server/agent/excel/core/agent.py` `_handle_cli_hold_events`（新方法） | 消费 `cli_result.hold_events` → #40 软失败 dict 追加 `res.failures`（code/kind/severity/message/sheet/recommendation）+ 经 `_agent_subtask_sink` 推 `pre_commit_hold` SSE 事件。sink None/异常静默降级。 |
| A2 接线 _run_set | `server/agent/excel/core/agent.py` `_run_set` ~L2684 | `res.final` 后调 `_handle_cli_hold_events`（getattr 兼容轻量 mock）。 |
| A2 接线 _run_add | `server/agent/excel/core/agent.py` `_run_add`/`_do_append` ~L3601 | `r = append_row` 后调 `_handle_cli_hold_events`（getattr 兼容）。 |
| _write_cell_and_verify | `server/agent/excel/core/agent.py` `_write_cell_and_verify` | 始终 `verify["cli_result"] = r`（原仅失败携带，成功路径 res.final=None 无法消费 needs_manual_fix）。 |
| 测试 | `server/tests/test_ad_upgrade_hold_o15.py`（新增 9） | TestAD1FormulaLossHoldEvent 3（hold 产事件 / on 不产 / off 不产）+ TestAD2D4CommentLossHoldEvent 2（still_lost>0 产 comment_loss + comment_replay_partial audit / 无丢失不产）+ TestA2AgentConsumesHoldEvents 4（res.failures / SSE / noop / sink 异常吞）。 |

## 确定性验证

```
python -m pytest server/tests/test_ad_upgrade_hold_o15.py -q
=> 9 passed in 8.84s

python -m pytest server/tests/test_ad_upgrade_hold_o15.py \
               server/tests/test_formula_gate.py \
               server/tests/test_comment_guard.py \
               server/tests/test_merge_preflight.py \
               server/tests/test_patch_validator.py -q
=> 38 passed in 21.15s   # 方法 A/B/C/D 线零红

python -m pytest server/tests/ -q
=> 951 passed, 1 failed, 1 skipped in 204.02s
   # 1 预存红：test_column_matcher_semantic::test_fuzzy_simplified_colname_hits
   #   与 O15 无关（列匹配器优先级 exact_substr vs rapidfuzz，未触及 column_matcher.py）
```

## 残留 follow-up

- **前端红 card**：`MergeGuideView.vue` + `index-*.js` minified 需重建，接 `pre_commit_hold` SSE 事件渲染拦截卡 + override 弹窗。
- **override 弹窗**：method B 的 `record_hold_audit` 已记 `operation=pre_commit_hold`，单独 `operation=pre_commit_hold_override` operation 留前端接时做。
- **方法 F/E/G/H**：八方法残留（见 ledger §3），D/A/B/C 首版 + O15 升级完成。

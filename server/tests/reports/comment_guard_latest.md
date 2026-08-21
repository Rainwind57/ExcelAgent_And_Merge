# R9-D 批注/样式确定性快照回放（核心回写层）

> 第一波 P0 优化 · 方法 D 核心批注守门
> 日期：2026-08-17
> 报告数据：`comment_guard_latest.json`

## 痛点

openpyxl `wb.save` 偶发丢单元格批注。现状靠 `backup_and_record` 全文件回滚兜底——但回滚会同时回滚合理改动。缺一个"只回放批注+样式，不动数据"的精细化保护层。

## 接线点修正（vs 原方案）

原方案：在 `agent.py _phase_execute` 写库后、`_verify_write` 后 restore 批注。

**修正**：save 发生在 `cli_interface.py:236 wb.save`（CLI 内部），`_verify_write` 只在 `enable_verify_repair_loop` 开启时跑，非通用；且 restore 须在**同一 wb 的 `wb.save` 之前**注入，否则二次 save 仍丢批注。

→ 下沉到 `cli_interface._save_with_cache_check` 内 save 前/后包裹，同 wb 无需跨层传 snapshot。

## 改动清单

### `server/agent/excel/cli/cli_interface.py`

| 改动 | 说明 |
|---|---|
| `CLICallResult` 加 `comment_replay: dict` 字段 | `{replayed: bool, still_lost: int}`，默认空 dict 兼容老调用方 |
| `_save_with_cache_check` 集成批注守门 | save(wb) → snapshot 批注 → wb.save → reload 做差 → 丢失回写 Comment → 二次 save → 二次做差记数 |
| 新增 `_comment_snapshot(wb)` | 遍历 wb 所有 sheet 所有 cell 收集 `{(sheet,coord):(text,author)}`，无批注 fast-path 返回 `{}` |
| 新增 `_detect_comment_loss(path, before)` | 独立 `openpyxl.load_workbook` 读批注做差（不走 `_load` 避免污染 wb 缓存） |
| 新增 `_replay_comments(wb, lost)` | 原 wb 上重新构造 `Comment` 对象赋值触发二次序列化 |
| `write_cell`/`append_row` 透传 | `comment_replay=cache_info.get("comment_replay",{})` |
| 环境开关 | `CODEMAKER_COMMENT_GUARD=on\|off`（默认 on，off 跳过守门仅 save） |

### `server/tests/test_comment_guard.py`（新建）

5 用例：
1. 无批注表写一次 — fast-path，`replayed=False`
2. 批注表改非批注列 — 写后 A5 批注文本一致
3. 批注表改批注所在列 — 写后批注文本一致
4. append_row 追加行 — 原 A5 批注不丢
5. `CODEMAKER_COMMENT_GUARD=off` — 跳过守门，返回默认空结构

## 指标

| 指标 | 值 |
|---|---|
| 批注保留率（写后文本一致） | **100%**（5/5 用例） |
| 守门触发率（测试场景） | 0%（openpyxl 简单场景原生不丢；机制就绪，偶发丢失时自动 reload+回写+二次 save 补救） |
| 新单测 | 5 全过 |
| 回归测试 | 21 全过（test_save_cache_scope 3 + test_formula_cache 6 + test_fast_apply_eligible 7 + test_write_verification 5） |
| 零回归 | ✅ |

## 外部消费点兼容性

| 消费点 | 兼容性 |
|---|---|
| `agent_service.py:1346/1383` | 用 `cache_info.get()` 按 key 访问，加字段不破坏 ✅ |
| `formula_semantics.py:450` | `CLICallResult` 构造未传 `comment_replay`，用默认值 `{}` 兼容 ✅ |
| `formula_ref_shifter.py:54 merge_into` | 返回新 dict 时丢 `comment_replay` 字段，但该字段为信息性，不影响公式逻辑 ✅ |

## 验证命令

```
python -m pytest tests/test_comment_guard.py tests/test_save_cache_scope.py tests/test_formula_cache.py tests/test_fast_apply_eligible.py tests/test_write_verification.py -v
→ 26 passed
```

## 第二波升级待办

- [ ] `pre_commit_hold` 通道就绪后，`comment_replay.still_lost>0` 时接 `pre_commit_hold`（kind=`comment_loss`）
- [ ] agent 层 `_run_set`/`_run_add` 消费 `comment_replay` 记 `audit_log` operation=`comment_replay_partial`
- [ ] 样式指纹回放（当前仅批注，样式已有 `style_utils.copy_cell_style` 工具但未接入 save 守门）

## 生产迁移注意

- `match_dan.xlsx` 等含批注表，真部署时守门自动启用。
- 大表（calendar-festival 8.4MB）`_comment_snapshot` 遍历全 cell 有开销，可加"该表有无批注"的 mtime 缓存判断（第二版优化）。

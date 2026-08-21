# R9-A1 公式守门 needs_manual_fix → audit 留痕 + 环境开关

> 第一波 P0 优化 · 方法 A 重新定义版阶段一
> 日期：2026-08-17
> 报告数据：`formula_guard_latest.json`

## 假设纠正（核心）

原方案"CLI 写后直接 save 不触发重算，公式守门需在 agent 层接线"——**经验证错误**。

实际：`cli_interface.py:217 _save_with_cache_check` 已被 `write_cell:599` / `append_row:641` 等所有写方法调用，内含 `snapshot_before` → `wb.save` → `validate_and_fix` → 丢失触发 LibreOffice 重算。

**真正缺口**：重算后仍丢（`result.needs_manual_fix=True`）时，`_save_with_cache_check:248` 只 `logger.warning`，不上报 agent 层。agent 层 grep `needs_manual_fix`/`cache_message` → **0 匹配**，信息死在 `CLICallResult.final` 里。

## 接线设计

CLI 层懒加载 BackupAuditor 记 audit（非 agent 层消费）。

**理由**：
- agent 层 `needs_manual_fix` grep 0 匹配完全忽略，auditor 在 agent 层（657/663）但改 agent.py 多处分散（3303/3413/2642/6585/2459/2530）。
- CLI 层记 audit 改动集中 `cli_interface.py`，agent 层不动，hold 阻断留第二波。

## 改动清单

### `server/agent/excel/cli/cli_interface.py`

| 改动 | 说明 |
|---|---|
| `StubCodeMakerCLI.__init__` 加 `self._auditor = None` | 懒加载 BackupAuditor 字段 |
| 新增 `_get_auditor()` | 懒加载 `BackupAuditor(workspace=self.workspace)`，加载失败返回 None 化为不记 audit |
| `_save_with_cache_check` needs_manual_fix 分支 | 读 `CODEMAKER_FORMULA_GATE` 开关：off=静默；on/hold=warning+`auditor.record(operation='formula_loss_detected', extra={cache_message, gate, replayed_comments, still_lost_comments})` |
| audit extra 携带 comment_replay 信息 | 方法 A+D 信息合并留痕（replayed/still_lost） |

### `server/tests/test_formula_gate.py`（新建）

5 用例（mock `snapshot_before`+`validate_and_fix` 聚焦 audit 逻辑，真实公式流程由 `test_formula_cache.py` 覆盖）：
1. 无公式表 — fast-path，audit 无记录
2. GATE=on + needs=True — audit 有 `formula_loss_detected`
3. GATE=off + needs=True — 静默不记
4. GATE=hold + needs=True — audit 留痕（阶段一等同 on，阻断留第二波）
5. needs=False — audit 无记录（重算成功）

## 环境开关

| `CODEMAKER_FORMULA_GATE` | 行为 |
|---|---|
| `on`（默认） | warning + audit_log 留痕 |
| `off` | 完全静默（不 warning 不 audit） |
| `hold` | warning + audit（阻断写库留第二波接 `pre_commit_hold`） |

## 指标

| 指标 | 值 |
|---|---|
| needs_manual_fix=True audit 覆盖率（on/hold） | **100%** |
| off 模式静默率 | 100% |
| 新单测 | 5 全过 |
| 回归测试 | 26 全过（formula_cache 6 + comment_guard 5 + save_cache_scope 3 + write_verification 5 + fast_apply 7） |
| 零回归 | ✅ |

## 外部兼容性

| 项 | 兼容性 |
|---|---|
| `BackupAuditor` 懒加载 | 复用 `server/backups/audit_log.jsonl` 路径约定，与 agent 层 auditor 写同一 jsonl（无状态 append，不冲突）✅ |
| `CLICallResult` | 未加新字段（已有 `needs_manual_fix`/`cache_message`），`write_cell`/`append_row` 透传不变 ✅ |
| agent 层 | 不动（hold 消费留第二波）✅ |

## 验证命令

```
python -m pytest tests/test_formula_gate.py tests/test_formula_cache.py tests/test_comment_guard.py tests/test_save_cache_scope.py tests/test_write_verification.py tests/test_fast_apply_eligible.py
→ 31 passed
```

## 第二波升级待办

- [ ] `pre_commit_hold` 通道就绪后，GATE=hold 模式接 `pre_commit_hold`（kind=`formula_loss`）阻断写库
- [ ] agent 层 `_run_set`/`_run_add` 消费 `CLICallResult.needs_manual_fix`，GATE=hold 时阻断 + SSE 推 `pre_commit_hold`

## 生产迁移注意

- `match_dan.xlsx` 真部署时 LibreOffice soffice 必须装（testtest 已配），重算后仍丢会触发 audit 留痕。
- `CODEMAKER_FORMULA_GATE` 默认 on，生产可按需调 hold（待第二波通道就绪）。
- audit_log.jsonl 会累积 `formula_loss_detected` 记录，可作为公式健康度监控数据源。

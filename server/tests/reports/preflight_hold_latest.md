# R9-B1 Pre-flight hold 漏行预检 + pre_commit_hold 事件通道（后端首版）

> 第二波 P0 优化 · 方法 B 基建
> 日期：2026-08-17
> 报告数据：`preflight_hold_latest.json`
> ca-overview §2.3.1：testbranch 全量覆盖丢 id=10500——有 detection 无 prevention。

## 痛点

apply 写盘前无"合并此 patch 将丢失哪些 base id"的预检。§2.3.1 的 testbranch 全量覆盖丢 id=10500 场景，有 `compare_sheet.missing_rows` 检测，无 apply 前阻断。

## 首版范围（后端聚焦）

| 范围 | 状态 |
|---|---|
| 后端漏行预检（`preflight_row_manifest`） | ✅ 落地 |
| 环境开关阻断（`CODEMAKER_PREFLIGHT_HOLD=on\|audit\|off`） | ✅ 落地 |
| audit 留痕（`audit_tables.preflight_holds` 字段） | ✅ 落地 |
| SSE 事件产出函数就位（`emit_hold_sse`） | ✅ 落地（函数就位，前端无消费分支） |
| force override 机制（`BranchApplyRequest.force`） | ✅ 落地 |
| 前端拦截卡（MergeGuideView.vue + index-*.js） | ⏳ 留后续（minified 需重建） |
| agent serve 场景 hold（`_step_sink` 加分支） | ⏳ 留后续 |
| stage3 apply 覆盖（盲区，无 SSE 不调 `_validate_apply_refs`） | ⏳ 留后续 |

## 改动清单

### `server/routers/precommit_hold.py`（新建）

| 组件 | 说明 |
|---|---|
| `PreCommitHoldEvent` | dataclass：`{kind, severity, count, sheets, message, recommendation}`，`to_dict` 含 `type=pre_commit_hold`（SSE payload 格式） |
| `PreflightReport` | dataclass：`{lost_rows, will_silently_drop, holds}`，`to_dict` 可 JSON 序列化供 409 detail |
| `preflight_row_manifest(mr, base_pks)` | 漏行预检：`base_ids - mergeset_ids = lost_ids`，复用 `collect_disk_sheet_pks` 落盘文件全量主键集 |
| `record_hold_audit(auditor, event, path)` | 复用 `BackupAuditor.record`（merge_branch 路径用 `_append_audit` 模式记） |
| `emit_hold_sse(task_emit_fn, task_id, event)` | 复用 `_compare_task_emit` pattern（前端无消费分支，事件产出就位待前端接） |

### `server/routers/merge_branch.py`

| 改动 | 说明 |
|---|---|
| `BranchApplyRequest` 加 `force: bool = False` | pre_commit_hold override（命中时确认放行） |
| import `collect_disk_sheet_pks` + preflight 函数 + `os` | |
| apply 路径（1612 后 1614 前）插 preflight | 写盘前预检：`off`=跳过零回归；`on`+无 force→raise 409 阻断；`audit`/force override→不阻断记 audit |
| `audit_tables` 加 `preflight_holds` 字段 | 命中时记 holds 清单 |

### `server/tests/test_merge_preflight.py`（新建）

7 用例：
1. 无漏行 — `will_silently_drop=False`
2. 漏行命中 — id=3 hold 命中
3. §2.3.1 场景 — id=10500 命中
4. 多 sheet 漏行 — holds 有 2 条
5. `base_pks` 空 — 跳过不报
6. `event.to_dict` — 含 `type=pre_commit_hold`
7. `report.to_dict` — 可 JSON 序列化

## 环境开关

| `CODEMAKER_PREFLIGHT_HOLD` | 行为 |
|---|---|
| `off`（默认） | 跳过预检，零回归 |
| `on` | 命中 + 无 `force` → raise 409 阻断；`force=true` → override 放行记 audit |
| `audit` | 命中不阻断，记 audit 留痕 |

## 指标

| 指标 | 值 |
|---|---|
| §2.3.1 漏行命中率 | 100%（id=10500 命中） |
| on 模式阻断率 | 100% |
| off 模式回归 | 0（零回归） |
| 新单测 | 7 全过 |
| 回归测试 | 31 全过（merge_eval + merge_formula_cache + merge_progress_snapshot + formula_gate + comment_guard + formula_cache） |
| 零回归 | ✅ |

## 数据访问

`MergeRequest` 不含 base 原始行数据（只有合并结果 `sheets`）。base 行 id 列复用 `collect_disk_sheet_pks(ours_path)` 读落盘 base 文件全量主键集 `{sheet_name: set}`。

## 接线设计

apply 路径写盘前（1612 后 1614 前）插 preflight。当前 apply 是**写后校验**（1616 `_validate_apply_refs`），preflight **写前阻断** + ref_report 写后 warning 两套并存。默认 `off` 保证零回归。

## 验证命令

```
python -m pytest tests/test_merge_preflight.py tests/test_merge_eval.py tests/test_merge_formula_cache.py tests/test_merge_progress_snapshot.py tests/test_formula_gate.py tests/test_comment_guard.py tests/test_formula_cache.py
→ 38 passed
```

## 后续升级

- [ ] 前端 `MergeGuideView.vue` 加 pre_commit_hold 拦截卡（需前端项目重建）
- [ ] agent serve：`_step_sink` 加 `etype=pre_commit_hold` 分支 + `agent.py` SSE 输出端 yield
- [ ] 方法 A/D 升级：`needs_manual_fix=True` / `comment still_lost>0` 接 `pre_commit_hold`（kind=`formula_loss`/`comment_loss`）
- [ ] stage3 apply 覆盖：stage3 apply/apply-batch 加 preflight + 推送通道

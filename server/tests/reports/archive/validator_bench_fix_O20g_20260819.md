# O20g set/delete locator 兜底提取（"删除名称为X的行"崩溃修复）

> 来源：用户实测「删除活动名称为春节活动的行」崩（Step3 locate_row 缺少行定位值）。
> 日期：2026-08-19。
> 状态：代码完成（11 单测绿，全量 1026 passed / 1 预存红零回归），e2e 阻 R7。

---

## 0. 根因

用户实测日志：
```
Step3 执行：match_locator 成功（列活动名称(3,mode=contains) 值=None）
           → locate_row: 缺少行定位值 → 通用 error_feedback retry → 未知错误中断
```

**根因链**：
1. DecomposeAgent `_to_split_intents` 只提取 LLM JSON 的 `table`/`sheet`/`action`/`fields`/`produces`/`consumes`，**漏产 `locator_field`/`locator_value`**。
2. DecomposeAgent `_build_prompt` 模板无 `locator_field`/`locator_value` 输出字段说明，LLM 不会产出这俩。
3. "删除活动名称为春节活动的行"类 set/delete 指令需行定位信号，DecomposeAgent 产的 SplitIntent `locator_value=None`。
4. `_run_set`/`_run_delete` 2504/2829 `if not intent.locator_value: res.add("locate_row", False, "缺少行定位值")` → 崩。
5. 走通用 error_feedback retry → verify-repair 多轮失败 → 未知错误中断。

---

## 1. 双保险修复

### 1.1 DecomposeAgent 提取 locator（上游）
- `_to_split_intents`：从 LLM JSON 提取 `locator_field`/`locator_value`，填入 SplitIntent（空串/空白视为 None）。
- `_build_prompt`：JSON 输出模板加 `locator_field`/`locator_value` 字段 + 规则说明"set/delete 操作用 locator_field+locator_value 标注定位行（如「删除活动名称为春节活动的行」→ locator_field=活动名称, locator_value=春节活动），fields 仅放需修改的列（delete 可空）"。

### 1.2 agent.py fields 兜底提取（下游）
- `_run_set`/`_run_delete` locate_row 前，`intent.locator_value` 为空时调新 helper `_fill_locator_from_fields(intent, loc_match)` 从 fields 字典兜底提取：
  - 按 `loc_match.column`（`_resolve_locator_and_mode` 已解析的定位列名）从 fields 取值。
  - `loc_match.column` 含后缀（如 `类型:int`）取 `:` 前段匹配。
  - 占位符值 `<...>` 不提取（非真实定位值）。
  - delete 操作：定位列从 fields 移除（避免 delete 把它当修改列重复处理）。
  - set 操作：定位列保留在 fields（set 可能改该列）。

---

## 2. 测试（`tests/test_locator_fallback_o20g.py` 11）

### TestToSplitIntentsLocator（3）
| 测试 | 场景 | 期望 |
|---|---|---|
| test_locator_field_value_extracted | LLM JSON 含 locator_field/locator_value | SplitIntent 提取 |
| test_locator_missing_defaults_none | LLM JSON 无这俩字段 | SplitIntent 这俩为 None |
| test_locator_empty_string_treated_as_none | 空串/空白 | 视为 None |

### TestFillLocatorFromFields（8）
| 测试 | 场景 | 期望 |
|---|---|---|
| test_extract_from_fields_by_column_name | fields 含定位列值 | 提取填 locator_value + delete 移除 |
| test_no_override_when_locator_value_present | locator_value 已有 | 不覆盖 |
| test_set_action_keeps_field_in_fields | set 操作 | 定位列保留在 fields |
| test_placeholder_value_not_extracted | fields 值为 `<...>` | 不提取 |
| test_column_with_suffix_stripped | loc_match.column 含后缀 | 取 `:` 前段匹配 |
| test_no_matching_column_noop | fields 无定位列 | 不提取 |
| test_empty_fields_noop | fields 空 | 不提取 |
| test_none_loc_match_noop | loc_match=None | 不提取 |

---

## 3. 确定性验证

| 测试文件 | 测数 | 覆盖 | 结果 |
|---|---|---|---|
| `tests/test_locator_fallback_o20g.py` | 11 | _to_split_intents×3 + _fill_locator_from_fields×8 | 11/11 passed |
| 相关回归（decompose/parse_agent/coverage_o20d/dedup_o20b/run_set_o20c/dry_run_o20f） | 73 | DecomposeAgent/agent 链路 | 73/73 passed |
| 全量回归 | 1026 | 全仓库 | 1026/1026 passed（1 预存红 `test_column_matcher_semantic`，1 skipped） |

**零回归**：1 预存红 `test_column_matcher_semantic`（O5-O20f 持续存在，与所有 O 改动无关）。

---

## 4. 残留 follow-up

1. **实跑验证**（阻 R7）：serve:8666 + backend:8000 未在线，O20g 待 serve 起后跑"删除活动名称为春节活动的行"确认不再崩在 locate_row。
2. **LLM 能力缺口**（§4.4）：单表漏拆子任务，G3 few-shot RAG 长期解。
3. **DecomposeAgent prompt locator 约束**：LLM 可能仍漏产 locator（O20g 下游 fields 兜底兜底，但依赖 fields 含定位列值；若 LLM 把"删除X的行"产 fields 空且无 locator → 仍崩，需 prompt 强化或规则模板兜底）。

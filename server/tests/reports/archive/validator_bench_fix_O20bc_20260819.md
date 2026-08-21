# O20b-c S1 重复写入 + S6 modify 失败信号修复

> 轮次：O20b+c（2026-08-19）
> 范围：需求文档 §2（S1 重复写入去重）+ §3（S6 modify 失败信号）。见 `docs/bench_failure_fix_requirements.md` §2/§3。

## O20b — S1 重复写入去重

### 改动

| 项 | 文件 | 改动 |
|---|---|---|
| _dedup_intents 新方法 | `server/agent/excel/subagent/validator_agent.py:630` | 按 (stem, sheet, action, locator_value, locator_field, fields_hash) 去重。区别 `_suppress_over_produce`（仅去 produces 过产）：本方法去完全重复（fields 一致）。BuildingInteract 不同 state 行 fields 不同不误杀，同表不同 locator（改不同行）不误杀。留首条，原地修改。 |
| validate_two_layer 接线 | `server/agent/excel/subagent/validator_agent.py:548` | 4-step 路径 validate_two_layer 开头调 `_dedup_intents`（在 validate_field_layer 前）。非阻断 ok 恒 True，去重后 intents 进 Step5。thinking 上报抑制条数。 |

### 测试

新增 `tests/test_dedup_intents_o20b.py` 8：
- 6 条相同 Quest → 去重留 1
- 不同 fields（BuildingInteract idle/collect）不误杀
- 不同 locator_value（改不同行）不误杀
- 不同表不互影响
- 空/single/partial-dup-mixed

### 验证

```
python -m pytest server/tests/test_dedup_intents_o20b.py \
               server/tests/test_validate_agent_two_layer.py \
               server/tests/test_validator_p9_p14_p27.py -q
=> 89 passed   # 含 O20b 8 + two_layer + P9-P27
```

### 残留

S1 实跑仍报"重复执行 6 次"（done_msg）。诊断：DecomposeAgent 产 6 条 intent 的 fields 可能含不同占位符（consumes `<new_combat_id>` 等）→ _dedup_intents 的 fields_sig 不同 → 不去重。需 DecomposeAgent prompt 约束"同表只产一条主配置"（§2.2 第 3 点）或 Step5 层查重。LLM e2e 实时 debug 太慢（246s/样例），留 follow-up。

---

## O20c — S6 modify 失败信号

### 改动

| 项 | 文件 | 改动 |
|---|---|---|
| _run_set 多字段失败入 failures | `server/agent/excel/core/agent.py:2685` | write 失败时入 `res.failures` 带 `{code:40, kind:write_failed, failed_col, failed_val, message}`（原仅 `failed.append(col_name)` + message 字符串，classifier 难提取列名）。 |
| _run_set 单字段 match_target 失败 | `server/agent/excel/core/agent.py:2700` | 目标列匹配失败入 `res.failures` 带 `{kind:column_not_found, failed_col=intent.target_field, message}`（原仅 message）。 |
| classify 读结构化 failed_col | `server/agent/excel/repair/error_classifier.py:179` | 优先从 `res.failures` 取结构化 failed_col + failed_val（比 detail regex 提取可靠）；kind=column_not_found 直接定类免 regex 漏。回退 regex 兜底（无 failures 时）。 |

### 测试

新增 `tests/test_run_set_failures_o20c.py` 5：
- kind=column_not_found → classify 直接定类 + failed_col/val
- kind=write_failed → classify 取 col（regex 兜底）
- 无 failures → 回退 regex
- 空 failures → UNKNOWN
- failures 无 failed_col → 回退 regex

### 验证

```
python -m pytest server/tests/test_run_set_failures_o20c.py \
               server/tests/test_error_classifier.py \
               server/tests/test_verify_repair_loop.py -q
=> 34 passed   # O20c 5 + classifier + repair

python -m pytest server/tests/ -q
=> 994 passed, 1 预存红（同 O14-O19，无新增回归）
```

## e2e 验证（S2 回归确认 O20a-c 无副作用）

S2 幽冥宗（O19 str+list 修复样例）重跑：ok=False stages=5 正常失败上报（"school/解锁等级列名不存在" + "已试策略：轮1:column_not_found/column_candidate_remap"）— O20a classifier regex + O20c classify 结构化均生效，O19 修复未回归。

## 残留 follow-up

- **O20d 覆盖度**（§4）：S1/S3/S4 仍需 LocatorAgent `_expand_by_fk` 多跳 + DecomposeAgent 全链兜底 + 占位符入 failures。含 LLM 能力缺口（单表漏拆），G3 few-shot RAG 长期解。
- **S1 DecomposeAgent 6 条重复**：fields 含不同占位符导致 _dedup_intents 不去重。需 DecomposeAgent prompt 约束或 Step5 层按 (stem, sheet) 去重（放宽 fields sig）。
- **S4 parser 崩**：`codemaker_parser._parse_via_llm` 返 None（S4 长 LLM 空响应），非 O20a-c 引入，属 parser LLM 能力缺口。

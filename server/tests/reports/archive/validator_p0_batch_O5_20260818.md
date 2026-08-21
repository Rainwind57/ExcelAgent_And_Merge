# O5 validator P0 第一批 — 确定性单测报告

> 轮次：O5（2026-08-18）
> 范围：validator P0 第一批（P10/P11/P12/P17），见 `docs/OPTIMIZATION_LEDGER.md` §1 + §4 + `docs/validator_audit.md` §3。
> LLM e2e（`table_case_eval.py`）被 R7 阻断（ledger §2.1：codemaker serve agentic LLM 慢/贵，非 excel-agent code 可修），本轮证据为确定性单测。

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| P12 | `server/agent/excel/core/produces_inference.py:57` `_field_matches_col` | 子串 `fk in k or k in fk` → **精确等值 only**（点分键取末段 + 归一后 ==）。审计「+后缀」公式 `k.endswith("_"+fk)` 仍让 `model_id` 命中 `id`（model_id 以 `_id` 结尾），升级为精确 only。 |
| P12 | `server/agent/excel/subagent/validator_agent.py:692` `_field_matches_fk` | 同上对齐（opt-in `_validate_forward_refs_llm` 路径，默认 off）。 |
| P17 | `server/agent/excel/core/produces_inference.py:73` `_should_consume` | `<auto>` 从 True 改为 False（`<auto>` 留空不转 producer 占位，消 `_phase_execute` placeholder_unresolved 二次 ask）。 |
| P10 | `server/agent/excel/core/produces_inference.py:121` `add_keys` | `add_keys[key] = i` 覆盖 → `setdefault(key, i)`（保留首 producer 候选，与 `_suppress_over_produce`「一表一 op 契约」语义一致）。 |
| P11 | `server/agent/excel/core/produces_inference.py:138` produces 标签 | `new_{stem}_id` → **sheet-aware** `new_{stem}_{sheet}_id`（sheet 缺省回退 stem 级）。消同 stem 多 producer 撞标签。注：4-step 主线 `validate_two_layer` 不调 align，sheet-aware 不被折叠；fallback `validate()` 仍折叠（保留旧行为）。 |
| 测试 | `server/tests/test_produces_inference_p0.py`（新增） | 21 测覆盖 P10/P11/P12/P17 单元 + 端到效。 |

## 确定性验证

```
python -m pytest server/tests/test_produces_inference_p0.py -v
=> 21 passed in 1.88s

python -m pytest server/tests/test_validate_agent_two_layer.py \
               server/tests/test_parse_agent.py \
               server/tests/test_decompose_agent.py \
               server/tests/test_produces_inference_p0.py \
               server/tests/test_multi_table_orchestration.py \
               server/tests/test_execute_agent.py \
               server/tests/test_subagent_roles.py -q
=> 137 passed, 9 failed in 26.39s
```

9 fail 全为 `test_decompose_agent.py` 预存红（DecomposeAgent mock 路径 `decompose` 返 0，基线即红，与本批无关）。零新红。

## 量化

| 指标 | before | after | delta |
|---|---|---|---|
| produces_inference 直接单测 | 0（无） | 21 | +21（新增覆盖） |
| 相关回归 PASS | 137（含预存 9 红） | 137（含预存 9 红） | 0（零新红） |
| `id` 命中 `model_id` 假阳性 | 是 | 否 | 消除 |
| `<auto>` 误转占位 → 二次 ask | 是 | 否 | 消除 |
| 同 stem 多 producer 撞标签 | 是 | 否（sheet-aware） | 消除 |
| add_keys 同 key 覆盖丢 producer | 是 | 否（setdefault） | 消除 |

## 根因归因

| 修复项 | 贡献 | 证据 |
|---|---|---|
| P12 精确 only | 消 produces_inference producer PK 提取误收 `model_id` + forward_ref LLM 假阳性主源 | `test_id_not_match_model_id` + `test_non_pk_id_field_not_used_as_substitute` |
| P17 `<auto>` False | 消可选列误转占位触发 placeholder_unresolved 二次 ask | `test_auto_not_consume` + `test_auto_fk_value_stays_auto` |
| P11 sheet-aware | 消同 stem 多 producer（entity_prefab candidate/formal）撞 produced 字典后写覆盖 | `test_same_stem_different_sheet_distinct_labels` |
| P10 setdefault | 消 add_keys 同 (stem,sheet) 后写覆盖丢首 producer | `test_same_stem_sheet_first_producer_wins` |

## 注意事项

- P12 升级为「精确 only」超出审计「精确+后缀」建议：审计公式 `k.endswith("_"+fk)` 仍让 `model_id` 命中 `id`，未达消假阳性目标，故升级。已在 ledger + audit 文档标注决策。
- P11 sheet-aware 仅在 4-step 主线（`validate_two_layer` 不调 align）生效；fallback `validate()` 路径 align 会折叠为 stem 级（保留旧行为，非主线）。多 producer 列表完整扩展留 follow-up。
- P9 未动：4-step `validate_two_layer` 不调 `_suppress_over_produce`，仅 fallback `validate()` 触发，降级优先级。
- LLM e2e `table_case_eval.py` 未跑：R7 阻断（serve agentic LLM 慢/贵，非 excel-agent code 可修，ledger §2.1）。
- `test_decompose_agent.py` 9 预存红：DecomposeAgent mock 路径 `decompose` 返 0，与本批无关，留待 DecomposeAgent 专项修复。

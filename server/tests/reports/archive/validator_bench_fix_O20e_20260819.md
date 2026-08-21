# O20e S1 重复写入根治（O20b follow-up）

> 来源：`docs/bench_failure_fix_requirements.md` §2 残留 follow-up + ledger O20b 行。
> 日期：2026-08-19。
> 状态：代码完成（5 单测绿，全量 1011 passed / 1 预存红零回归），e2e 阻 R7。

---

## 0. O20b 残留根因

O20b `_dedup_intents` 已实现按 (stem, sheet, action, locator, fields_hash) 去重，但 S1 实跑仍 6 条重复：

- **根因链**：S1 有 6 候选表（quest/combat/reward/item/entity_prefab/interaction），DecomposeAgent 每候选表单 prompt 并发，LLM 对每个候选表 prompt 都可能顺带产一条 Quest intent（Quest 作为 producer 被 FK 链多端引用）。
- 6 条 Quest intent 的 consumes 引用不同 producer label（`<new_combat_id>` / `<new_reward_id>` / `<new_item_id>` 等），导致 `fields` 占位符值不同。
- `_dedup_intents` 的 `fields_sig` 用 `json.dumps(fields)` 直接 hash，占位符值不同 → sig 不同 → 不去重。

---

## 1. 双保险修复

### 1.1 `validator_agent.py:_dedup_intents` 占位符归一
- fields_sig 计算前把 `<...>` 占位符值归一为 `<ph>`（正则 `_PH_RE = re.compile(r"<[^>]+>")` sub）。
- 消除跨候选 prompt 产同表 intent 因 consumes 引用不同 producer label 的假性差异。
- 占位符差异非真实业务差异（仅 LLM 对 consumes 的不同引用），去重应忽略。
- 真实字段值差异（如 state=idle/collect 不含 `<...>`）不受影响保留。

### 1.2 `decompose_agent.py:_build_prompt` 强约束
- prompt 加规则："同一表同一 sheet 只产一条主配置，不要重复产多条相同 op；若指令对同一表有多个不同操作（如 idle/collect 多状态行），按真实业务子任务产多条且 fields 各自不同，不要因 consumes 占位符不同而重复产同配置"。
- 双保险：prompt 约束 LLM 不过产 + `_dedup_intents` 兜底去重残留。

---

## 2. 测试（`tests/test_dedup_intents_o20b.py` TestDedupPlaceholderNormalizationO20e 5）

| 测试 | 场景 | 期望 |
|---|---|---|
| test_placeholder_only_diff_dedup | fields 仅占位符值不同（`<new_combat_id>` vs `<new_reward_id>` vs `<new_item_id>`） | 归一为 `<ph>` 后去重留 1（去 2） |
| test_real_field_diff_not_dedup | fields 真实值不同（state=idle/collect） | 不归一非占位符值，保留 2 条 |
| test_mixed_placeholder_and_real_diff_kept | 占位符不同 + 真实字段不同（类型=主线/支线） | 纯占位符差异去重，真实差异保留（留 2） |
| test_multiple_placeholder_fields_normalized | 多占位符字段（战斗id + 奖励id）各归一为 `<ph>` | 组合相同去重留 1 |
| test_non_string_field_not_normalized_kept | 非字符串字段（数字 id=1/2）不受归一影响 | id 真实不同 → 不去重保留 2 |

---

## 3. 确定性验证

| 测试文件 | 测数 | 覆盖 | 结果 |
|---|---|---|---|
| `tests/test_dedup_intents_o20b.py` | 13 | 原 O20b 8 + O20e 5 | 13/13 passed |
| 相关回归（decompose/parse_agent/coverage_o20d/tips_to_failures/unified_entry） | 71 | validator/agent 链路 | 71/71 passed |
| 全量回归 | 1011 | 全仓库 | 1011/1011 passed（1 预存红 `test_column_matcher_semantic`，1 skipped） |

**零回归**：1 预存红 `test_column_matcher_semantic`（O5-O20d 持续存在，与所有 O 改动无关）。

---

## 4. 残留 follow-up

1. **LLM 能力缺口**（§4.4）：单表漏拆子任务，G3 few-shot RAG 注入 DecomposeAgent prompt 长期解。
2. **S4 parser 崩**（独立项）：`codemaker_parser._parse_via_llm` 返 None（S4 长 LLM 空响应），需加 fallback/重试/降级。
3. **e2e 验证**（阻 R7）：serve:8666 + backend:8000 未在线，O20e e2e 指标待 serve 起后跑 S1 验证 6 条→1 条。

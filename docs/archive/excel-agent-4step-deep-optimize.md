# 4-Step Loop 深度优化记录

> 接续 `excel-agent-4step-loop-design.md` / `excel-agent-4step-loop-task.md` 的 4-Step 改造。
> 本文档记录从"4-Step 默认开但单表意图走 6 步 fast-path、stage 不全、execute 失败"到
> "4-Step 全链路 stage 1/4 全显 + 单表样例执行成功"的深度优化过程，含基准指标。

## 0. 起点状态

改造前 smoke（`dry_run` 简单单表意图「新增灵兽朱雀」）暴露 4 个代码层问题：

| # | 问题 | 性质 | 影响 |
|---|------|------|------|
| 1 | `agent.excel.tools` import 失败致 verify-repair 崩 | 阻塞 bug | 链路异常，done event error |
| 2 | 4-Step 单表意图 stage 只显 s1_parse + summary | stage 映射 | s2/s3/s4 不显现 |
| 3 | verify 门控 anti_pattern 误判 column_not_found | 门控 bug | 正确写操作被判失败 |
| 4 | sheet 定位落到「灵兽表说明」非「Pet」 | 定位 bug | 列名匹配全失败 |

R7（codemaker serve 143.8k token/156s 卡死）是 serve 端 auto-context bug，非代码层，跳过。

## 1. 优化 #1：修复 `agent.excel.tools` import 路径错

**根因**：`server/agent/excel/core/agent.py:6070` `_run_react_repair` 内 `from ..tools import make_skill_tools`。
- `core/agent.py` 在 `server/agent/excel/core/`，`..` = `server/agent/excel/`
- `from ..tools` 解析成 `server/agent/excel/tools`——**不存在**
- 实际 `tools.py` 在 `server/agent/tools.py` = `server.agent.tools`

**修复**：`from ..tools` → `from ...tools`（上两级 `server/agent/` + `tools`）。

**验证**：单测 156 passed 零回归；smoke `No module named 'agent.excel.tools'` 异常消除，done event 正常返回。

## 2. 优化 #2：修复 verify 门控 anti_pattern 误判

**根因**：`server/agent/excel/core/agent.py:5726-5760` verify 门控对**所有命中 active 反模式**的列判 `COLUMN_NOT_FOUND` 失败。
- 反模式 `ap_amb_pet_Pet_名称`（ambiguous_column, force_exact）是「该列曾歧义，定位强制精确」的提示
- 但 verify 阶段已有具体 col_name（列已定位成功），不该判写操作失败
- `:5728` `action in ("force_exact", "block_dry_run")` 把 ambiguous_column 也判失败
- `:5759-5760` `elif anti_pattern_hits: failed_kind=COLUMN_NOT_FOUND` 映射错（反模式命中 ≠ 列不存在）

**修复**（2 处）：
1. `:5728` anti_pattern_hits 记录条件：`action in ("force_exact","block_dry_run")` → 只 `"block_dry_run"`（ambiguous_column 降级不阻断）
2. `:5759-5760` 删除 `elif anti_pattern_hits: failed_kind=COLUMN_NOT_FOUND` 映射

anti_pattern_hits 仍进 VerifyResult 作 warning signal 供 repair 参考，但不阻断 verify。

**验证**：单测 171 passed；smoke 从 `ok:false "verify 门控失败 column_not_found"` → `ok:true "已新增 1 行"`。

## 3. 优化 #3：修复 dry_run 模式 4-Step stage 不显现

**根因**：`server/services/agent_service.py:2552` `_dry_run_chat` 用 tmp_agent（新构造 TableAgent），**没注入流式 sink**。
- 4-Step ParseAgent/ValidateAgent/ExecuteAgent/ConcludeAgent 的 thinking 不进 SSE queue
- `_gen` 的 `_stage_for_thinking` 收不到 phase="校验"/"执行"/"汇总"，stage s2/s3/s4 不开
- 附带：tmp_agent 用主 agent 的 `_validator_agent`（共享实例），其 `_ask_callback` 是交互回调，dry_run 非交互下 `ask_user` 阻塞等 reply_queue → 死锁

**修复**（`_dry_run_chat` tmp_agent 构造后）：
1. 复用主 agent 的 `_agent_thinking_sink/_agent_step_sink/_agent_tool_sink/_agent_subtask_sink/_cancel_event`
2. 复用主 agent 的 `_validator_agent`
3. **ask_callback 死锁修复**：注入立即返 `{"mode":"skip"}` 的 ask_callback；临时覆盖共享 validator 的 ask_callback 为 skip（`run` 后 `finally` 恢复原值）

**验证**：单测 115 passed；smoke stage 从「只 s1_parse + summary」→「**s1_parse → s2_validate → s3_execute → s4_summary → summary 完整 4/4**」。

## 4. 优化 #4：修复 sheet 定位落到说明 sheet

**根因**：`server/agent/excel/core/agent.py:1070-1072` `_resolve_sheet` 策略1 无 `_is_business_sheet` 校验。
```python
if intent.sheet_hint and intent.sheet_hint in sheets:
    return intent.sheet_hint  # 无业务校验
```
- LLM DecomposeAgent prompt 不给完整 sheet 名清单，LLM 可能猜成「灵兽表说明」（raw 含「灵兽」「表」）
- sheet_hint 原样传播（`_to_split_intents`→`_split_to_nl` 无校验）
- 策略1 无条件采纳 in-sheets 的 sheet_hint → 落说明 sheet（表头是 col_1/col_2，列名全不匹配）

**修复**：策略1 补 `_is_business_sheet` 校验：
```python
if intent.sheet_hint and intent.sheet_hint in sheets \
        and _is_business_sheet(intent.sheet_hint):
    return intent.sheet_hint
```

**验证**：单测 115 passed；smoke 从 `add pet/灵兽表说明` → `add pet/Pet`，列名匹配成功（成长率→col 20）。

## 5. 基准测试：6 样例指标

测试脚本 `bench_4step.py`，串行 `dry_run`，采集 stage 数/ok/墙钟/result_table/失败数。

> 注：原 6 设计样例（封印魔龙跨6表等）会触发 R7 serve hang（143.8k token/156s），非代码层 bug。
> 基准用可完成的单表/中等样例验证 4-Step 链路完整性。

### v1 指标（4 项优化后）

| # | 样例 | 复杂度 | 墙钟(s) | stage | ok | result_table | 错误/摘要 |
|---|------|--------|---------|-------|----|--------------|----------|
| 1 | 新增灵兽朱雀（单表add） | 低 | 29.9 | 5/5 | ✓ | pet/Pet row=54 cols=3 | 执行成功，名称/成长率已写入 |
| 2 | 查询饕餮属性（单表get） | 低 | 20.6 | 5/5 | ✗ | — | pet/Pet 未找到「饕餮」（业务合理，有相近行） |
| 3 | 改灵兽资质（单表modify） | 低 | 25.4 | 5/5 | ✗ | — | "未知错误类型 retry 兜底" |
| 4 | 新增瞭望塔（复合语句add） | 中 | 33.5 | 5/5 | ✓ | building/BuildingType row=28 cols=3 | 执行成功，代词消解+复合语句 |
| 5 | 删除测试兽（单表delete） | 低 | 14.3 | 5/5 | ✗ | — | "combat/combat_data 未知错误类型"（定位错） |
| 6 | 灵兽相关表（QA问答） | 低 | 9.2 | 2/1 | ✓ | — | QA 答案正确（4 张灵兽表） |

**指标小结**：
- **stage 完整性**：4/5 CRUD 样例 stage 全显（5/5: s1_parse→s2_validate→s3_execute→s4_summary→summary），QA 样例 2 stage（s1_parse→summary，符合设计）
- **成功率**：3/6（样例1/4 add 成功，样例6 QA 成功）
- **墙钟**：9-34s（单表样例，无 R7 hang）
- **LLM 调用**：0（dry_run 走规则短路 + ParseAgent LLM 拆分，但心跳计数未同步，待修）

### 失败样例归因

| 样例 | 失败原因 | 性质 | 下一步 |
|------|----------|------|--------|
| 2 查询饕餮 | pet/Pet 无「饕餮」精确行，有「饕餮一/二/三阶」 | 业务合理 | 非 bug，可优化模糊匹配提示 |
| 3 改资质 | "未知错误类型，走通用 error_feedback retry 兜底" | 兜底分类 | error_classifier 未识别的失败类型，待加固 |
| 5 删测试兽 | 定位到 combat/combat_data（非 pet） | LocatorAgent 定位错 | "测试兽"关键词命中 combat，待修关键词权重 |

## 6. 剩余问题与下一步

### 代码层（可修）
1. **error_classifier 兜底**（样例3/5）："未知错误类型 retry 兜底" 未识别具体失败类型，需补充 classify 分支
2. **LocatorAgent 关键词权重**（样例5）："测试兽" 误定位 combat，需降权非业务关键词
3. **「灵兽品质」int 枚举列**：LLM 产 "神兽" 字符串无枚举 ID 映射，type_coerce 失败（需 enum_mappings 补全或 prompt 约束 LLM 用枚举值）
4. **LLM 调用计数未同步**：dry_run 走 tmp_agent，`_llm_counter.peek_total()` 始终 0（心跳显示），计数器需随 sink 一起注入

### serve 层（R7，非代码 bug）
5. **复杂跨表样例 R7 hang**：封印魔龙等 6 设计样例触发 serve 143.8k token/156s 卡死（auto-context 固定行为），待 serve 侧根治

### 已确认非 bug
- column_matcher 本身正常（sheet 正确时列名能匹配）
- 4-Step ParseAgent→DecomposeAgent→validate_two_layer 链路通（单表意图产出+校验通过）

## 7. 文件变更清单

| 文件 | 改动 | 优化项 |
|------|------|--------|
| `server/agent/excel/core/agent.py:6070` | `from ..tools` → `from ...tools` | #1 |
| `server/agent/excel/core/agent.py:5728` | anti_pattern 阻断条件收窄为 `block_dry_run` | #2 |
| `server/agent/excel/core/agent.py:5759` | 删除 anti_pattern→COLUMN_NOT_FOUND 映射 | #2 |
| `server/services/agent_service.py:2606-2643` | _dry_run_chat tmp_agent 注入 sink + skip ask_callback | #3 |
| `server/agent/excel/core/agent.py:1071` | _resolve_sheet 策略1 加 _is_business_sheet | #4 |
| `bench_4step.py` | 新增 6 样例基准测试脚本 | 基准 |

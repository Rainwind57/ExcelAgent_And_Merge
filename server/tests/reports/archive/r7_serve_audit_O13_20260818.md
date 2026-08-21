# R7 codemaker serve agentic LLM — 审计 + 诊断

> 轮次：O13（2026-08-18）
> 范围：R7 serve 根治**审计**。R7 根因在 codemaker serve 侧（非 excel-agent 仓库可修），本轮审计仓库侧缓解是否完整在位 + 写诊断探针 + 文档化 serve 侧修复需求。见 `docs/OPTIMIZATION_LEDGER.md` §2.1 P0。

## 1. R7 根因（serve 侧，非本仓库）

codemaker serve 对 `/session/{id}/message` 端点做 **auto-context-grounding**：自动读项目 xlsx 文件作上下文。excel-agent 发送 ~9.6-20k token 聚焦 prompt 后，serve 内部膨胀到 **143.8k token + 90-180s 超时返空**。

证据（`docs/archive/excel-agent-diagnosis.md` §7.4）：
- excel-agent 这边 prompt 仅 ~9.6k token，多出的 13 万 token 来自 codemaker serve 自带 agent。
- `_call_llm_raw` 对 `/session/{id}/message` 发送聚焦 prompt 后，serve 180s 仍返回空（raw_len=0）→ decompose 返回 [] → 规则模板兜底。
- 对照实验锁定：非 excel-agent 代码 bug，是 serve 端固定行为。

## 2. 仓库侧缓解审计（全部在位）

| 缓解 | 位置 | 状态 |
|---|---|---|
| session 隔离（空临时目录，避免 serve 用 project 目录作隐式 file context） | `subagent/base.py:35 _isolated_empty_dir` + `:154 _ensure_own_session`（`CODEMAKER_SUBAGENT_ISOLATE_CONTEXT=1` 默认 on） | ✅ 在位 |
| StepAIEnhancer session 隔离 | `step_ai_enhancer.py:121 _ensure_session`（`CODEMAKER_AI_ENHANCER_ISOLATE_CONTEXT=1` 默认 on，#26） | ✅ 在位 |
| DecomposeAgent per-candidate 隔离 session | `decompose_agent.py:119`（`directory=_isolated_empty_dir()`） | ✅ 在位 |
| DecomposeAgent 超时可配 | `decompose_agent.py:70`（`CODEMAKER_DECOMPOSE_TIMEOUT=45`） | ✅ 在位 |
| parse_multi 超时可配（单次长超时代替多次短超时） | `codemaker_parser.py:783`（`CODEMAKER_PARSE_MULTI_TIMEOUT=90`） | ✅ 在位 |
| 熔断（连续失败跳 LLM 降级规则） | `codemaker_parser.py:349 _circuit_threshold`（`CODEMAKER_PARSE_CIRCUIT_THRESHOLD=3`） | ✅ 在位 |
| scoped-decompose（per-candidate prompt，缩小单 LLM 调用范围） | `decompose_agent.py:77 jobs` per-candidate | ✅ 在位 |
| LocatorAgent 复杂输入扩表 + 跳 LLM 收敛 | `locator_agent.py _is_complex_input + _expand_by_fk` | ✅ 在位 |
| 规则模板兜底（LLM 失败/空 → cross_table_splitter） | `agent.py` fallback | ✅ 在位 |

**结论**：excel-agent 仓库侧能做的 R7 缓解**全部已落地**。session 隔离避免 serve 读项目目录、scoped-decompose 缩小单 prompt 范围、超时可配、熔断 + 规则兜底保降级。无更多仓库侧优化空间。

## 3. serve 侧根治需求（外部代码库）

R7 根治需 codemaker serve 改动（非 excel-agent 仓库）：

1. **关 auto-context-grounding**：`/session/{id}/message` 端点对 prompt 文本出现的表 stem 不自动读 xlsx 文件作上下文。或为 excel-agent 这类调用提供「无 auto-context」会话模式。
2. **提供纯文本补全端点**：非 agentic 通道，纯 LLM 补全（无文件工具循环），供 excel-agent DecomposeAgent/parse_multi 用。
3. **serve 日志排查**：为何读 xlsx 后返回空（可能 xlsx 过大/解析炸 + 工具循环超时）。

## 4. 诊断探针（`server/tests/r7_serve_probe.py`）

**用途**：serve 可达时表征 R7 + 修复后验证。

**机制**：
- 发送极小聚焦 prompt（~50 字符，无表 stem，避免触发 auto-context 读 xlsx）
- 测墙钟 + 响应是否空
- 判定：healthy（<30s 有响应）/ R7-suspected（30-90s 或响应短）/ R7-confirmed（>90s 或空响应）/ serve 不可达

**运行**：
```
cd server && set CODEMAKER_R7_PROBE=1 && python -m tests.r7_serve_probe
# 退出码: 0=healthy / 1=suspected / 2=confirmed / 3=不可达 / 4=未启用
```

**当前环境结果**：serve 不可达（exit 3）—— 无 codemaker serve 运行，探针机械工作正常（import + health_check + 判定逻辑验证通过）。

## 5. 后续

- R7 根治 = serve 侧改动（外部）。excel-agent 仓库侧无更多可做。
- serve 修复后：跑 `r7_serve_probe.py` 验证 healthy（exit 0）→ 跑 `table_case_eval.py` LLM e2e 验证 O5-O12 改动真实收益（locate/cov/acc/pass/elapsed）+ 跨链样例 5-7。
- R7 未解前：LLM e2e + 跨链样例 5-7 持续阻断，excel-agent 优化以确定性单测为证据。

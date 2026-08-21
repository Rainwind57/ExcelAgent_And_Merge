# O20f S4 parser 崩修复（_dry_run_chat parse 降级 parse_multi）

> 来源：`docs/bench_failure_fix_requirements.md` §0 S4 "parser 崩"（独立项，非 O20 引入）。
> 日期：2026-08-19。
> 状态：代码完成（4 单测绿，全量 1015 passed / 1 预存红零回归），e2e 阻 R7。

---

## 0. 根因定位

S4 v2（O20a alias 修复后）done_message：
```
抱歉，处理您的请求时出错：codemaker 解析失败：'新增一个限时活动叫"万圣狂欢"...'
```
stages_seq = `[s1_parse, summary]`（崩在 s1_parse 阶段）。

**调用链定位**：
1. bench `dry_run=True` → `AgentService._dry_run_chat`（1717）
2. `_dry_run_chat` 2586 `self.agent.parser.parse(text)` 单意图解析
3. S4 万圣狂欢 6 表 add+modify 混合指令单 parse 易超时/空响应 → `_parse_via_llm` 返 None
4. `codemaker_parser.parse` 408-412 `raise RuntimeError(f"codemaker 解析失败：{text!r}")`
5. 2587 catch → `_map_codemaker_error` → "抱歉，处理您的请求时出错：codemaker 解析失败：..." → 返回错误响应

**根因**：`_dry_run_chat` 2586 单次 `parse` 无降级，复杂跨表指令单意图解析失败直接崩。

---

## 1. 修复

`agent_service.py:_dry_run_chat` 2586 `parse` 失败时降级 `parse_multi`：

```python
try:
    intent = self.agent.parser.parse(text)
except Exception as e:
    # O20f：parse 单意图失败时降级 parse_multi（复杂跨表指令更健壮）
    pm_intents = []
    if hasattr(self.agent.parser, "parse_multi"):
        try:
            pm_intents = self.agent.parser.parse_multi(text) or []
        except Exception:
            pm_intents = []
    if pm_intents:
        intent = pm_intents[0]
    else:
        # 仍无 intent 才返回错误（真正 LLM 不可用）
        err_type = getattr(e, "error_type", "") or self._infer_error_type(str(e))
        _, advice = self._map_codemaker_error(err_type, str(e))
        return AgentChatResponse(ok=False, ..., message=advice, error=str(e))
```

**设计**：
- `parse_multi` 复杂跨表指令多意图解析更健壮（含规则快速路径 `split_multi_intent` + LLM 多意图），失败返空 list 不 raise（5.7/6.3 已处理）。
- 取首条 intent 作为定位 intent（供 2596 `_resolve_table` 用）。
- `parse_multi` 也空才返回错误响应（真正 LLM 不可用，保 D6）。
- `parse_multi` raise 时 try/except 当作空处理（不二次崩）。

---

## 2. 测试（`tests/test_dry_run_parse_fallback_o20f.py` 4）

| 测试 | 场景 | 期望 |
|---|---|---|
| test_parse_fails_fallback_to_parse_multi_success | parse raise + parse_multi 返 2 intent | 降级取首条，不返回 parse 错误（进了 _resolve_table） |
| test_parse_fails_parse_multi_empty_returns_error | parse raise + parse_multi 返空 | 返回错误响应（ok=False） |
| test_parse_multi_no_attribute_still_returns_error | parser 无 parse_multi（spec 限制） | 跳过降级，返回错误响应 |
| test_parse_multi_raises_handled_as_empty | parse_multi raise | 当作空处理，返回错误响应（不二次崩） |

**Mock 设计**：
- `_StubParser`：`parse` raise（模拟超时/空响应），`parse_multi` 返预设 intent 列表，跟踪调用计数。
- `_make_service_stub`：`object.__new__(AgentService)` + mock `agent`/`router`/`_resolve_table`，跳过重对象构造。
- test 3 用 `MagicMock(spec=["parse", "_last_error_type"])` 限制无 `parse_multi` 属性，验证 `hasattr` 分支。

---

## 3. 确定性验证

| 测试文件 | 测数 | 覆盖 | 结果 |
|---|---|---|---|
| `tests/test_dry_run_parse_fallback_o20f.py` | 4 | parse 降级 parse_multi 全路径 | 4/4 passed |
| 全量回归 | 1015 | 全仓库 | 1015/1015 passed（1 预存红 `test_column_matcher_semantic`，1 skipped） |

**零回归**：1 预存红 `test_column_matcher_semantic`（O5-O20e 持续存在，与所有 O 改动无关）。

---

## 4. 残留 follow-up

1. **S4 实跑验证**（阻 R7）：serve:8666 + backend:8000 未在线，O20f 降级链待 serve 起后跑 S4 确认不再崩在 s1_parse（stages_seq 应 ≥3 stages）。
2. **LLM 能力缺口**（§4.4）：单表漏拆子任务，G3 few-shot RAG 长期解。
3. **parse 本身重试/降级**（长期）：`codemaker_parser.parse` 当前 raise 由调用方 catch，O20f 在 `_dry_run_chat` 层降级；其他调用点（TableAgent.run 内）已有 try/except + fallback，无需改 parser 层。

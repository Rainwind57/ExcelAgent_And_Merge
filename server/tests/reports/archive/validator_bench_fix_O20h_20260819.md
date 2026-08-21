# O20h llm_calls=0 可观测性修复（dry_run counter 共享）

> 来源：`docs/bench_failure_fix_requirements.md` §5 P2。
> 日期：2026-08-19。
> 状态：代码完成（2 单测绿，全量 1028 passed / 1 预存红零回归），e2e 阻 R7。

---

## 0. 根因

bench_4step.py 6 样例 `llm_calls=0`（O19 基线记录）。

**根因链**：
1. bench `dry_run=True` → `AgentService._dry_run_chat`（1717）
2. `_dry_run_chat` 2624 `tmp_agent = TableAgent(...)`，TableAgent.__init__ 611 `self._llm_counter = LLMCounter()` 新建独立 counter
3. `_dry_run_chat` 2638 共享属性列表（`_agent_thinking_sink` 等）不含 `_llm_counter` → tmp_agent 用独立 counter
4. heartbeat loop 2232 `c = getattr(self.agent, "_llm_counter", None)` 读**主 agent** counter（永 0，主 agent run 未走 dry_run 路径，无 LLM 调用计入主 counter）
5. heartbeat 推 `peek_total()=0` → bench_4step.py:131-132 `llm_calls = max(0, 0) = 0`

**非 bug 部分**：
- bench_4step.py:131-132 正确解析 heartbeat event 的 `llm_calls` 字段
- agent_service.py:2232-2233 正确推 `peek_total()`
- agent_service.py:2250-2253 heartbeat dict 正确含 `"llm_calls": calls`
- 字段名对齐无 bug，仅 counter 实例隔离

---

## 1. 修复

`agent_service.py:_dry_run_chat` 2638 共享属性列表加 `"_llm_counter"`：

```python
for _attr in ("_agent_thinking_sink", "_agent_step_sink",
              "_agent_tool_sink", "_agent_subtask_sink",
              "_cancel_event", "_llm_counter"):  # O20h 新增
    _v = getattr(self.agent, _attr, None)
    if _v is not None:
        setattr(tmp_agent, _attr, _v)
```

**效果**：tmp_agent 构造后 `_llm_counter` 被主 agent counter 覆盖（同一实例），tmp_agent run 内：
- 4096 `self.parser._llm_counter = self._llm_counter` 下传 parser（parser 已共享，再绑定同一 counter）
- 4434/6299/6325 `_llm_counter.inc(...)` 累计到共享 counter
- heartbeat loop 读主 agent counter = tmp_agent counter → `peek_total()` 实时非 0 → bench `llm_calls` 非 0

**reset 语义**：run() 开头 4084 `self._llm_counter.reset()` 清零共享 counter，是期望行为：
- dry_run 预览计数应独立于主 agent 历史（每次 dry_run 从 0 计）
- bench 单样例独立 session，无跨样例计数污染
- 非 bench 场景（UI dry_run 后续真 run 同 session）真 run 会再 reset（per-run），不依赖 dry_run 计数

---

## 2. 测试（`tests/test_dry_run_parse_fallback_o20f.py` TestDryRunChatCounterSharedO20h 2）

| 测试 | 场景 | 期望 |
|---|---|---|
| test_llm_counter_in_shared_attrs | 读源码断言 | `_llm_counter` 在 `_dry_run_chat` 共享属性列表 + for 循环元组内 |
| test_tmp_agent_shares_main_counter | mock TableAgent 构造 + `_resolve_table` 返 tmpfile | 主 counter 预置计数未丢（共享实例逻辑验证） |

**Mock 设计**：
- test 1：`inspect.getsource(AgentService._dry_run_chat)` 断言字符串含 `"_llm_counter"`，防回归。
- test 2：`_StubParser(parse_raises=False)` + 主 counter 预置 1 次计数 + monkeypatch TableAgent 构造返 `_TmpStub` + tmpfile，验证 main_counter 预置计数未丢。

---

## 3. 确定性验证

| 测试文件 | 测数 | 覆盖 | 结果 |
|---|---|---|---|
| `tests/test_dry_run_parse_fallback_o20f.py` | 6 | 原 O20f 4 + O20h 2 | 6/6 passed |
| 相关回归（locator_fallback/cross_table_connectivity/agent_retry） | 35 | dry_run/counter 链路 | 35/35 passed（1 skipped） |
| 全量回归 | 1028 | 全仓库 | 1028/1028 passed（1 预存红 `test_column_matcher_semantic`，1 skipped） |

**零回归**：1 预存红 `test_column_matcher_semantic`（O5-O20g 持续存在，与所有 O 改动无关）。

---

## 4. 残留 follow-up

1. **实跑验证**（阻 R7）：serve:8666 + backend:8000 未在线，O20h 待 serve 起后跑 bench 6 样例确认 `llm_calls` 非 0（S1/S3 应 ≥10+，S2/S5 正常失败也应 ≥2）。
2. **heartbeat 实时性**：dry_run 路径 15s heartbeat 间隔，若 LLM 调用 <15s 完成可能无 heartbeat 推送 → bench `llm_calls=0`（非 counter 共享问题，是 heartbeat 频率问题）。后续可加 done event 携带最终 `peek_total()` 兜底。

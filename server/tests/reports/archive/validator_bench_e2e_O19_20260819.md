# O19 bench e2e 验证 + str+list 崩溃修复

> 轮次：O19（2026-08-19）
> 范围：R7 解封后首次真 LLM e2e 跑通 6 样例（bench_4step.py）+ 发现并修复 str+list 硬崩 bug。
> 前置：codemaker serve (:8666) + backend uvicorn (:8000) 起着，serve 可用（session 创建成功，projectID=global）。

## 执行

起 codemaker serve + backend uvicorn，跑 `bench_4step.py` 全 6 样例（封印魔龙/幽冥宗/九尾天狐/万圣狂欢/聚灵塔/复合修改）。首次 R7 解封后真 LLM e2e，archive R7 阻断记录过时。

## e2e 基线指标（6 样例）

| # | 样例 | ok | 墙钟s | stages | result_table | 关键问题 |
|---|---|---|---|---|---|---|
| 1 | 封印魔龙 | ✅ | 237 | 5 | quest row23 | 部分完成：只写 quest 2/8 表，Quest 18-23 行 6 条重复写入 |
| 2 | 幽冥宗 | ❌→✅修复 | 45→278 | 4→5 | - | **str+list 崩** → 修复后正常失败（school/解锁等级列不存在） |
| 3 | 九尾天狐 | ✅ | 118 | 5 | pet row54 | 部分完成：只写 pet 1/4 表 |
| 4 | 万圣狂欢 | ❌ | 256 | 2 | - | DecomposeAgent **错表**：活动指令定位到 residence_building |
| 5 | 聚灵塔 | ❌→✅修复 | 248→302 | 5 | residence_building row17 | **str+list 崩** → 修复后部分完成 3/6 + 11 次失败 |
| 6 | 复合修改 | ❌ | 213 | 5 | item/Fabao row5 | 部分成功 2/7（描述✅，权重/技能/删除级联/hero ❌） |

## str+list 崩溃修复

**根因**：`agent.py:6754` `_run_verify_repair_loop`：
```python
"attempted_strategies": ("已尝试：" + rctx.summarized_strategies()  # list!
                         if rctx.summarized_strategies() else "已尝试多轮自动修复"),
```
`rctx.summarized_strategies()` 返 `list[str]`（`repair_context.py:89`），`"已尝试：" + list` 崩 TypeError。

**诊断**：临时在 `agent_service.py` L1878/L2701 except 块加 `traceback.format_exc()` 写 `diag_traceback.log`，复现 S2 抓 stack：
```
File "agent.py:6754" in _run_verify_repair_loop
    "attempted_strategies": ("已尝试：" + rctx.summarized_strategies()
TypeError: can only concatenate str (not "list") to str
```

**修复**（`agent.py:6746` 前定义 `_strategies_str`）：
```python
_strategies_list = rctx.summarized_strategies()
_strategies_str = "已尝试：" + " | ".join(_strategies_list) if _strategies_list else ""
...
"attempted_strategies": (_strategies_str if _strategies_str
                         else "已尝试多轮自动修复"),
```
单次调用缓存（原调两次 summarized_strategies）+ `" | ".join` 拼 str。移除 diag 钩子还原 agent_service.py。

## 验证

```
python -m pytest server/tests/test_agent_p27_checkpoint.py \
               server/tests/test_ad_upgrade_hold_o15.py \
               server/tests/test_id_scope_multi_branch_o16.py \
               server/tests/test_column_lineage_o17.py \
               server/tests/test_rag_bm25_o18.py \
               server/tests/test_execute_agent.py \
               server/tests/test_verify_repair_loop.py -q
=> 86 passed in 9.71s   # verify-repair loop + O14-O18 全绿

python -m pytest server/tests/ -q
=> 981 passed, 1 failed, 1 skipped in 222.35s
   # 1 预存红：test_column_matcher_semantic（同 O14-O18，与 O19 无关）
```

e2e 复跑 S2/S5 确认 str+list 崩消解（从异常中断 → 正常失败上报，stages 4→5 完整）。

## 残留 follow-up（按优先级）

1. **S4 DecomposeAgent 错表**（功能错误，高优先）：用户讲活动/奖励/道具/邮件，agent 定位到 residence_building。需查 DecomposeAgent 拆分逻辑或 LocatorAgent 候选生成。
2. **S1 Quest 重复写入**（数据正确性）：Quest 18-23 行 6 条相同配置，拓扑重跑或 intent 重复产。
3. **覆盖度不足**（S1/S3/S6）：expect 8/4/4 表，实际只写 2/1/2 表。DecomposeAgent 拆分不完整或占位符链断。
4. **llm_calls=0**（可观测性）：heartbeat 未捕 llm_calls 计数，bench SSE 解析或 _llm_counter 对接缺失。
5. **R7 serve auto-context**（archive §5）：复杂样例 200-300s，serve auto-context 固定行为未根治（关 auto-context / 纯文本端点 / serve 日志排查）。

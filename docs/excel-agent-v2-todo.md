# excel-agent V2 4-Step 硬隔离落地 TODO

> 来源：2026-08-21 系统性审查。核心判断：V2 "4 步硬隔离" 目前是**注释级承诺**，
> 不是**代码级承诺**。`contracts.py:11` 自标"过渡期尽力保证，非绝对"是诚实的，
> 但 `step3_execute_subagent.py:9` 的 D4 "严禁 LLM" 用词偏了——执行上并非禁止，
> 而是把 `_phase_partition/plan/validate` 这些 LLM 阶段保留在 Step3 内部未拆除。

---

## 0. 关键口径决策（必须先定，决定 P1 及之后所有修复路径）

**决策**：路 B（全拆开到各 Step）。用户确认 2026-08-21。

**卡点**：`_run_single_impl`（agent.py:5915）这条万用通道仍包含 legacy 5 段全流程
（`_phase_partition` → `_phase_plan` → `_phase_validate` → `_phase_execute` → `_phase_summarize`）。
Step3 SubAgent 调 `_run_single` 实际是 "5-into-1" 合体，而非"只做执行"。

路 B 分阶段推进（S4 级工程，不可一次完成）：
- **阶段 1**（当前）：P0 已完成；P1-3（artifact 收敛）+ P2-2（反模式归纳合一）独立先行。
- **阶段 2**：`_phase_partition` 提取到 Step1（resolve_table/resolve_sheet 与 locator 重叠）。
- **阶段 3**：`_phase_plan` + `_phase_validate` 提取到 Step2（写前 LLM 校验职责唯一）。
- **阶段 4**：`_phase_execute` 留 Step3；`_phase_summarize` 移 Step4。
- **阶段 5**：schema_bundle 的 self 参数依赖解耦 + 6 步降级路径同步 + 全量回归。

**注**：阶段 1 先加 `execute_no_llm` 短路到 `_phase_partition/plan/validate` 入口，
让 P0-2 metrics 在路 B 完成前先收敛到真 0（不阻断后续拆分）。

---

## P0 — 立即修（让 bench 真实可读，否则所有判断建立在假数据上）

### P0-1. `run_v2` 入口 `_llm_counter` 未 reset（7.6）
- **位置**：`agent.py:4448-4449`（run() 内 reset），`run_v2`（agent.py:4342 起）无。
- **后果**：跨请求 counter 累加 → metrics 横向无法对比。
- **修法**：`run_v2` 入口加 `self._llm_counter.reset()`（与 run() 一致）。
- **状态**：[x] 完成（2026-08-21）

### P0-2. Step3 `metrics["llm_calls"]: 0` 硬编码假数据（一.1）
- **位置**：`step3_execute_subagent.py:166`（硬编码 0）。
- **后果**：Step3 实际跑 N×4 LLM 调用，指标报 0 → bench / OPTIMIZATION_METRICS.md 失真。
- **修法**：改 `self._services.peek_llm_total()` 取真实值（services 加 `peek_llm_total` 方法）。
- **状态**：[x] 完成（2026-08-21）。注：路 B 拆分 + execute_no_llm 短路后此值收敛到真 0。

### P0-3. `_last_locator_result` 跨步私有态副作用（三）
- **位置**：`step2_validate_subagent.py:69-70`（探 `_locator_agent._last_locator_result`）。
- **后果**：多 segment 时 locator 只记最后一个，Step2 拿到末段结果；违反 contracts.py:16
  "前一态不被后步改写"硬不变量。
- **修法**：ParseAgent `_parse_segments`/`_parse_whole` 收集全段到 `_last_locator_results`，
  Step1 SubAgent 读写入 `s1.artifacts["locator_results"]`，Step2 改读 artifacts。
  删除 `_assemble` 后的冗余 locate（消除末段覆盖）。
- **状态**：[x] 完成（2026-08-21）

---

## P1 — 兑现 V2 注释承诺（依赖口径决策路 A）

### P1-1. Step3 "路（一.2 + 二）
- **位置**：`_phase_partition`（5962/5997 ai_resolve_table/ai_confirm_table）、
  `_phase_plan`（6053 ai_plan_operation）、`_phase_validate`（6111 ai_validate_plan）、
  `_phase_plan_validate_merged`（6203 ai_pipeline_merge）。
- **后果**：`no_llm` 只短路"写失败后的重试环"（`_phase_execute:6429`）。Step3 在
  N 条 intent × 4 个 LLM 点上仍跑 LLM。N 倍放大。
- **修法**（路 A）：上述 4 处入口加 `if getattr(self, "execute_no_llm", False): return/skip`。
- **状态**：[ ] 待办（待口径决策确认）

### P1-2. Step2 / Step3 双套校验职责归一（二）
- **位置**：`step2_validate_subagent.py:71`（`_step2_validate_intents` → validate_two_layer）
  vs `_phase_validate`（agent.py:6087，含 ai_validate_plan）。
- **后果**：两套都能改 `intent.extras`，时序上 Step3 内逐 intent 再调一次 LLM 写前校验。
- **修法**：写前 LLM 校验职责唯一归 Step2；Step3 内 `_phase_validate` 走
  `CODEMAKER_VALIDATE_SKIP_IN_V2` 或 `execute_no_llm` 短路直接 return。
- **状态**：[ ] 待办（与 P1-1 同批）

### P1-3. artifact 命名冗余收敛（六）
- **位置**：`step3_execute_subagent.py:168-172`（6 键：steps/all_steps、results/all_result_rows
  冗余）；`run_v2:4399-4401` + `step4:108-111` 复制。
- **致命点**：`run_v2:4405` 取 `s4.artifacts["failures"]` 累加到 `all_failures`，但 Step3
  写在 `s3.artifacts["failures"]` 那份没被 extend 进来（Step4 只是 copy）。聚合口径漂移。
- **修法**：step3 artifacts 删 `all_steps`/`all_result_rows` 冗余键；run_v2 顶层直接从
  s3 取 failures（不再经 s4 复制）；step4 artifacts 不再复制 failures/subtasks，只产
  summary + induced_count。
- **状态**：[x] 完成（2026-08-21）

---

## P2 — 长期漂移风险消除

### P2-1. Step2 `_tmp_res` 伪载体解耦（五）
- **位置**：`step2_validate_subagent.py:62-72`（用首条 intent 构造 `_tmp_res`）。
- **后果**：多 intent 场景 thinking 标签归属混乱（都打在 intents[0]）；V2 contracts
  "错误归属固定本步 step_id" 在 intent 维度被破坏。
- **修法**：`_step2_validate_intents` 接收 `thinking_sink`/`failure_list`（list 参数），
  不借 `_tmp_res` 中介；或 Step2 subagent 直接接 SSE 进度 sink。
- **状态**：[ ] 待办

### P2-2. 反模式归纳双份合一（四）
- **位置**：`agent.py:7920` `_phase_conclude`（legacy 仍调）vs
  `step4_conclude_subagent.py:88-91`（内联同一 `induce_anti_patterns`）。
- **后果**：两份构造 `failed_traces` 逻辑并行维护，形状可能漂移。
- **修法**：提成 `TableAgent._collect_failed_traces(failures, stem, sheet)` +
  `_induce_anti_patterns_via(traces, enhancer, stream_res)` 两个静态 helper，
  V2 Step4 与 legacy `_phase_conclude` 共用。删 step4 内联复制。
- **状态**：[x] 完成（2026-08-21）

---

## P3 — 小重构

| # | 问题 | 位置 | 修法 | 状态 |
|---|------|------|------|------|
| 7.1 | split_multi_intent 被调 2 次（Step1 + ParseAgent 内部） | step1:68,75 | ParseAgent 缓存 `_last_segments`，Step1 读复用 | [x] 完成（2026-08-21） |
| 7.2 | `_dispatch` 与 `_retry_dispatch` 复制 5 分支 | agent.py:6403,5896 | 提模块级自由函数 `_dispatch_action`，两闭包调它 | [x] 完成（2026-08-21） |
| 7.3 | `execute_no_llm` 实例属性跨请求污染风险 | agent.py:5801 | per-request 实例或显式参数 | [ ] 待办 |
| 7.4 | orchestrator 对 step.execute 返回 None 无防御 | orchestrator.py:88 | 加 None 兜底转 soft error | [x] 完成（2026-08-21） |
| 7.5 | hard error 利用率低（仅 Step1 标 hard，2/3/4 全 soft） | step2-4 | 关键失败标 hard | [ ] 待办 |
| 7.7 | `_phase_summarize` 与 Step4 汇总重叠（N+1 次 LLM） | agent.py:7868 + step4 | batch 场景收敛 | [ ] 待办 |

---

## 进度日志

- 2026-08-21（批次 1）：统一开关 / Step3 env 突变去 env / step2 写回 s1 删除 / step4
  type("R",...) 内联 / orchestrator 死代码删除 / 7 步管道出口 adapter / ExcelAgentServices
  服务对象注入。
- 2026-08-21（批次 2 — 本轮）：
  - **P0-1** 完成：run_v2 入口 `_llm_counter.reset()`。
  - **P0-2** 完成：step3 metrics 改 `services.peek_llm_total()` 真实值（services 加方法）。
  - **P0-3** 完成：ParseAgent `_last_locator_results` 全段收集 + Step1 写入
    `s1.artifacts["locator_results"]` + Step2 改读 artifacts（消除步间隔离违反）。
  - **口径决策**：路 B（全拆开到各 Step）。分 5 阶段推进。
  - **P1-3** 完成：step3 artifacts 删冗余键（all_steps/all_result_rows）；
    run_v2 顶层直接从 s3 取 failures（不经 s4 复制）；step4 不再复制 failures/subtasks。
  - **P2-2** 完成：提 `TableAgent._collect_failed_traces` + `_induce_anti_patterns_via`
    共享 helper，V2 Step4 与 legacy `_phase_conclude` 共用，消除双份漂移。
  - 测试：1083 passed（排除 test_pk_conflict_step2_e2e + test_column_matcher_semantic 既有失败）。
- **剩余**：路 B 阶段 2-5（`_phase_*` 拆分，S4 级工程）；P1-1/P1-2（execute_no_llm
  短路，路 B 拆分后自然解决，或作为路 B 完成前的过渡短路）；P2-1（_tmp_res 解耦）；
  P3 小重构（7.1 split 复用 / 7.2 dispatch 合并 / 7.3 实例属性污染 / 7.4 None 防御 /
  7.5 hard error 利用率 / 7.7 summarize 收敛）。

- 2026-08-21（批次 3 — 本轮）：
  - **P3-7.4** 完成：orchestrator step.execute 返回 None 兜底转 soft error。
  - **P3-7.2** 完成：提模块级自由函数 `_dispatch_action`，`_dispatch` 与
    `_retry_dispatch` 闭包统一调它（兼容 SimpleNamespace 测试 mock）。
  - **P3-7.1** 完成：ParseAgent 缓存 `_last_segments`，Step1 读复用，消除重复调
    split_multi_intent。
  - 测试：1083 passed（排除 2 个既有失败）。
- **剩余**：路 B 阶段 2-5（`_phase_*` 拆分，S4 级工程）；P1-1/P1-2（execute_no_llm
  短路）；P2-1（_tmp_res 解耦）；P3-7.3（实例属性污染）/7.5（hard error）/7.7（summarize）。

- 2026-08-21（批次 4 — 全链路审查修复）：
  - **全链路审查**（4 维度并行探查）：SSE 事件流 / 状态隔离并发 / 错误处理 hard 语义 /
    职责归属冗余。新发现 9 类问题。
  - **致命 1**：`agent_service.py:2299` `_4STEP` 未定义（重命名漏改）→ subtask 事件
    NameError。已修（改 `_V2` + step3_execute 命名）。
  - **致命 2**：verify-repair LLM 泄漏（agent.py:6475→7152）——写成功+校验失败进
    repair loop 调 LLM，绕过 Step3 零 LLM 不变量。已修（6475 加 `execute_no_llm` guard）。
  - **高危 3**：`execute_no_llm` 实例属性并发互踩（services + _run_single 双层突变）。
    已修（改 `threading.local` + property/setter，各线程隔离）。
  - 测试：1083 passed（排除 2 个既有失败）。
- **剩余**（审查发现的未修项，按优先级）：
  - **中危 4** [x] 完成（2026-08-21）：Step1/Step3 metrics 改差值法（execute 前后读
    counter 差值 = 本步 LLM 调用数，替代硬编码 0 / 累计值）。
  - **中危 5** [x] 完成（2026-08-21）：Step3 子任务异常补写 all_failures +
    sub_tasks（原只加通用 StepError，具体表/列/根因丢失 + 汇总漏计）。
  - **中危 6** [x] 完成（2026-08-21）：run_v2 stage_end 段仅聚合 hard error 到
    all_failures（soft failures 由 s3.artifacts 单一源取，避免同一 failure 双记 +
    形状不一）。
  - **中危 7** [x] 完成（2026-08-21）：Step3 全子任务失败 → hard error（原全 soft，
    hard 语义形同空设）。
  - **中危 8** [x] 完成（2026-08-21）：`_resolve_table` 不复用 Step1 locator_results
    （冗余路由）。修法 A（仅 V2 短路复用）：Step1 SubAgent 把全段 candidates stems 合并
    去重注入每条 `intent.extras["locator_candidates"]`；`_phase_partition` 在
    execute_no_llm=True（V2 Step3 透传）路径下，若 table_hint 在候选集内 → 直接精查表
    跳过 `_resolve_table`（行索引策略1 用 locator_value 可能误命中它表覆盖正确
    table_hint，如 reward_id 命中 item 表），无候选匹配才回退 `_resolve_table` 全策略。
    legacy 路径不动。测试：1083 passed。
  - **低危**：~~Step4 ok 镜像 Step3~~ [x] 完成（2026-08-21）；
    _phase_summarize 冗余模板汇总（N+1 LLM）——已确认不存在（前批次已消除，
    `_phase_summarize` 成功路径走模板、`_phase_conclude` 单次 induce）；
    ~~needs_confirm ok=None 被计失败~~ [x] 完成（2026-08-21）；
    ~~sub_tasks 缺 index/needs_user_fill/partial 字段~~ [x] 完成（2026-08-21）；
    ~~SkillUpdater 写盘 lost-update（无锁）~~ [x] 完成（2026-08-21）；
    ~~`_llm_counter` 共享 reset 互踩~~ [x] 完成（2026-08-21）；
    ~~`_cancel_event` 共享取消信号错发~~ [x] 完成（2026-08-21）；
    ~~services.get_locator_result 死码~~ [x] 完成（2026-08-21）。
- 2026-08-21（批次 6 — 本轮）：
  - **中危 8** [x] 完成：`_resolve_table` 复用 Step1 locator_results candidates（V2 短路路径）。
    Step1 SubAgent 注入 `intent.extras["locator_candidates"]`；`_phase_partition` execute_no_llm
    路径下 table_hint 命中候选集则精查表跳过 `_resolve_table`（避免行索引策略1 用
    locator_value 误命中它表覆盖 decompose 已选定的 table_hint）。legacy 路径不动。
  - **低危 Step4 ok 镜像 Step3** [x] 完成：Step4 不再独立判 all_ok=n_fail==0，改镜像
    s3.ok（Step4 只汇总不改 ok 语义）；n_ok/n_fail/n_pending 口径与 Step3 一致（ok=None
    待确认不计失败）；metrics 新增 subtasks_pending。
  - **低危 needs_confirm ok=None 被计失败** [x] 完成：Step3 原 `if not sub_res.ok` 把
    ok=None（needs_confirm 默认态）当 False 走失败分支 → failures 重复收集 + metrics
    subtasks_fail 误计。改为 needs_confirm 单独透传 pending_search 为软失败，仅
    `sub_res.ok is False` 进真失败分支；全失败判断排除待确认项；metrics 新增
    subtasks_pending；`ok = not any(e.is_hard ...)` 让 soft 不阻断（与中危 6 语义一致）。
  - **低危 sub_tasks 缺 index/needs_user_fill/partial** [x] 完成：Step3 sub_tasks 对齐
    legacy 多任务路径形状，补 index/needs_user_fill/partial 字段（前端分段渲染依赖）。
  - **低危 SkillUpdater 写盘 lost-update** [x] 完成：`_apply_anti_pattern_upsert` 的
    read-modify-write 持模块级 threading.Lock（`_AP_UPSERT_LOCK`），避免并发请求 A/B 各读
    旧文件→改→写后写覆盖先写。原 `_atomic_write` 仅防"并发读半截文件"。
  - **低危 _llm_counter 共享 reset 互踩** [x] 完成：run_v2/run 入口 per-run 新建
    LLMCounter 赋值 self._llm_counter（替代 reset 共享实例）。原 reset 清 _instance_stats
    在单实例 agent 跨请求串行/并发时互踩（A 计数被 B reset 清零）。
  - **低危 _cancel_event 共享取消信号错发** [x] 完成：_cancel_event 改 thread-local
    property（同 execute_no_llm 模式），读写都走当前线程 local，per-request 隔离。原实例
    属性被单实例 agent 跨请求共享，worker 设值互踩。29 处 getattr/赋值透明走 property。
  - **低危 services.get_locator_result 死码** [x] 完成：Step2 已改读 s1.artifacts，
    该方法无调用方，删除方法 + 改 step2 注释。
  - 测试：1083 passed, 1 skipped（基线一致，无回归）。

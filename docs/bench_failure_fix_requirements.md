# Bench E2E 失败修复需求文档（基于 O19 真 LLM 跑 6 样例诊断）

> 来源：O19 bench_4step.py 跑 6 样例（R7 解封后首次真 LLM e2e）。
> 诊断方式：临时 traceback 钩子抓 stack + 派 explore agent 读 ParseAgent/DecomposeAgent/LocatorAgent/validator_agent/error_classifier 源码定位根因。
> 日期：2026-08-19。

## 0. e2e 基线 + 能力边界

| # | 样例 | ok | 问题 | 代码可修 | LLM 能力缺口 |
|---|---|---|---|---|---|
| S1 | 封印魔龙 | True | 重复写入 + 覆盖度不足 | ✅ 去重钩子缺 | ⚠ 候选不全 |
| S4 | 万圣狂欢 | False | 错表 + 未知错误 + parser 崩 | ✅ alias 错 + classifier regex + parse 降级（O20a/O20f） | ⚠ DecomposeAgent 错路由 |
| S6 | 复合修改 | False | modify 失败 | ✅ classifier regex + 失败信号 | - |
| S2/S5 | 幽冥宗/聚灵塔 | False | str+list 崩（O19 已修） | ✅ 已修 | - |
| S3 | 九尾天狐 | True | 覆盖度不足（pet 1/4 表） | ✅ 候选策略 | ⚠ 单表漏拆 |

**结论**：4 项失败中 3 项（S1 重复、S4/S6 未知错误）主因是代码层缺统一去重与错误分类信号，非 LLM 本身。1 项（S4 错表）含 alias 硬 bug。覆盖度不足（S1/S3）含 LLM 能力成分但代码层候选策略可缓解。

---

## 1. P0 — S4 错表 + 未知错误短路（功能错误，高优先）

### 1.1 根因
- **alias 硬 bug**：`server/agent/excel/locator/alias_mapping.json:8` `"道具" → "assistant_level.xlsx"`，正确应为 `item`。S4 文本"必给道具/兑换券道具/宝箱道具"3 次命中"道具"全指向 assistant_level（错表），item 表只靠 reward↔item FK 扩表间接进候选。
- **DecomposeAgent 错路由**：`subagent/decompose_agent.py:222-241` 每候选表单 prompt，LLM 对未见表把 4 种子类型全路由到 residence_building。
- **UNKNOWN 短路**：`repair/error_classifier.py:285-293` regex `_COL_NOT_FOUND_RE:46` 要求"列...[不存在|未找到]"顺序，但 `_run_set` 产"未找到列[X]/无法匹配目标列"是"未找到+列"顺序反→匹配失败落 UNKNOWN→`agent.py:6639-6643` 交互模式直接 break 不自动修复→报"未知错误"。

### 1.2 修复实现思路
1. **alias 修**：`alias_mapping.json:8` 改 `"道具" → "item"`（需核 resources 真实表 stem，item.xlsx 通常为道具/物品表）。
2. **DecomposeAgent 输出校验**：`decompose_agent.py:222-241` 对每候选表 LLM 返回 intent 的 `table` 字段校验是否真实命中候选（LLM 幻觉表直接丢弃 + 降级回规则 splitter_baseline 兜底）。
3. **classifier regex 放开**：`error_classifier.py:46` `_COL_NOT_FOUND_RE` 加 `"未找到列"` 模式（不要求顺序），或新增独立 `未找到列\[` 匹配分支。

### 1.3 模块归属
- `server/agent/excel/locator/alias_mapping.json` — alias 数据修
- `server/agent/excel/subagent/decompose_agent.py:222-241` — LLM 输出 table 校验
- `server/agent/excel/repair/error_classifier.py:46, 285-293` — regex 放开 + UNKNOWN 兜底

---

## 2. P0 — S1 重复写入（数据正确性，高优先）

### 2.1 根因
- `DecomposeAgent._to_split_intents`（`decompose_agent.py:270-299`）直接转 LLM JSON 数组 → SplitIntent，**无同表同配置去重**。
- 4-step 路径走 `validate_two_layer`（`validator_agent.py:527-567`），该函数非阻断（ok 恒 True）且**从不调 `_suppress_over_produce`**（该去重只在 `validate()` 主入口走，4-step 不走）。
- LLM 过产 6 条相同 Quest 配置 → 原样进 Step5 → `agent.py:4654` 按 ordered_idx 每位置执行一次 → 写 6 行相同。

### 2.2 修复实现思路
1. **DecomposeAgent 输出去重**：`decompose_agent.py:270-299` 按 `(stem, sheet, 关键字段hash)` 对 SplitIntent 去重（同表同配置只留 1 条）。关键字段 = produces_label + locator_value + target_field + value 的 hash。
2. **validate_two_layer 接 _suppress_over_produce**：`validator_agent.py:527-567` 在 FK 拓扑层后调 `_suppress_over_produce`（或新增 `_dedup_intents` 对 4-step 路径生效），让过产被抑制而非原样进 Step5。
3. **prompt 约束**：DecomposeAgent 每候选表 prompt 加"同表只产一条主配置"约束（降 LLM 过产概率）。

### 2.3 模块归属
- `server/agent/excel/subagent/decompose_agent.py:270-299` — 输出去重
- `server/agent/excel/subagent/validator_agent.py:527-567` — 4-step 接去重
- `server/agent/excel/subagent/decompose_agent.py:222-241` — prompt 约束

---

## 3. P1 — S6 modify 失败（modify 链路分类信号缺失）

### 3.1 根因
- `_run_set`（`agent.py:2469-2748`）多字段 modify 时，列名/点分键（`attributes.HPMaxCon` 类）匹配失败 → `match_target=False` → 但单列失败未入 `res.failures` 带 `failed_col`，整体落空 UNKNOWN。
- `error_classifier.py:46` regex 顺序不匹配（同 §1.1 第 3 点）→ UNKNOWN → 短路不修复。
- 阴阳权重 0.3→0.45 + 法宝 spell 70 modify 都走此路径。

### 3.2 修复实现思路
1. **_run_set 失败信号**：`agent.py:2469` 多字段路径 `fields` 映射失败的单列单独入 `res.failures` 带 `failed_col`，让 classifier 有列名数据而非空 UNKNOWN。
2. **classifier regex 放开**：同 §1.2 第 3 点。
3. **点分键 alias 补**：`agent.py:2532` `_translate_dotted_keys` 对 fabao 权重/倍率列缺别名时补 alias 映射（需核 fabao 表真实列名）。

### 3.3 模块归属
- `server/agent/excel/core/agent.py:2469` `_run_set` — 失败信号入 failures
- `server/agent/excel/repair/error_classifier.py:46, 285-293` — regex 放开
- `server/agent/excel/core/agent.py:2532` `_translate_dotted_keys` — 点分键 alias

---

## 4. P1 — S1/S3 覆盖度不足（候选策略 + LLM 漏拆）

### 4.1 根因
- `DecomposeAgent.decompose`（`decompose_agent.py:52-167`）每候选表单 prompt 并发，产出意图数受 LocatorAgent 候选表集合完整度 + 每表单 LLM 是否识别子任务双重限制。
- S1 期望 8 表（quest/combat/reward/item*/entity_prefab/interaction/spawn_*），候选不全或 LLM 单表漏拆，最终只返回 2 条有效意图。
- 其余 6 表要么没进候选（`locator_agent.py:227-252` `_expand_by_fk` 只从已命中候选补 FK 对端，未命中整表的业务子表不进入），要么占位符依赖未闭环在 Step5 `_phase_execute`（`agent.py:5920-5936`）被标 `placeholder_unresolved` 跳过/回滚。

### 4.2 修复实现思路
1. **候选完整度提升**：`locator_agent.py:227-252` `_expand_by_fk` 扩为多跳（FK 链主表漏拆时多跳扩表），复杂输入改用 `locate_all`（含 column_semantic 0.60 命中）而非仅 FK 扩表。
2. **DecomposeAgent 全链兜底**：`decompose_agent.py:77-84` 对 FK 链主表漏拆时补一次全链 LLM 拆分兜底（识别"任务→战斗→奖励"类业务链关键词触发多表联合拆分）。
3. **占位符未解析不静默跳过**：`agent.py:5920-5936` 占位符未解析时入 `res.failures` 带 `placeholder_unresolved` + 依赖表名，让 _phase_summarize 上报而非静默跳过（保 D6）。

### 4.3 模块归属
- `server/agent/excel/subagent/locator_agent.py:227-252` — _expand_by_fk 多跳
- `server/agent/excel/subagent/decompose_agent.py:77-84` — 全链 LLM 兜底
- `server/agent/excel/core/agent.py:5920-5936` — 占位符未解析入 failures

### 4.4 能力边界
此项含 LLM 能力成分（单表漏拆子任务）。代码层候选策略 + 全链兜底可缓解，但 LLM 本身对复杂业务链理解不足需单独跟进（G3 few-shot RAG 注入 DecomposeAgent prompt 是长期解）。

---

## 5. P2 — llm_calls=0（可观测性）

### 5.1 根因
bench SSE 解析未捕 heartbeat 的 llm_calls 计数，或 `_llm_counter` 对接 bench 流式输出缺失。非功能 bug，纯可观测性。

### 5.2 修复思路
bench_4step.py SSE 解析加 heartbeat event 的 llm_calls 提取 + agent `_llm_counter.peek_total()` 经 SSE heartbeat 推送。低优先，不影响功能。

### 5.3 实施（O20h，2026-08-19）
✅ 代码完成：根因 = bench `dry_run=True` 走 `_dry_run_chat` 2624 构造 `tmp_agent = TableAgent(...)` 新建独立 `_llm_counter`，heartbeat loop 读主 agent counter（永 0）。修复 = `_dry_run_chat` 2638 共享属性列表加 `"_llm_counter"`，tmp_agent 共享主 agent counter 实例 → heartbeat `peek_total()` 实时非 0。bench_4step.py:131-132 SSE 解析 + agent_service.py:2232-2233 heartbeat 推送字段已对齐无 bug。2 单测绿，全量 1028 passed / 1 预存红零回归。e2e 阻 R7（serve 起后跑 bench 6 样例确认 llm_calls 非 0）。

---

## 6. 实施顺序建议

1. **O20a**：S4 错表修复（§1）— alias 修 + DecomposeAgent table 校验 + classifier regex 放开。最小闭环，先跑 S4 验证错表消解。✅ 完成（981 passed / 1 预存红）
2. **O20b**：S1 重复写入修复（§2）— DecomposeAgent 输出去重 + validate_two_layer 接 _suppress_over_produce。跑 S1 验证 6 条→1 条。✅ 代码完成（8 单测绿），但 S1 实跑仍 6 条重复（fields 含不同占位符 sig 不同 → 不去重，留 DecomposeAgent prompt 约束 follow-up）→ **O20e 根治**（2026-08-19）：`_dedup_intents` 占位符值归一为 `<ph>`（正则 sub）消除跨候选 prompt 产同表 intent 假性差异 + DecomposeAgent prompt 加"同表同 sheet 只产一条主配置"约束。5 单测绿，全量 1011 passed / 1 预存红零回归。
3. **O20c**：S6 modify 失败修复（§3）— _run_set 失败信号 + classifier regex（与 O20a 共享 §1.2 第 3 点）。跑 S6 验证阴阳权重/spell modify 成功。✅ 完成（5 单测绿 + S2 回归确认 classifier 生效）
4. **O20d**：S1/S3/S4 覆盖度（§4）— 候选策略 + 全链兜底 + 占位符入 failures。跑 S1/S3 验证 8/4 表覆盖提升。✅ 代码完成（12 单测绿，全量 1006 passed / 1 预存红零回归），e2e 阻 R7（serve 未在线）。残留：LLM 能力缺口（单表漏拆，G3 few-shot RAG 长期解）。

每轮改完跑单测（无回归）+ bench 对应样例 e2e 验证（真指标），不全跑 6 样例（耗时），单样例针对性验证。

## 7. 能力声明（不能完成的样例漏在哪）

- **S1 封印魔龙**：当前漏 6/8 表（combat/reward/item/entity_prefab/interaction/spawn_* 未执行）+ 6 条重复写入。修复后预期覆盖 4-6 表（候选策略缓解），剩余 2-4 表需 LLM 全链理解提升（长期）。
- **S4 万圣狂欢**：当前 4 项全失败（错表 + 未知错误）。修复后预期 2-3 项成功（alias 修 + classifier 通），剩余需 DecomposeAgent 路由改进。
- **S6 复合修改**：当前 2/7 成功。修复后预期 4-5/7 成功（modify 失败信号 + classifier），剩余点分键 alias 需逐表补。
- **S3 九尾天狐**：当前 pet 1/4 表。修复后预期 2-3 表（候选策略），剩余需 LLM 单表拆分提升。
- **S2/S5**：str+list 崩 O19 已修，现 ok=False 但正常失败上报（school/解锁等级列不存在 + 聚灵塔 3/6+11 失败）。功能仍需 DecomposeAgent 改进（长期）。

**agent 当前能力优先级**：先消硬崩（已修）+ 错表（§1）+ 重复写入（§2）+ modify 分类（§3）= 3 个 P0 代码可修项，把 ok=False 从"崩/错表/未知错误"降到"正常失败上报"。覆盖度（§4）是 P1，含 LLM 能力缺口，长期跟。

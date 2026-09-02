# Excel-Agent-And-Merge 交接 Prompt（Shadow 评测闭环 + 优化落地）—— 完整承接版

日期：2026-09-02。**本文件是唯一交接来源，下一次对话直接以此为上下文继续，无需重新探索。**

项目根：`C:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge`（win32 / PowerShell 5.1 / 非 git 仓）。
Python venv：`.venv\Scripts\python.exe`（3.14）。codemaker serve **已在运行**
（`http://127.0.0.1:8666`，Basic Auth，凭据在项目根 `.env` 的
`CODEMAKER_USERNAME/PASSWORD/SERVER_URL/MODEL`；还有 `CODEMAKER_DECOMPOSE_TIMEOUT=120`、
`CODEMAKER_PROMPT_ATTEMPT_TIMEOUT=150`）。

---

## 0. 最高铁律（每步必守）
1. **禁止硬编码**——不准用表名/PK/关键词/样例特判"凑过"某 case，只修通用框架能力。
2. **判定门 LLM 优先，规则兜底**。
3. **不许硬拟合验证**——不准改测试输入/换实体制造 PASS。验证 = 全量
   `cd server; & ..\.venv\Scripts\python.exe -m pytest tests/ -q` 绿 + 纯函数单测 + 确定性度量；
   任何**改变主链路判定结果**的行为改动，必须先过 §3 的 shadow/error-budget 门控，不许直接切默认。
4. **基线**：全量套件当前 **1520 passed + 1 skipped**，仅 **2 个既有 real-API 已知失败**
   （`test_decompose_prompt_injects_domain_few_shots_without_parser_hook`、
   `test_e2e_real_formula_sum_delete_row`），与本轮改动无关，勿动。全量跑一次 ~5.5 分钟。

---

## 1. 本轮（含之前几轮累积）已落地的能力（全部加法/可回退/纯函数单测）

### 1.1 现象3（脏写根因）
- `server/agent/excel/core/pipeline/column_gate.py`：写前列匹配闸门。关键列＝复合主键
  （单列自增主键不算，符合项目 P26"仅强校验主键"原则）未匹配、且存在无法绑定表头的字段键
  → abort，禁止写残缺行，回 Step2；仅非关键列未匹配 → partial 放行。接入
  `core/agent.py::_run_add` + 新方法 `_column_match_gate_abort`；写后验证把 `match_field`
  软失败与 `coerce_value` 同口径归正 `partial`（消除"行已写却整体判失败"矛盾）。
  开关：`CODEMAKER_COLUMN_MATCH_GATE`（默认 1，设 0 回退）。
- `value_extractor.extract_fields_from_text`（header 锚定确定性抽取）接入
  `decompose_agent._splitter_baseline` 的 path-b add 分支（0-LLM 兜底路径）。
  开关：`CODEMAKER_BASELINE_VALUE_EXTRACTOR`（默认 1）。

### 1.2 主线1/主线2
- `core/pipeline/resolution_ledger.py`：`make_issue_id`（**内容派生**稳定 id，绝不用
  `id(obj)`）+ `ResolutionLedger`（幂等记录，跨 Step/跨 deepcopy 稳定）。`contracts.py`
  的 `StepContext` 加 `resolution_ledger: dict` 字段 + `get_ledger()/sync_ledger()`。
  Step2 (`step2_validate_subagent.py`) 执行末尾把已解决的 PK 修正/用户跳过决策登记进台账。
- `decompose_agent._to_split_intents`：非 dict 元素原静默 `continue` → 改为 `add_thinking`
  可追踪 trace（不再静默吞）。

### 1.3 观测 & 诊断
- `core/pipeline/step_trace.py`：`build_step_trace(step_metrics, llm_stats, thresholds)` 聚合
  各 Step metrics + LLM 快照，**确定性慢因归因**（优先级 llm_timeout > schema_too_large >
  candidate_overflow > slow_step）。接入 `step4_conclude_subagent.py`
  的 `artifacts["step_trace"]` + `warnings`（观测只读，不改变执行结果）。
- `core/pipeline/table_card.py`：`build_table_card`/`render_card_text`——轻量表卡（用途/PK/FK/别名/
  命中列），纯函数，**目前只用于 shadow_runner 的量化，未接主 prompt**（这是后续可做的提速点）。

### 1.4 建议 7/5/4/2（本轮重点，已全部落地验证）
- **建议7 Step4 透明化** `conclude_report.py`：
  - `is_clean_success(prior_ok, n_ok, n_fail, n_skipped, n_partial, has_incomplete, n_failures)`
    ——有任一问题信号（真失败/跳过/partial写入/漏解析/任何 failure 记录）即非"干净成功"。
  - `render_bucketed_failures(failures)`——按 Step1/2/3 **分桶、不截断**（原 `failures[:5]` 会吞掉后续失败）。
  - 接入 `step4_conclude_subagent.py`：`all_ok` 收口到 `is_clean_success`；`_prior_step_failures`
    补 `step_id`；三处 `failures[:5]` detail 循环替换为 `render_bucketed_failures`。
- **建议5 Step2 结构化校验** `issue_severity.py`：
  - `classify_severity(error_type, issue_type, severity=None, is_pk_missing=False)`——
    显式 severity 优先 → `HARD_ISSUE_TYPES={unique_violation,type_mismatch,col_not_found}` 命中→hard
    → `missing_required` 仅 `is_pk_missing=True` 才 hard → 否则 soft。**不解析 root_cause 自然语言**。
  - 接入 Step2 `_is_hard_validation_issue`：结构化判定为主，保留一条**窄字符串兼容兜底**
    （"指令明确"+"业务必填列"，待 validator 侧发结构化 severity 后可删）。
  - 与项目既有 P25/P26 原则一致（非主键 missing_required 必须 soft，已有回归测试锁定）。
- **建议4 schema-first** `field_partition.py`：
  - `partition_fields_by_schema(fields, header_names)`——按归一化表头列集合把 fields 分
    known/unknown（点分嵌套键放行；纯数字键→unknown；保守，不误伤模糊匹配）。
  - `_to_split_intents` 接入：未知列 **surfacing**（`add_thinking` 记可追踪 trace，供 Step2/
    诊断可见），**不从 fields 直接删键**（避免误伤 agent 侧 ColumnMatcher 的模糊/别名匹配能力）；
    写时的实际排除由已落地的 `column_gate` 保证。
- **建议2 LLM 预算硬限** `llm_budget.py`：
  - `LLMBudget(limit=3)`：`try_consume()`超预算返回 False 不累加；`remaining/exhausted/snapshot`。
  - `DecomposeAgent` 覆盖 `_call_llm`/`_call_llm_raw`：调用前过 `_budget_gate()`，耗尽则短路
    返回 None（调用方走 baseline/partial，**不再串行补洞**）。`decompose()`/`decompose_segment()`
    入口调 `_init_step1_budget()` 重置。开关：`CODEMAKER_STEP1_LLM_BUDGET`（默认 **3**）。
  - **已知作用域限制**：预算是**每次 decompose 调用（每段）**重置，非"整个 Step1 请求总计 ≤3"
    ——若要 request 级总预算需穿 `ParseAgent`（尚未做，见 §5 待办）。
  - **已封住的祸源**：`_backfill_missing` 逐缺表串行调 `_call_llm_raw` 补洞——现在超预算直接
    截断，避免"缺表越多、串行越久"的墙钟爆炸。**尚未处理**：`force_grouped` 链式分组路径直连
    `client.prompt`（不走 `_call_llm`），本预算未覆盖它，但它已被 `_max_groups=2` 硬顶限流。

### 1.5 提速（主路径 schema 裁剪，已改默认值，数据见 §3.3）
- `decompose_agent._build_schema_block`：
  - `CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET` 默认 **0 → 1500**（原先设过 4000，
    但实测真实块基本 <4000 会"空转"，**1500 才是数据验证过的有效值**）。
    超阈值时用既有 `schema_budget.py`（`apply_schema_budget`）：required 表完整、
    dependency 表转 PK/FK 摘要、context 表不注入。
  - 动态 `max_cols` 默认收紧：候选 >5 张：8→**6**；候选 3~5 张：12→**10**（≤3 仍 16）；
    **命中用户输入的列强制保留**，只砍非命中噪声列（"召回宽、决策窄"）。
  - 均可用 env（`CODEMAKER_DECOMPOSE_SCHEMA_COLS`/`_SHEETS`/`_CHAR_BUDGET`）覆盖回退。
- **重要教训**：我最初用"手挑最小候选集"测量提速效果，显示几乎无变化（因为旧默认对
  ≤3候选本就给16列，没有裁剪空间）；换成**真实 LocatorAgent 规则路径**（宽召回 4~12
  候选）重测后，才测出 schema 平均砍 **~58~62%**。**教训：任何"优化生效证据"必须用真实
  召回场景验证，不能用手工挑选的简化输入自证。**

### 1.6 本轮新增测试文件（server/tests/，全部通过）
```
test_column_gate.py                  test_baseline_value_extractor.py
test_to_split_intents_drop_trace.py  test_resolution_ledger.py
test_step2_resolution_ledger.py      test_step_trace.py
test_table_card.py                  test_replan_hints.py
test_preflight.py                   test_semantic_shadow.py
test_schema_block_budget_wiring.py  test_conclude_report.py
test_issue_severity.py              test_field_partition.py
test_llm_budget.py                  test_shadow_eval.py
```
基线 1417 passed → 现 **1520 passed**（本会话累计 +103 测试，全部通过，无回归）。

### 1.7 还有两个此前落地但本轮未详述的模块（供检索用）
- `core/pipeline/replan_hints.py`：`build_replan_hints(errors)`——把 Step2 错误归纳成结构化
  重规划提示（缺表/缺producer/悬空FK/PK冲突/类型），接入 Step2 artifacts（**默认不开 hard gate**）。
- `core/pipeline/preflight.py`：`build_preflight_report(items)`——Step3 dry-run 悬空占位符
  go/no-go 报告，接入 Step3 artifacts（观测只读，真实事务快照/回滚**未做**，属超范围/需专项）。

---

## 2. Shadow 评测闭环（核心交付：把"赌"变"测"）

### 2.1 纯函数评分/门控核心（已确定性单测，`test_shadow_eval.py` 12 个测试全过）
`server/agent/excel/core/pipeline/shadow_eval.py`：
```python
score_case(actual_intents, expect) -> {
    table_recall, table_precision, missing_tables, extra_tables,
    field_recall, n_expected_tables, n_actual_tables
}
aggregate_scores(scores: list[dict]) -> {
    cases, avg_table_recall, avg_table_precision, avg_field_recall,
    total_missing_tables, total_extra_tables, perfect_recall_cases
}
```

`server/agent/excel/core/pipeline/promotion.py`：
```python
DEFAULT_ERROR_BUDGET = {
    "max_recall_drop": 0.02,   # 表 recall 允许最大下降（绝对值）
    "max_field_drop": 0.05,    # 字段 recall 允许最大下降
    "allow_new_missing": 0,    # 允许新增缺表总数
    "min_recall": 0.0,         # 候选 recall 绝对下限（可选硬门槛）
}
evaluate_promotion(baseline_agg, candidate_agg, budget=None) -> {
    "promote": bool, "violations": [str...], "deltas": {recall, precision, field_recall, missing_tables}
}
```

### 2.2 【重要】指标计算原则（下一次对话做任何评测/判断必须遵循这套口径，勿另创）

**表级归一化**（`_norm()`，`shadow_eval.py:19-20`）：
```
_NORM_RE = r"[\s_:\-./\\()\[\]{}（）【】]+"
norm(v) = _NORM_RE.sub("", str(v).split(":")[0]).strip().lower()
```
即：去掉 `:type` 后缀 → 去空白/下划线/冒号/横杠/斜杠/括号等分隔符 → 转小写。
**表名比对全部走这个归一化**（如 "Reward" 与 "reward:sheet" 视为同一表）。

**table_recall**（召回率）：
```
recall = |expect_tables ∩ actual_tables| / |expect_tables|
```
- 分母是**金标期望的表集合大小**；expect 为空时约定 recall=1.0（无期望即无所谓漏）。
- 只看"表是否出现"，**不看该表产出了几条 intent、顺序、sheet 是否对**（表级颗粒度，粗但稳健）。

**table_precision**（精确率）：
```
precision = |expect_tables ∩ actual_tables| / |actual_tables|
```
- 分母是**实际产出的表集合大小**；actual 为空时：若 expect 也空→1.0，否则→0.0（产空但有期望＝全错）。
- **意义**：precision 低 = 产了金标没要求的表（幻觉表/过度展开）；recall 低 = 漏了该产的表。

**missing_tables / extra_tables**：`expect - actual` / `actual - expect`（排序后的表名列表，供人读诊断）。

**field_recall**（字段覆盖率，可选，仅当 `expect.fields` 非空才计算）：
```
对每个 expect.fields 里的 (table, [col1,col2,...])：
  该表在 actual 中所有 intent 的 fields 键（归一化后）合并成一个集合 `have`
  对每个期望列 c：命中 c∈have 记 1，否则 0
field_recall = 命中数 / 期望列总数（跨所有表汇总，非按表加权平均）
```
- **重要坑（已实测踩过）**：管线产出的字段键是**canonical 规范列名**（如 `id/name/day_limit`），
  金标若写**显示名**（如 `reward_id/名称`）会导致归一化后仍不匹配 → field_recall 假性为 0。
  **当前金标 `cases.yaml` 已移除 field 期望**（因无 schema-aware 的 display↔canonical 映射），
  **只用 table_recall/table_precision 做门控**——这是当前唯一可靠指标，下一次对话延续此原则。
  若要恢复 field 级评测，需先做"表头显示名→规范名"映射表（可从 `resources/*.xlsx` 的 row2 类型行
  或 `column_aliases.yaml` 派生），这是后续可做但**未做**的事。

**aggregate_scores 聚合**：
- `avg_table_recall/precision/field_recall`：多 case **简单算术平均**（非按表数加权）。
- `total_missing_tables/total_extra_tables`：所有 case 缺表/多表数**求和**（不去重，跨 case 独立计）。
- `perfect_recall_cases`：`table_recall>=1.0 且 missing_tables为空` 的 case 数（双重确认，防浮点误差）。

**evaluate_promotion 门控原则**：
- `recall_delta = candidate.avg_table_recall - baseline.avg_table_recall`；
  若 `recall_delta < -max_recall_drop`（即**跌幅超过预算**）→ violation。
- 同理 `field_delta`、`missing_delta`（新增缺表数超过 `allow_new_missing` → violation）。
- `min_recall` 是绝对下限硬门槛（可选，默认 0 不生效）。
- **只要有一条 violation，`promote=False`**（保守：宁可 hold 不误判 promote）。
- **这是判定"能否把某优化/降级切为主路径默认"的唯一标准**——下一次对话做建议1/3/6的
  灰度验证，必须用这个函数，不能凭感觉判断"看起来还行"。

### 2.3 Runner 用法
- **0-LLM 确定性版**（真实 LocatorAgent 规则路径，不发真实 LLM，秒级）：
  ```
  & .venv\Scripts\python.exe bench\shadow_runner.py
  ```
  产出 schema 字符量/占比/模板依赖对比（§3.3 数据即此产出）。

- **真实 codemaker serve 版**（真发 LLM，慢，每案例数十秒到数分钟）：
  `bench/shadow_eval_runner.py`，已支持：
  - `--only <id1,id2,...>`：只跑指定金标 case id（**强烈建议每次只跑1~2条**，避免超时）。
  - `--candidate KEY=VAL,KEY2=VAL2`：跑 A/B，baseline(默认env) vs candidate(覆盖env)，
    自动过 `evaluate_promotion` 门控并打印 `PROMOTION: promote=... violations=...`。
  - `--report <path>`：结果 JSON 输出路径（默认 `bench/golden/shadow_eval_report.json`）。
  - 自动从项目根 `.env` 加载 codemaker 凭据（`_load_dotenv()`，在 import codemaker_client 前执行）。
  - 若 serve 不可达 → 打印说明后**优雅退出 0**，不阻断。
  - 示例（单条 + A/B 门控验证建议3"关领域展开器"）：
    ```
    & .venv\Scripts\python.exe bench\shadow_eval_runner.py --only npc_dialogue_option `
      --candidate CODEMAKER_DECOMPOSE_DISABLE_DOMAIN=1 --report bench\golden\ab_test1.json
    ```
  - **耗时警示（已实测）**：简单用例(1表)~45s；中等(2表)~130s；复杂(10表)~**423s（7分钟）**。
    **8 条金标全跑会超过20分钟被迫中断过一次**——下一次对话务必用 `--only` 限量跑，
    或用后台进程（见 §2.4）异步跑重用例，不要同步等待。

### 2.4 后台异步运行模式（重用例必须这样跑，勿同步阻塞等待）
```powershell
Start-Process -FilePath ".venv\Scripts\python.exe" `
  -ArgumentList "bench\shadow_eval_runner.py","--only","<case_id>","--report","bench\golden\<name>.json" `
  -WorkingDirectory "C:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge" `
  -RedirectStandardOutput "bench\golden\<name>.log" -RedirectStandardError "bench\golden\<name>.err" `
  -WindowStyle Hidden -PassThru | Select-Object Id
# 之后可用 Get-Process -Id <pid> 查是否还活着；Get-Content <name>.log 查进度/结果
```

---

## 3. 已跑出的真实数据（金标真值 `bench/golden/cases.yaml`，8 条，见 §3.1）

### 3.1 金标 8 条全清单（后续所有评测都从这里选）
| # | id | 表数 | expect.tables | 复杂度 |
|---|----|------|---------------|--------|
| 1 | single_add_reward | 1 | reward | 简单 |
| 2 | single_set_prefab | 1 | entity_prefab | 简单 |
| 3 | npc_dialogue_option | 3 | entity_prefab, interaction, spawn_world_entity | 中 |
| 4 | npc_reward_mail | 4 | entity_prefab, interaction, reward, mail | 中 |
| **5** | **pet_evolve** | **2** | **pet, pet_evolve** | **中（本次已验证）** |
| 6 | school_chain | 3 | school, school_ability, school_spirit | 中 |
| 7 | quest_spawn | 2 | quest, spawn_quest_entity | 中 |
| **8** | **long_mixed** | **10** | **entity_prefab, interaction, spawn_world_entity, reward, mail, pet, pet_evolve, school, school_ability, activity** | **重（本次已验证）** |

### 3.2 本次已验证的两条用例（真实 codemaker serve，score_case 结果）—— **下一次对话直接复用这两条数据，勿重跑 long_mixed（太贵）**

**用例 #5 pet_evolve（中，已验证，结果完美）**
- 原文：`"新增灵兽子鼠，品质3，并配置它的进化链到二阶形态"`
- expect.tables：`[pet, pet_evolve]`
- 实测结果：`table_recall=1.0, table_precision=1.0, missing_tables=[], extra_tables=[]`
- 耗时：**129.0s**
- 结论：管线在 2 表规模的跨表链输入上**完全正确**（拆表准确，无幻觉，无遗漏）。
- 报告文件：`bench\golden\shadow_medium.json`

**用例 #8 long_mixed（重，已验证，暴露核心问题）**
- 原文：`"新增NPC铁匠老张 model_id 1015 放 space_id 10008 坐标(60,0,30)，点击对话'欢迎'，
  选项'锻造'和'离开'，选锻造获得 reward_id 10066，并发邮件；同时新增灵兽子鼠品质3配进化链；
  再配门派神通与灵根映射；最后加一条限时活动。"`
- expect.tables：`[entity_prefab, interaction, spawn_world_entity, reward, mail, pet, pet_evolve, school, school_ability, activity]`（10表）
- 实测结果：
  ```
  table_recall = 0.5       (10个期望表只对上5个)
  table_precision = 0.8333 (产了6个表，5个在期望内，1个不在)
  missing_tables = [pet_evolve, reward, school, school_ability, spawn_world_entity]
  extra_tables = [school_spirit]
  ```
- 耗时：**423.3s（7分钟）**
- 结论（**核心证据**）：**复杂多表（10表规模）输入下，管线只拆对了一半的表，且耗时7分钟**。
  这精确复现了诊断文档描述的核心问题（"复杂样例匹配出错 + serve耗时过长"），且提供了
  可复现、可量化、可用于 A/B 对比的**基线数据**。
- 报告文件：`bench\golden\shadow_heavy.json`；日志：`bench\golden\heavy_run.log`
- **重要**：这是**当前生产默认配置下**跑出的 baseline 结果（未加任何 candidate override）。
  下一次对话若要验证建议1/2/3的优化效果，应该用 `--candidate` 参数跑这条 case 的**candidate版本**，
  与这个 baseline（recall=0.5, precision=0.8333, dur=423.3s）做 A/B 对比，
  **不需要重新跑 baseline**（已有数据，直接复用）。

### 3.3 0-LLM shadow_runner 数据（真实 LocatorAgent 规则路径，量 prompt 体量，非真实 LLM 拆分正确性）
```
avg_schema_reduction ≈ 58~62%（宽召回 4~12 候选场景下，budget=1500+context-drop+cols收紧 生效）
schema 占全 prompt 比例 ≈ 32~35%
模板依赖（detect_cross_table_action 命中）＝ 4/8 案例
```
用途：证明 schema 裁剪确实生效、量化"非schema部分"（few-shot已确认默认关闭/主要是compact_rules+FK+text）
占比，为后续优化提供"该砍哪里"的方向依据。**这套数据不涉及 LLM 拆分正确性，只涉及 prompt 体量**。

---

## 4. 下一次对话的建议起点（承接明确，无需重新决策）

**已有 baseline（long_mixed: recall=0.5, precision=0.833, dur=423s），下一步应做 A/B 对比验证优化效果：**

1. **验证建议2（LLM预算硬限对复杂用例的影响）**：
   ```
   & .venv\Scripts\python.exe bench\shadow_eval_runner.py --only long_mixed `
     --candidate CODEMAKER_STEP1_LLM_BUDGET=1 --report bench\golden\ab_budget.json
   ```
   （用后台异步方式跑，见§2.4；单条重用例已知要~7分钟）
   拿到 candidate 的 recall/precision/dur 后，人工对比：
   - dur 是否显著下降（验证"预算硬限确实少调LLM、减少墙钟"）？
   - recall 是否在 error-budget 内（默认 max_recall_drop=0.02，即不能比 0.5 再跌超2%）？
   代入 `evaluate_promotion({"avg_table_recall":0.5,...}, candidate_agg)` 得到 promote 判定。

2. **验证建议3（模板降级，关掉领域展开器）**：
   ```
   --candidate CODEMAKER_DECOMPOSE_DISABLE_DOMAIN=1
   ```
   看关模板后 recall 是否守住（若守住甚至更高，说明当前模板可能是"帮倒忙"或"无关"，
   支持降级；若掉得很惨，说明模板目前是必要兜底，不能轻易降级）。

3. **建议1（Step1四阶段重构）需要先完成上面两个验证**，因为它是最大改动，
   应该在已验证"预算硬限"和"模板降级"各自的独立效果后，再决定是否合并成一次架构改造，
   还是保持现有渐进式改动（更稳）。

**执行纪律（每步）**：改动 env/代码 → 纯函数单测（若涉及新代码）→ 全量 pytest ≥1520 passed
（2个已知real-API失败除外）→ 用 shadow_eval_runner 单条 `--only` 异步验证 → `evaluate_promotion`
门控判定 → 达标才考虑改默认值，否则保持现状记录 hold 原因。**全程不允许跳过 shadow 验证直接改默认。**

---

## 5. 尚未做 / 已知限制（如实列出，避免下一次对话重复踩坑）
- LLM 预算作用域是"每次 decompose 调用"，非"整个 Step1 请求"，若要 request 级总控需改造
  `ParseAgent` 把预算对象传下去（未做）。
- `field_recall` 目前因 canonical/display 列名不一致，金标已移除 field 期望，只用表级指标
  （如需恢复，需先建列名映射表，未做）。
- Step3 事务快照/回滚未做（代码里已注明"超出本次范围"）。
- 建议6（Step3 拆离 legacy _run_single）未动。
- 建议5 的 root_cause 窄字符串兜底未删（等 validator 侧发结构化 severity）。
- table_card.py 建好但**未接入主 prompt**（只用于 shadow_runner 量化），是"降 schema 占比"
  之外的另一个潜在提速点（把摘要卡换掉当前 dependency 摘要渲染，可能更紧凑），未验证。

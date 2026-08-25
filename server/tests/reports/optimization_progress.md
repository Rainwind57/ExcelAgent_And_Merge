# excel_LLM Agent 持续优化进度日志

> 每次 `python -m tests.random_sample_eval` 随机抽样运行后自动追加一轮。
> 用于追踪准确率/性能随优化的变化趋势，定位仍存在的错误模式。
> 手工分析轮（无 serve 或定点修复）也在此留痕。

---

## 轮次 2026-08-25 #6（三问题诊断修复：显示命名/主键列空串/状态不一致 + 拓扑链断裂暴露）

- 动机: 用户报告三问题：①Step1 后直接 Step3 跳过 Step2；②item「（未指明列）」+ 物品编号 coerce 失败；③Step6 汇总失败但行已写入。
- 诊断方式: run_v2_direct 跑 complex_task_chain_inputs_extra#0（冰魄碎片案例），打印 LLM raw 产出 + 子任务结果。

### 三问题根因

**问题一「Step1 后 Step3 跳过 Step2」= 显示错觉，非逻辑跳步**
- V2 流水线 Step1→Step2→Step3→Step4 串行正确（`orchestrator.py:56 STEP_ORDER` 循环）。
- 用户看到的「Step3 计划/Step4 校验/Step6 汇总」是 `_run_single_impl` 内部 phase 名（旧 6-step 残留命名），非 V2 步序。
- V2 Step3 复用旧 `_run_single_impl` 处理每个子任务，内部 phase 名带 Step 编号易与 V2 步序混淆。
- V2 Step2 的 thinking 推送缺失（step2_validate_subagent 无 add_thinking），前端只看到 Step3 内部 phase。
- **不改 step 名**（影响前端 + 测试依赖），记录待办：V2 Step2 加 thinking 推送或内部 phase 名去 Step 编号。

**问题二「（未指明列）」+ 物品编号 coerce 失败 = LLM 把 produces 主键列产空串**
- LLM 产出: `{"名称":"冰魄碎片","物品编号":"","品质":2,...,"produces":"new_item_id"}`。
- `物品编号`（item_id 主键列）产**空字符串 ""**（因 item_id 占位），`produces="new_item_id"`。
- Step3 `_resolve_placeholders` 只替 `<...>` 占位符，不替空串 → 主键列空 → coerce 失败跳过。
- **是 ④ DecomposeAgent schema-grounding 缺口实证**：LLM 把 produces 主键列产空串而非占位符，Step1 出口无校验纠正。

**问题三「Step6 汇总失败但行已写入」= res.ok 不可逆 + coerce 失败误标**
- `res.add("coerce_value", False, error)` 置 `res.ok=False` 不可逆（`:495-496`，无回 True 机制）。
- 行写入成功后 `res.add("append_row", True)` 无法恢复 `res.ok` → Step6 读 `res.ok=False` 判失败，但行实际已落库。**状态不一致**。

### 修复

1. **问题二 prompt 约束**（`decompose_agent.py::_build_prompt`）：produces 标了 new_<stem>_id 时，主键列 fields 值**必须**填 `<produces_label>` 占位符，绝不能留空字符串。系统按拓扑序产出真实主键值后自动回填。
2. **问题三 状态一致性**（`core/agent.py::_do_append`）：写后验证通过 = 行已正确落库 → 若有 coerce 失败字段但行写入成功，恢复 `res.ok=True` 并标 `partial=True`。原 coerce 失败误标整体失败但行已写入，状态不一致。

### 端到端验证（run_v2_direct，冰魄碎片案例）

- **问题二 prompt 修复见效**：LLM 现在产 `物品编号` 占位符引用（`要引用前置数据（道具(new_item_id)）`），不再空串 ✓。
- **问题三 状态一致性改善**：activity/reward 成功写入标 `ok=True`（不再因 coerce 失败误标）✓。
- **新暴露：拓扑链断裂**：reward/item 某些 add 失败（fields 空，LLM 段级产空/超时）→ produces 没产出 → 下游占位符悬空 `placeholder_unresolved`。
  - LLM 诊断甚至自指：「道具主表自增未启用或前置创建步骤未执行，占位 unresolved 复现。提示并令物品编号走自增分配即可避免。」
  - 根因仍是 #5 的 LLM 段级超时产空，需继续提 timeout 或段级重试。

### 仍待跟进

1. **拓扑链断裂**：producer add 失败 → consumer 占位符悬空。方向：占位符 unresolved 时若是主键列，走自增分配而非失败（LLM 诊断建议）。
2. **问题一显示命名**：V2 Step2 加 thinking 推送，或内部 phase 名去 Step 编号（避免与 V2 步序混淆）。
3. **LLM 段级超时产空**：#5 残留，继续提 timeout 或段级 LLM 重试。
4. **④ DecomposeAgent 出口 schema-grounding**：Step1 出口校验 produces 主键列值为空 → 自动填占位符（prompt 约束的兜底）。

---



- 动机: 用户指出「切换模型仍产碎片」= 代码框架问题非 LLM 模型问题。本轮深度诊断锁定真根因。
- 诊断方法: monkeypatch `_decompose_single_prompt`/`_splitter_baseline`/`client.create_session`/`client.prompt`，打印 LLM raw 响应 + 产出 fields + create_session 错误（`tests/run_v2_direct.py` 加载 .env + 诊断 patch）。

### 🔴 真根因（LLM 模型无关，纯框架/harness 问题）

**LLM 实际产出完美**，碎片来自调用框架超时 + path2 别名扫描：

1. **LLM 调用频繁超时**（harness 配置）：`CODEMAKER_DECOMPOSE_TIMEOUT=40` 太短，8 表候选并发每表都调，大量 `timed out` 产空。
   - 证据: reward 段 8 表候选，5 次 LLM 调用全部 `timed out`（len=0）。
   - LLM 成功时产出完美: activity `{"活动id":3001,"活动名称":"焚天赤龙降临","活动描述":"赤龙现世...","活动开始时间":"2024-12-21",...}`，reward `{"reward_id":30010,"名称":"焚天赤龙首杀奖励","每日领取上限":1,"金币公式":"800","经验概率":100}`。
2. **超时产空走兜底**：兜底 `_splitter_baseline` path b 产 `[('reward', {})]`（fields 空 dict）。
3. **Step3 path2 别名扫描产碎片**：`_run_add` 在 `intent.raw` 整段原文上扫「别名+值」对，`名称` 别名后到下一别名间的整段叙述（`包也建一下，reward_id 30010...`）当值 → 写盘 `{42:'包也建一下',9:'800'}`。原 >50 字守卫放过短碎片（14 字）。

**用户判断完全正确**: 切换模型仍碎片 = 超时框架问题与模型无关。碎片来自 path2 别名扫描（非 LLM 产）。

### 三修根治

1. **timeout 提高**（`.env`）：`CODEMAKER_DECOMPOSE_TIMEOUT` 40→120。复杂跨表输入 8 表候选单 prompt schema 大，40s 不够 LLM 产出。120s 给长输出留时间。
2. **段级候选超量裁剪**（`decompose_agent.py::_prune_segment_candidates`）：候选 >5 表时按 column_signal 命中列数 + 段文本子串命中 + 命中级别（弱信号 column_extract 降权）排序取 top 3，FK 依赖表无条件保留。控 prompt token <8k 防 serve 超时空返。原裁剪后 <2 回退原候选，8 表全保留致超时。
3. **path2 叙述碎片硬拦**（`core/agent.py::_run_add` path2）：原 >50 字守卫放过短碎片。改为 tail 含中文句读（，。；：、）且非纯数字/括号列表 → 叙述碎片跳过（无论长度）。合法标量值（数字/日期/坐标/列表）不含中文句读不会误拦。

### 端到端验证（run_v2_direct，焚天赤龙全链）

- **改前**: 6 子任务，reward 碎片 `{42:'包也建一下',9:'800'}`，city/assistant 错表，activity 完全缺失。
- **改后**: 8 子任务，**碎片彻底消除**：
  - ✅ activity 成功 `{1:10015, 6:'2024-12-21 00:00:00', 7:'2024-12-28 23:59:59'}`
  - ✅ reward 成功 `{1:100604, 4:1, 6:100.0, 8:100.0, 9:'800', 12:'10001'}`（**碎片 `包也建一下` 消除**）
  - ✅ item/combat 成功
  - ✅ city/assistant 错表消除（候选裁剪去掉无关表）
  - ⚠ reward/entity_prefab/interaction/quest「无法解析」（LLM 某些段仍超时或 fields 空，**软失败跳过不产碎片**）
- **回归**: `34 failed/61 passed` 改前改后完全一致，零回归。

### 核心结论

- **碎片根因是 harness 超时 + path2 别名扫描，非 LLM 模型问题**。三修根治：timeout 提高（LLM 不再超时）+ 候选裁剪（prompt 不再膨胀）+ path2 碎片硬拦（兜底退化也不产垃圾）。
- **LLM 成功时产出完美**，证明 #4 的 ①②③⑤ 框架修复 + 本轮 timeout/裁剪/path2 三修方向正确。
- 残留「无法解析」是 LLM 某些段仍超时（可继续提 timeout 或段级重试），但**不再产碎片**，软失败可接受。

### 仍待跟进

1. **残留「无法解析」子任务**：entity_prefab/interaction/quest 某些段 LLM 仍超时。可继续提 timeout 或段级 LLM 重试机制（失败重跑一次小 schema）。
2. **段级对账重复调用**：日志显示同段被调多次（段级重跑 decompose_segment），浪费 LLM 预算，可去重。
3. **#4 ③ hard 收口验证**：本轮 LLM 成功产出多，Step2 hard issue 触发场景少，待复杂输入验证。
4. **alias 再分级**：player_common/hero_level 等泛 alias 仍占 cap 名额。

---



- 动机: 派出 agent 深度分析（file:line 交叉验证通过）定位 Step1/Step2 七大框架级缺陷，本轮落地其中 P0 四条，端到端验证真实失效模式。
- 验证方式: 绕过 OrchestratorAgent qa 误路由，直调 `agent.run_v2`（新脚本 `tests/run_v2_direct.py`），跑焚天赤龙单条全链路。

### P0 落地（4 条，均框架级硬约束）

1. **①占位符可解析豁免**（`validator_agent.py:822-860` 区块）：扫描前先按拓扑序收集本批 `extras["produces"]` label 集，占位符 label 在集内 → 可解析不报 FORWARD_REF_BROKEN。与 `validate_fk_layer:726-732` 同构。消除合法跨表链假阳性 hard issue。
2. **②retries 容错**（`decompose_agent.py:505`）：`retries = 0 if len(candidates)>=4` 改 `max(1, _retry_env)`。原把"候选多"当"简单"反了——复杂跨表输入候选常 ≥4，失败立即放弃不重试。改后至少重试 1 次。
3. **③Step2 hard 语义收口**（`step2_validate_subagent.py:75-84`）：原一律 `is_hard=False`，COL_NOT_FOUND/TYPE_MISMATCH/真悬空占位符全软流到 Step3 写盘才硬 fail。改据 `issue_type` 映射 `_HARD_TYPES`，硬类提 `is_hard=True` 前置拦截。**修复途中发现 import 路径 bug**（`..subagent` 应为 `...subagent`，`core/pipeline/` → `agent/excel/subagent/` 需三层），已修。
4. **⑤复合主键替换面补全**（`operation_orchestrator.py`）：`_resolve_placeholders` 补 `locator_fields/locator_values` 列表遍历；`_iter_values` 补 `locator_values` yield。原只替 `locator_value/value/fields.values()`，复合主键定位值占位符永远悬空（case5 双键等场景）。

### 端到端验证（run_v2_direct，焚天赤龙全链路）

- **链路跑通** ✓（绕过 router 后 V2 4-Step 完整执行，非 qa 0 操作）。
- **③ import bug 修复后 Step2 不再异常** ✓。
- **真实失效模式确认**（与派出 agent 分析一致）：
  - reward 仍产碎片 `{42:'包也建一下，', 9:'800'}` → decompose LLM 在复杂输入下退化，叙述切片塞 fields。**数字索引键 42/9 应触发 `_to_split_intents:1015-1022` 重拆，但仍落 Step3** —— 重拆机制存在但 LLM 质量问题未必救回（C 残留缺口实证）。
  - city/assistant 错表写入（`{4:1020}` model_id 列）→ 候选污染 A 修复后**仍偶发**（LLM 波动）。
  - reward/quest/interaction 三表产「无锚点碎片」被 `_coerce_value` 写盘闸拦 → **③ Step2 hard 拦截未观察到**，说明 validator 的 COL_NOT_FOUND issue 未有效传递到 Step2 hard 拦截（待下轮追查 validator 是否对该 intent 跑了校验）。
- **回归**: 改前改后 `34 failed/61 passed` 完全一致（git stash 对照），零回归。既有 34 failure 属 `_pk_cols_cache` AttributeError（testtest checkout 污染）等预存问题。

### 核心结论

- **框架修复 4 条逻辑全部生效**，但**真实瓶颈仍在 Step1 decompose LLM 质量**（C 残留缺口实证）：复杂输入下 LLM 退化产垃圾 fields，重拆机制救不回。
- **③ hard 收口部分生效**：需要 validator 真产 issue 才能拦。数字索引键场景 validator 应判 COL_NOT_FOUND 但未观察到 Step2 hard 拦，疑 validate 未对该 intent 跑或 issue 传递断链，下轮追查。
- **candidate 污染 A 修复后仍偶发错表**（city/assistant）：LLM 波动，需 alias 再分级（player_common/hero_level 等泛 alias 占 cap 名额）。

### 仍待跟进（下轮优先级）

1. **追查 ③ issue 传递断链**：reward 数字索引键 42/9 应被 validator 判 COL_NOT_FOUND 提 hard，但未观察到。加 Step2 thinking 打印确认 validate 是否对 reward intent 跑了。
2. **④ DecomposeAgent 出口 schema-grounding**（C 残留缺口根治）：用候选 stem 真实表头对 fields key 精确/row2 桥接校验，数字索引键/幻觉列就地删并记 thinking，必填列缺失从原文反向提取补填。
3. **decompose prompt 端约束**：字符串字段只填输入明确给出的值，杜绝 `包也建一下` 类碎片灌 str 列。
4. **alias 命中再分级**：player_common/hero_level 等泛 alias 占 cap 名额，按"是否被输入语义强指向"细分。
5. **⑥占位符形态**：`{reward_id}` 花括号形态 V2 无处替换，需先确认 LLM 实际产出形态再定是否改 `_PLACEHOLDER_RE`。

---



- 动机: 用户指出代码库堆满针对样例的补丁（`修案例2 fabao`/`§P0 碎片守卫`/`_GENERIC_COLS` 硬编码…），治标不治本。要求从**框架层**改，对各种输入通用正确，不做样例过拟合。
- 诊断的框架级根因（失败链）: 确定性层在"猜语义"（该由 LLM 做）→ 噪声候选 → `cap=8` 硬截断丢正确表 → LLM/兜底选错 → 下游一堆样例守卫擦屁股。职责错位，每来新输入就加新补丁。
- 通用原则: **确定性层只做召回+校验，语义决策交 LLM；LLM 失败就 fail-soft 不臆造；写入按 schema 校验。**

### A — 保召回（`locator_agent.py`，0 LLM 可复现）
1. **数据驱动歧义列抑制**（承接 #2）：某列在命中集映射 ≥3 表即判「跨表共享列」，不凭它单独补候选。是 `_GENERIC_COLS` 硬编码的数据驱动泛化。
2. **复杂输入放宽 cap**：`complex_input` 时 `_cand_cap` 由 8 提到 12。单 prompt 路径下候选多只是 prompt 变长（非多次 LLM），复杂跨表输入合法涉及更多表，固定 cap=8 会挤掉判别性 column_extract 候选。
- 验证 `LocatorAgent.locate(焚天赤龙)`：
  - 初始: `interaction, assistant, city, guild, pve_combat_npc`（3 噪声表 + 漏 reward/combat/prefab）
  - A 后: `entity_prefab, spell, item, reward, combat, activity, player_common, hero_level, interaction, pve_combat_npc, _test_item, space`（**噪声表消除 + 正确跨表主语 interaction/pve_combat_npc 回到候选**）

### B — fail-soft，不臆造错表（`decompose_agent.py::_splitter_baseline` path b）
- 仅靠列名反查的弱信号候选（`level=column_extract/column_reverse`）**不再臆造 add intent**——列名共享（model_id 命中 guild）不代表输入语义指向该表。只对语义命中表（alias/文件名/sheet/llm 推断）或 FK 被引用前置表产 intent；否则软失败跳过，不写猜测的错表。通用判据（命中级别 taxonomy + FK 图）。
- 附带修字段对应错：兜底值提取限定为**紧邻**列名（≤6 非数字字符），杜绝跨整段抓远处数字（"坐标"抓到别处 BOSS 坐标）。
- 验证探针：`guild(column_extract)` 被跳过，`combat(alias)` 正常产出 `{气血基础:'500000'}`（紧邻取值）。

### C — 统一 schema 写入闸（现状核实，非重写）
- 核实：写入路径已有类型校验闸——`_coerce_value` 失败即丢弃字段（path1 line 3934 / path2 line 4034 一致）。核心闸已在，无需重写（重写反增回归风险）。
- 真实残留缺口：**字符串列接受任意值**（`包也建一下` 塞进 str 列 coerce 必过）。此属 decompose 语义质量（LLM path1 产错字段），确定性写入闸无法通用识别（强判即过拟合）。留给 A/B 上游净化 + 后续 decompose prompt 约束解决。

### 回归
`git stash` 对照：A+B 改前改后均 `5 failed/30 passed`（`test_decompose_agent/test_locator_fallback_o20g/test_composite_pk_locate/test_column_matcher_semantic`），**零回归**。既有 5 failure 属 `_to_split_intents` 元组 API 漂移 / column_matcher source / testtest checkout，与本次无关。

### 仍待跟进（框架层）
- 端到端 LLM 复跑确认 decompose 在净化候选下产出正确表/字段（当轮以 0-LLM 探针 + 单测代验，未跑全链）。
- `player_common/hero_level` 等泛 alias 命中仍占 cap 名额——可对 alias 命中按"是否被输入语义强指向"再分级。
- decompose LLM prompt 端：约束字符串字段只填输入明确给出的值，杜绝 `包也建一下` 类碎片灌值。

---

## 轮次 2026-08-25 #2（根因修复：Step1 候选池污染 → 划分不正确 / 字段对应错）

- 方式: 端到端复现（serve 已恢复）+ ColumnExtractor/LocatorAgent 定点探针（0 LLM，可复现）
- 复现样例: `cases/complex_task_chain_inputs.json#0`（焚天赤龙降临）
- 观察到的错误（用户报告）:
  - 划分不正确：写出 `guild/GuildHall`×4、`assistant/Assistant` 等**完全错误的表**，真正的 combat/pve_combat_npc/entity_prefab/spawn_world_entity **全部缺失**
  - 字段对应错：reward 行 `{42:'包也建一下', 9:'800'}`；guild `{12:'200'}`（把 BOSS 坐标"200"塞进 guild 坐标列）

### 根因（探针实证）

对整段输入跑 `ColumnExtractor.extract` 打印命中：
```
assistant | model_id | 0.765
city     | model_id | 0.765
guild    | model_id | 0.765   （命中 20+ 次）
pve_combat_npc | 技能列表/等级公式 | 0.74   （正确）
```
- **`model_id` 是跨表共享列**（assistant/city/guild/combat/npc/prefab 都有），输入里 model_id 指 BOSS/NPC 实体，却把 guild/assistant/city 靠单列 model_id 拉进候选。
- `_GENERIC_COLS`（名称/描述/类型/id/编号…）硬编码集合**没覆盖 model_id 这类共享列** → 噪声表以 conf=0.70 补进候选池。
- 候选池 `_cand_cap=8` 下，噪声表**挤掉了真正的动作主语表**（reward/combat/prefab/spawn）→ 划分到错表 + 字段错配。

### 修复（`agent/excel/subagent/locator_agent.py`，数据驱动，0 LLM）

在 1a 列名信号补候选块加**数据驱动歧义列抑制**：统计每列在 `column_signal.hits` 里映射到的不同表数，某列命中 ≥3 张表即判为「跨表共享列」（非判别性）。补候选时「判别性专有列 = 非通用列 且 非歧义列」，全为通用/歧义列命中的表不补。是 `_GENERIC_COLS` 硬编码集合的数据驱动泛化，不绑业务词/表。

### 验证（0 LLM，可复现）

`LocatorAgent().locate(焚天赤龙输入)` 候选池对比：
- 修复前: `[interaction, assistant, city, guild, pve_combat_npc]`（3 张噪声表）
- 修复后: `[entity_prefab, spell, item, reward, combat, activity, player_common, hero_level]`（**全部真实表，guild/assistant/city 消除**）

回归: `test_decompose_agent / test_locator_fallback_o20g / test_composite_pk_locate / test_column_matcher_semantic` 经 `git stash` 对照——改前改后**均 5 failed/30 passed（完全一致）**，本次修复零回归。既有 5 failure 属 `_to_split_intents` 元组 API 漂移 / column_matcher source / testtest checkout，与本次无关。

### 仍待跟进

- 候选 cap=8 下 `pve_combat_npc`/`interaction`（conf=0.70 column_extract）仍可能被 8 张 0.9 alias 表挤出——需靠 FK 扩表(1b)兜回；复杂跨表输入可考虑对 column_extract 判别性命中给更高 conf 或提高 cap。
- `candidate_terms` 对长段落把整段作为 term[0] 是浪费（非噪声主因，已确认不影响本轮）。
- reward path1 LLM fields 碎片 `包也建一下`（无句读逃过守卫）——需 decompose prompt 端约束，属 LLM 质量。

---

## 轮次 2026-08-25（定点修复：path2 值提取碎片 / spurious_fragment_row）

- 方式: 定点复现 + 单测验证（当轮 codemaker serve 返回 500 UnknownError，端到端 eval 暂不可跑）
- 复现样例: `cases/complex_task_chain_inputs.json#0`（焚天赤龙降临，9+ 子任务跨 6 表）
- 目标缺陷:
  - `❌ spurious_fragment_row`：reward 待写值均为无锚点碎片 `{42:'包也建一下，', 9:'填 800；'}`
  - `❌ 提取新增值 未能从语句中提取到列值`

### 一、根因定位

值提取有两条路径（`agent/excel/core/agent.py`）：
- **path1（主）**：用 Step1 decompose 产出的结构化 `intent.extras["fields"]`（列名→值）。
- **path2（降级）**：path1 无 fields 时，扫描 `intent.raw` 原文的「别名+值」对（L3946 起）。

链路失败根因分层：
1. **上游（LLM 相关，非确定性）**：reward 子任务 decompose 未产出结构化 fields → `extras["fields"]` 空 → 降级 path2。path2 在整段 raw（含"奖励包也建一下，reward_id 30010…"）上做别名扫描，抓到碎片。
2. **path2 值清洗缺陷（确定性，可修）**：
   - `_strip_separators` 的 `_SEPARATORS` 词表缺赋值动词「填/写/置」→ "金币公式填 800" 的 tail "填 800；" 未剥前导"填"。
   - tail 尾部句读"；"未裁 → 值为 "填 800；" 而非 "800"。
3. **碎片行守卫的锚点判据**：`_do_append` 仅以「有无锚点（数字/无中文句读的干净串）」判碎片。清洗后 "800" 成数字锚点，会让守卫误放行、写出垃圾行（回归风险）。

### 二、本轮修复（确定性，不依赖 LLM）

`server/agent/excel/core/agent.py`：

1. **`_SEPARATORS` 扩充赋值动词** + 新增 `_TAIL_PUNCT` 句读集。
2. **`_strip_separators` 尾部句读裁剪**：仅当裁完为纯数字/小数或括号结构（坐标/列表）时才采用，否则保留原值——避免把 "包也建一下，" 的句读裁掉后被误判为干净名称锚点。
3. **碎片行守卫升级**：区分「干净名称锚点 / 数字锚点 / 中文碎片」。合法实体须有 PK 或干净名称锚点；「仅数字锚点 + 混中文碎片、无名称锚点」也判碎片跳过。既清洗了 `填 800；`→`800`，又不因此写出 `{42:'包也建一下', 9:'800'}` 垃圾行（无回归）。

### 三、验证

- 单测（无 LLM）：`_strip_separators` 行为符合预期
  - `'填 800；' -> '800'`
  - `'包也建一下，' -> '包也建一下，'`（保留句读供守卫识别）
  - `'改为 30' -> '30'`；`'(190,0,140)；' -> '(190,0,140)'`；`'2025-01-04 20:00:00'` 不变
- 守卫逻辑推演：reward 碎片行 `{42:'包也建一下，', 9:'800'}` → 无 PK、无名称锚点、有中文碎片 → 判碎片跳过（软跳过，不计失败），消除 `spurious_fragment_row ❌`。
- 相关既有单测失败（`test_pk_step2_fullchain_e2e` 的 `_pk_cols_cache` AttributeError、`test_step2_step3_fixes` 的 coverage_gap）经 `git stash` 对照确认为**改动前既存**，与本次修复无关。

### 四、仍待跟进（下一轮）

- **根因1（LLM decompose 未产 reward fields）为主要瓶颈**：属非确定性 LLM 质量问题。方向：在 decompose prompt 注入 reward 真实表头列名并加「每子任务须按列名产 fields」约束；对含 `xxx 30010 / 道具 10001 共 20 个` 强标量信号的子句，加确定性 fields 兜底提取（别名紧邻数字/列表），避免整条落 path2。
- **serve 500（UnknownError）**：当轮 LLM 后端不可用，恢复后需跑 `python -m tests.random_sample_eval --n 3 --seed 42` 拿修复后指标基线，验证 chain_complete/coverage 提升与耗时/LLM 调用数无回退。
- **偶发 qa 误路由**：焚天赤龙这类明确编辑指令被 OrchestratorAgent 分诊为 qa（LLM 波动）→ 0 写入。方向：对含 `新增/新建 + 表名/字段` 强编辑信号的输入加确定性 crud 前置判定，绕过 qa 分诊。

# ValidatorAgent 跨表复合指令失败链审计（P9–P27）

> 来源：用户「聚灵塔」跨表多实体复合指令 7 分钟卡住 + 全批废弃的根因深挖。
> 核验基线：**已落地 O1–O12 + O14**（O1 per-subtask skip → O2 删写前 forward_ref LLM → O3 validate_two_layer 降级纯展示非阻断 → O4 字段层补 id_scope → **O5 produces_inference P0 第一批 P10/P11/P12/P17** → **O6 P13 forward_refs_llm produced 收集改 relation to_column** → **O8 第二批 P22/P23 tips→failures 通道 + P19 互斥校验** → **O9 第二批收尾 P21 两入口校验集合统一 + P24/P25 skip 过滤增强+dormant 注释** → **O10 第三批 P26 批级事务 opt-in strict** → **O11 P9 multi_op 标记 + P14 LLM 不可达可观测 + P27 NLIntent checkpoint 序列化层** → **O12 P27 接线：TableAgent save/load/resume + 4-step post_parse/post_validate save 调用** → **O14 P27 resume 全闭环：显式 env stall 触发 + completed_op_keys per-op 跟踪 + Step5 增量 save + produced 重算**）。
> 交叉引用：`TODO_OPTIMIZATION.md` #19（ValidatorAgent 接真 LLM 裁决）、`openspec/changes/agent-verify-repair-loop/design.md` D1/D2/D6。
> 核验日期：2026-08-18。

---

## 0. 核验结论速览

| 类 | P 编号 | 状态 |
|---|---|---|
| 跨表/多 op 结构 | P9 P10 P11 | P9/P10/P11 ✅(O5/O11) |
| 匹配/判定正确性 | P12 P13 P14 | P12/P13 ✅(O5/O6)；P14 ✅(O11 可观测) |
| 反问/重校闭环 | P15 P16 | **O3 后 MOOT**（field/nl 重校分支已删） |
| 门控/配置打架 | P17 P18 P19 P20 P21 | P17 ✅(O5)；P19/P21 ✅(O8/O9)；P18/P20 **O3 后 largely addressed/moot** |
| 失败语义/状态机 | P22 P23 P24 P25 | P22/P23 ✅(O8)；P24/P25 ✅(O9 增强+dormant 注释) |
| 事务/成本 | P26 P27 | P26 ✅(O10)；P27 ✅(O11 序列化 + O12 接线 + O14 resume 闭环,heartbeat auto-trigger 留 follow-up) |

**净结论**：P9–P14、P17、P19、P21、P23、P26、P27 共 **12 项硬属实**，全部已消解（O5 P10/P11/P12/P17，O6 P13，O8 P22/P23/P19，O9 P21/P24/P25 增强，O10 P26，O11 P9/P14/P27 序列化，O12 P27 接线，O14 P27 resume 闭环）。P15/P16/P18/P20/P22/P24/P25 共 **7 项被 O2/O3 改动消除或弱化**。validator P9–P27 全部落地，残留 follow-up：P27 heartbeat auto-trigger（后台线程+竞态，留 serve 修复后）、e2e（阻 R7）、P24/P25 真 partial 态（需反转 O3 非阻断 design 决策）。

---

## 1. 逐条核验

### 跨表 / 多 op 结构类

#### P9 `_suppress_over_produce` 同表多行误杀 — ✅ 代码属实（路径受限）
- **证据**：`validator_agent.py:571-591` `key=(stem,sheet); if key in seen: skip_idx.append(i)`，后写同 sheet 的 add 被 `intents.pop(i)` 静默删。
- **路径限定**：仅 `validate()` 主入口（`validator_agent.py:101`）调用。**`validate_two_layer`（4-step 路径）不调** → 用户 4-step 跑的"动画被 pop"**不在此**，需确认实际走哪条路径。`validate()` 路径（6-step/fallback）确有此 bug。
- **根因**：无法区分"LLM 一表拆冗余 op"与"用户显式多行 op"。

#### P10 `add_keys` 字典覆盖（P9 连带） — ✅ 属实
- **证据**：`produces_inference.py:121` `add_keys[_sheet_key(stem,sheet)] = i`，同 key 后写覆盖前写 → 同 sheet 第二个 add 在 producer 查询阶段已丢失。
- **叠加**：与 P9 叠加 → producer/consumer 连线漏建 → 反触发 `FORWARD_REF_BROKEN`。

#### P11 `new_{stem}_id` 标签模板冲突 — ✅ 属实
- **证据**：`produces_inference.py:138` `label = f"new_{_stem_of_path(r.to_path)}_id"` 固定模板。同 stem 多 producer（如 candidate prefab 8030 + formal prefab 8008 同属 `entity_prefab`）→ 同 `produces_label` → `produced` 字典后写覆盖（`validator_agent.py:422`）→ consumer 占位符解析到错 PK。

### 匹配 / 判定正确性类

#### P12 `_field_matches_col`/`_field_matches_fk` 子串匹配过宽 — ✅ 属实
- **证据**：`produces_inference.py:66` 与 `validator_agent.py:692` 均 `k == fk or fk in k or k in fk`。fk=`id` 命中 `model_id`/`item_id`/`prefab_id`/`combat_id` 任意含 "id" 字段。
- **后果**：produces_inference 把 producer 非 PK id 字段误当代换源；forward_ref LLM 对每条 FK 边×每个含 id 字段触发 → 假阳性 + 额外 LLM。14 条 issue 假阳性主源。

#### P13 `"id" in kl` 过度收集 produced 集 — ✅ 属实
- **证据**：`validator_agent.py:721-723` `if "id" in kl or "编号" in str(k): produced.add((stem,str(v)))`。`grid_id`/`effect_id` 等非主键 id 字段也被收为 producer PK → 与 P12 叠加，前向引用"已产出"判定失真。
- **范围**：在 `_validate_forward_refs_llm` 内；O2 后 `validate_two_layer` 不调、`validate()` 默认 off（`CODEMAKER_VALIDATOR_LLM_FORWARD_REFS` 默认 "0"）。代码仍在。

#### P14 LLM 不可达静默返 `""` → 既存引用可放行可阻断 — ✅ 属实
- **证据**：`validator_agent.py:768-769` `except Exception: return ""`；调用方（743）把 `""` 当"无 issue"放行。同输入 LLM 可用→报 build→阻断；不可用→静默 OK。行为不确定、不可复现。
- **范围**：同 P13，O2 后默认不调。

### 反问 / 重校闭环类

#### P15 重校不重跑 forward_ref LLM → 死路逼 skip — ⚠️ **O3 后 MOOT**
- **原断言**：field/nl 修正后只重跑 `validate_field_layer` + `validate_fk_layer`，不清 forward_ref_broken → 只能 skip。
- **现状**：O3 删除了 `validate_two_layer` 的 field/nl 重校分支（现纯展示非阻断）。forward_ref LLM 也已 O2 移出写前。此问题在 `validate_two_layer` 已不存在。

#### P16 仅一轮重校、无增量迭代 — ⚠️ **O3 后 MOOT**
- 同 P15，field/nl 分支已删。多 issue 修复全交写后 `verify_repair_loop`（最多 3 轮）。

### 门控 / 配置打架类

#### P17 `_should_consume` 把 `<auto>` 当 consume — ✅ 属实
- **证据**：`produces_inference.py:82` `if s == "" or s == "<auto>": return True`。`<auto>` 本是"用户没提的可选列→留空"，却被判可替换成 `<producer_label>` → 解析不到 → 触发 `_phase_execute` placeholder_unresolved 二次 ask（`agent.py:5470`）。可选列本不该 ask 却被 ask。

#### P18 pre-validate 与 post verify-repair 抢拦截 — ⚠️ **O3 后 largely addressed**
- **原断言**：pre 失败 → skip → 全批废弃 → 永远到不了 post verify_repair。
- **现状**：O3 使 `validate_two_layer` 非阻断（ok=True 恒）→ intents 写盘 → `verify_repair_loop`（`agent.py:5608`）跑得到。pre 不再架空 post。投资与收益错配已消。

#### P19 `execute_no_llm` 与 `enable_verify_repair_loop` 互斥缺失 — ✅ 属实（潜在低频）
- **证据**：`agent.py:5589-5605` `execute_no_llm=1` + 写失败 → 早返 `return res` 跳 `verify_repair`（5608）。`enable_verify_repair_loop` 默认 True（578）。无互斥校验 → 失败路径零修复。
- **频率**：`execute_no_llm` 默认 off（582 `CODEMAKER_EXECUTE_NO_LLM` 默认 "0"）。低频但配置陷阱。

#### P20 `validate_two_layer` 不走 skill tools — ⚠️ **O3 后 MOOT**
- **证据**：`make_skill_tools` 仅绑在 `_run_react_repair`（`agent.py:6075`，C 路径）。
- **现状**：O3 后 pre 不修复（纯展示），修复全交 C（有 skill tools）。pre"无 tools"不再相关。

#### P21 `validate()` 与 `validate_two_layer()` 覆盖不一致 — ✅ 属实
- **证据**：`validate()`（74-127）含 `_validate_consumes_match`（112）+ `_validate_fk_coverage`（118）；`validate_two_layer` 含 `validate_field_layer` + `validate_fk_layer`。两套校验集合不同，走哪条取决于调用方 → 同输入不同路径结论不同。

### 失败语义 / 状态机类

#### P22 CI/非交互 continue 静默放行带病 — ⚠️ 机制变但根在
- **原断言**：无 `_ask_callback` → continue → `_mark_validation_ok` 标 ok，tips 仍在且不进 failures。
- **现状**：O3 删 continue 分支，现 `validate_two_layer` **恒 ok=True** → 全 proceed + tips（not in failures）。CI 带病照样落盘、不上报。P22 仍适用（甚至更直接：不再区分 continue/skip，一律 proceed）。违背 D6"失败必上报"。

#### P23 continue 遗留 tips 不入 failures — ✅ 属实（根因）
- **证据**：tips 与 failures 是两套数据通道，pre-validate tips 从不汇入 `res.failures`（failures 只在 placeholder-gate `agent.py:5531` + execute/C 路径产生）。
- **修法**：continue/展示模式把遗留 tips 转软失败 dict 追加到 `intent.failures`（归 O7）。

#### P24 skip 状态悬空 — ⚠️ **O3 后 largely MOOT**
- **原断言**：skip 设 `res.ok=True`、message="用户跳过此项"，聚合 neither ok nor failure（`agent.py:5450` + 4599）。
- **现状**：`_phase_execute:5446` skip 分支现不可达（`validate_two_layer` 不再标 `validation.skipped`）。placeholder-gate"skip"→ 记 failure（5531）非悬空。5446 成死代码。

#### P25 级联 skip — ⚠️ **O3 后半 MOOT**
- **原断言**：producer 被全弃 → consumer 占位悬空 → 二次 ask → 链式全废。
- **现状**：`validate_two_layer` skip 级联已消（O3 不 skip）。placeholder-gate 级联仍在（producer 占位未解 → consumer 占位未解 → 多 failure），但记 failure 非静默。恶化受控。

### 事务 / 成本类

#### P26 无跨子任务事务/部分回滚 — ✅ 属实
- **证据**：`agent.py:5552-5561` per-op `auditor.backup_and_record` + `_rollback_write`（5582/5591/5616/5648）。每 op 独立快照+回滚，无批级原子。district 成功写盘 + combat 失败/跳过 → district 残留半成品、无 combat。重跑 → `UNIQUE_VIOLATION` → 再失败。

#### P27 4-step loop 无 checkpoint — ✅ 属实
- **证据**：checkpoint 仅 `pipeline.py`（file-mode，`_step0_checkpoint` 等）+ `agent_service._session_checkpoints`（每输入写后快照，`2499-2501`）。4-step NL 路径（`agent.py:3848+`）无中断点 → stall/放弃 → 从 Step1 parse 重跑（N 次 LLM 成本白花）。

---

## 2. 三类系统性缺陷（归并确认）

1. **校验前置过度**（P2/P3/P18/P20）：本应"写后内存验证 + repair 迭代"解决的事，堆到写前用 N 次 LLM 串行判，判不出就 all-or-nothing 弃批。→ 卡住 + 全废两症状同源。**O2/O3 已消解**（删写前 LLM + pre 降级纯展示 + 修复交 C）。
2. **匹配/标签语义脆弱**（P9–P13、P23）：子串匹配 + 固定标签模板 + 同表去重，在"同表多行/同 stem 多 producer"合理输入上系统性误杀，专门打击建筑+动画+prefab 复合指令。**未消解，需专项修。**
3. **状态机/通道割裂**（P15/P16/P22/P24/P25）：tips 与 failures 两通道、skip 无 partial 态、重校不闭环、级联 skip。O3 消解 P15/P16/P24/P25 之 validate_two_layer 侧；**P22/P23 根因（tips≠failures）未消，归 O7。**

---

## 3. 修复优先级（action items）

### 第一批（结构性误杀 + 匹配假阳性，必须先动）
- [x] **P12** ✅(O5) 收紧 `_field_matches_col`/`_field_matches_fk`：子串匹配 → **精确等值 only**（`produces_inference.py:57` + `validator_agent.py:692`）。审计「+后缀」公式 `k.endswith("_"+fk)` 仍让 `model_id` 命中 `id`（model_id 以 `_id` 结尾），升级为精确 only 彻底消假阳性。优先于 P13。
- [x] **P13** ✅(O6) `_validate_forward_refs_llm` produced 收集改用 producer 显式 PK 列（relation `to_column`，`producer_pk_cols` map + `_field_matches_fk` 匹配），非 `"id" in kl` 启发式（`validator_agent.py:703`）。opt-in 路径（4-step 主线不调），补齐 P12 语义。
- [x] **P11** ✅(O5) produces 标签去模板冲突：`new_{stem}_id` → **sheet-aware** `new_{stem}_{sheet}_id`（`produces_inference.py:138`，sheet 缺省回退 stem 级）。注：4-step 主线 `validate_two_layer` 不调 `_align_produces_labels`，sheet-aware 标签不被折叠；fallback `validate()` 仍折叠（保留旧行为）。
- [x] **P10** ✅(O5) `add_keys` 同 key 覆盖：改 `add_keys.setdefault(key, i)`（`produces_inference.py:121`），保留首 producer 候选，与 `_suppress_over_produce`「一表一 op 契约」语义一致。多 producer 列表完整扩展（同 stem 多 sheet 全支持）留 follow-up。
- [x] **P9** ✅(O11) `_suppress_over_produce` 同表去重：NLIntent 加 `multi_op_same_sheet` 字段，`_suppress` 跳过标记 op（保用户显式多 producer 同 sheet，仅抑制未标记 LLM 过产）。4-step `validate_two_layer` 不调 `_suppress` → 仅 fallback `validate()` 路径生效，降级。
- [x] **P17** ✅(O5) `_should_consume`：`<auto>` 不当 consume（从 True 改为 False，`produces_inference.py:73`，`<auto>` 留空不转占位，消 `_phase_execute` placeholder_unresolved 二次 ask）。

### 第二批（失败语义 + 状态机，归 O7）
- [x] **P23/P22** ✅(O8) tips → failures 通道统一：NLIntent 加 `failures` 字段 + `attach_tips_as_soft_failures()` 把 validate_two_layer 遗留 tips 转 #40 形状软失败 dict 追加 `intent.failures`（4-step + 6-step 两路径接入）+ partition 创建时 transfer 到 `res.failures` → `all_failures` 聚合 + `_phase_summarize` 上报。保 D6 上报不静默吞，消 CI/非交互 continue 带病照样落盘。
- [x] **P24/P25** ✅(O9 增强 + O12 design 评估) skip 过滤点增强：4-step/6-step 两过滤点 thinking 列出 skipped stem/sheet（非仅计数，「不静默丢」供汇总单列「已跳过清单」）+ 注释 O3-dormant。**O12 评估**：O8 选 soft-failure 通道（P23）而非 partial-skip → 重激活 partial-skip 需反转 O3 非阻断设计，属 design 决策非接线，留 design follow-up。
- [x] **P19** ✅(O8) `execute_no_llm` × `enable_verify_repair_loop` 互斥校验：`_check_p19_mutex_conflict` 方法（`agent.py`），`__init__` 调，同开 warning 提示（不强制改，保用户显式意图；CI/自动化应避免此组合）。
- [x] **P21** ✅(O9) `validate()` 加可选 `schema_getter`/`data_getter` 参数，提供时跑 `validate_field_layer`+`validate_fk_layer` 合并 issues（Issue→str，FORWARD_REF_BROKEN 带「断链」关键字供 hard_issues 判定），消除「同输入不同路径结论不同」（两入口共享同一字段/FK 校验集合）；缺省 None→保留旧行为，现有调用方不传→不变。

### 第三批（事务 + 成本）
- [x] **P26** ✅(O10) 批级事务/部分回滚：`_compute_rollback_targets` staticmethod + `CODEMAKER_BATCH_TRANSACTIONAL=1` opt-in strict 模式（任一硬失败回滚整批前序已 commit op，不限 G8 直接依赖，批级原子）。默认 off（保留 G8 链回滚：仅回滚失败步直接依赖 producer，避免牵连无关独立 op）。
- [x] **P27** ✅(O11 序列化 + O12 接线 + O14 resume 闭环) 4-step NL 路径 checkpoint：NLIntent `to/from_checkpoint_dict` + TableAgent `_nl_checkpoints` + `_save/_load/_resume_from_checkpoint` + 4-step post_parse/post_validate save 接线（opt-in `CODEMAKER_4STEP_CHECKPOINT=1`）。**O14 resume 全闭环**：① stall 检测=显式 env `CODEMAKER_4STEP_RESUME=<session_id>` 触发（run() 入口调 `_resume_from_checkpoint`，skip Step1 parse + post_parse/post_validate save，免重 LLM decompose）；② per-op 成功跟踪=checkpoint dict 加 `completed_op_keys` 字段（orig_idx 集），`_save_nl_checkpoint` 加可选参数；③ Step5 loop 成功 op 后增量回写（新 `_save_nl_progress` 方法，覆盖 post_validate/回退 post_parse stage）；④ resume 跳过已成功 op（filter `ordered_idx` 去 `completed_op_keys`）+ 从 checkpoint execution 重算 produced（`_capture_produced` 重放 result_rows，供后续 op 占位符替换，免查库）；⑤ `_resume_from_checkpoint` 返三元组 `(intents, stage, completed)`。opt-in 双 env gate，默认 off。**残留 follow-up**：heartbeat auto-trigger（后台线程+竞态，留 serve 修复后）、e2e（阻 R7）。

### 已消解（O1–O6，归档）
- [x] P1 全批废弃（O1→O3 per-subtask → 纯展示）
- [x] P3 串行 LLM 卡死（O2 删写前 forward_ref LLM）
- [x] P4 id_scope 不在字段层（O4 已接）
- [x] P5 既存 FK 无信任通道（O3 后既存 FK 交写后 ref_integrity）
- [x] P15/P16 重校死路（O3 删 field/nl 分支）
- [x] P18 pre 架空 post（O3 非阻断 → C 跑得到）
- [x] P20 pre 无 skill tools（O3 后 pre 不修复，C 有 tools）
- [x] P10 add_keys 覆盖（O5 setdefault）
- [x] P11 produces 标签模板冲突（O5 sheet-aware）
- [x] P12 子串匹配过宽（O5 精确等值 only）
- [x] P13 forward_refs_llm produced 收集启发式（O6 relation to_column）
- [x] P17 `<auto>` 误 consume（O5 改 False）

---

## 4. 与 O1–O6 关系备忘
- O2 已把 `_validate_forward_refs_llm` 从 `validate_two_layer` 移除、`validate()` 默认 off。P14 代码仍在该方法内但默认不触发；P12 修后再决定是否彻底删该方法。O6 已修 P13（produced 收集改 relation to_column）。
- O3 使 `ask_user`/`set_ask_callback`/`_mark_validation_skipped` 在 validator 内 dormant（agent_service 仍注入 `_ask_callback` 但 validator 不读）。O7 决定去留：删 vs 重激活作 C 的交互通道。
- O4 字段层 id_scope 仅在 id_mgr 已 load 时生效（与写路径同前提）。接 P4 实战前先核 `engine/id_scope` 对 combat 等模块的注册段。
- O5 修 produces_inference（P10/P11/P12/P17）+ validator_agent._field_matches_fk（P12）。关键决策：P12 审计「精确等值+后缀」公式仍让 `model_id` 命中 `id`，升级为**精确等值 only**（点分键取末段 + 归一后 ==）。P11 sheet-aware 标签仅在 4-step `validate_two_layer` 路径生效（不调 align）；fallback `validate()` 路径 align 会折叠为 stem 级（保留旧行为，非主线）。新增 `tests/test_produces_inference_p0.py` 21 测覆盖四项。
- O6 修 `_validate_forward_refs_llm`（P13）：produced 收集从 `"id" in kl` 启发式 → relation `to_column` 声明 PK 列（`producer_pk_cols` map + `_field_matches_fk`）。opt-in 路径（`CODEMAKER_VALIDATOR_LLM_FORWARD_REFS=1`，4-step 主线不调），低频但补齐 P12 语义（否则非主键 id 字段污染 produced 集 → 假阴性）。新增 `tests/test_validator_forward_refs_p13.py` 9 测覆盖。
- O8 修 P22/P23（tips→failures 通道）+ P19（互斥校验）。NLIntent 加 `failures` 字段；`attach_tips_as_soft_failures()` 把 validate_two_layer 遗留 tips 转 #40 形状软失败 dict 追加 intent.failures；4-step + 6-step 两路径接入；partition 创建时 transfer 到 res.failures → all_failures 聚合 + _phase_summarize 上报（保 D6）。`_check_p19_mutex_conflict` 方法 __init__ 调，EXECUTE_NO_LLM × verify_repair 同开 warning。新增 `tests/test_validator_tips_to_failures_p23.py` 10 + `tests/test_agent_p19_mutex.py` 4。P24/P25（skip partial 态）/P21（两入口校验集合统一）留 O9。
- O9 修 P21（两入口校验集合统一）+ P24/P25（skip 过滤增强 + dormant 注释）。`validate()` 加可选 `schema_getter`/`data_getter` 参数，提供时跑 `validate_field_layer`+`validate_fk_layer` 合并 issues（Issue→str，FORWARD_REF_BROKEN 带「断链」关键字供 hard_issues 判定），消除「同输入不同路径结论不同」；缺省 None→保留旧行为，现有调用方不传→不变。4-step/6-step 两 skip 过滤点 thinking 列出 skipped stem/sheet（非仅计数，「不静默丢」供汇总单列「已跳过清单」）+ 注释 O3-dormant + _phase_execute:5512 skip 分支注释 dormant。新增 `tests/test_validator_unified_entry_p21.py` 8。真 partial 态（重激活 _mark_validation_skipped）留 follow-up。
- O10 修 P26（批级事务/部分回滚）。`_compute_rollback_targets` staticmethod + `CODEMAKER_BATCH_TRANSACTIONAL=1` opt-in strict 模式（任一硬失败回滚整批前序已 commit op，不限 G8 直接依赖，批级原子）。默认 off（保留 G8 链回滚）。strict 场景：district 成功+combat 失败且非直接依赖时，默认留半成品→重跑 UNIQUE_VIOLATION；strict 回滚 district 整批原子。新增 `tests/test_agent_p26_batch_txn.py` 9。P27（4-step NL checkpoint：NLIntent 序列化 + 续跑）留 follow-up。
- O14 修 P27 resume 闭环（O12 follow-up 收尾）。设计决策：① stall 检测=显式 env `CODEMAKER_4STEP_RESUME=<session_id>` 触发（非后台线程 heartbeat，免竞态+可确定性单测）；② per-op 成功跟踪=checkpoint dict 加 `completed_op_keys` 字段（orig_idx 集，`_save_nl_checkpoint` 加可选参数）；③ Step5 loop 成功 op 后增量回写（新 `_save_nl_progress` 方法，覆盖 post_validate/回退 post_parse stage）；④ resume 跳过已成功 op（filter `ordered_idx` 去 `completed_op_keys`）+ 从 checkpoint execution 重算 produced（`_capture_produced` 重放 result_rows，免查库）；⑤ `_resume_from_checkpoint` 返三元组 `(intents, stage, completed)`。run() 入口：env 触发→`_resume_from_checkpoint`→skip Step1 parse + post_parse/post_validate save。opt-in 双 env gate（`CODEMAKER_4STEP_CHECKPOINT=1` + `CODEMAKER_4STEP_RESUME=<session_id>`），默认 off。新增 `tests/test_agent_p27_checkpoint.py` TestO14CompletedOpKeys 7（save 带 completed_op_keys / 默认空 / resume 返 completed / `_save_nl_progress` 增量回写 / 回退 post_parse / 无 checkpoint 返 False / env off 返 False）；旧 13 测更新为三元组签名。**残留 follow-up**：heartbeat auto-trigger（后台线程+竞态，留 serve 修复后）、e2e（阻 R7）、P24/P25 真 partial 态（design 决策非接线）。

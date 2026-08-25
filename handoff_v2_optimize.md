# 全链路优化 Handoff Prompt（V2 Excel Agent 4-Step）

## 一、项目背景

优化 **4-Step V2 Excel Agent pipeline**（Step1 parse → Step2 validate → Step3 execute → Step4 conclude），通过自然语言操控游戏配置 Excel。

- 环境：codemaker serve（HTTP 127.0.0.1:8666，Basic Auth codemaker/CMniubi2026）
- 模型：netease-codemaker/deepseek-v4-flash
- CODEMAKER_EXCEL_PIPELINE_V2=1
- caveman skill（lite 级：精简但保留完整句子/冠词）

## 二、测试用例

"焚天赤龙降临"（738 字符）—— 创建 8 个跨表实体：
1. 限时世界 BOSS 活动（activity_id 3001）
2. 首杀奖励包（reward_id 30010）
3. 保底掉落池（pool_id 3001）
4. BOSS 战斗（model_id 1200，技能 9101-9104）
5. 引导 NPC 对话树（2 对话 + 3 选项 + 跳转）

## 三、核心约束（用户强调，不可违背）

- **提升 agent 泛化能力，禁止测试用例硬编码**
- 所有标准基于通用规则（列类型/值长度/值形式/列名约定/FK 关系图），不绑定业务关键词/表名/测试用例
- 接受错误修复建议后要能全链路贯通

## 四、关键认知

### 4.1 ok 判定机制
- `res.ok = res.ok_from_steps()` = `all(step.success)` —— 任一 step.success=False 翻转整个 intent 为失败（即使行已写入）
- 三态 ok：True（成功/n_ok）/ False（失败/n_fail）/ None（pending，不计失败）

### 4.2 关键模块
- `enum_resolver.resolve_label(stem, sheet, column, value)` → int code 或 None（中文枚举标签如"史诗"/"节日" → 数字码）
- `ColumnExtractor` 信号（sig_by_stem 命中）、FK 边（producer/consumer）、占位符拓扑回填（`<name>` 解析）
- Splitter baseline（零-LLM fallback，cross_table_splitter 11 模板 + ColumnExtractor path b）
- LLM-as-judge、按根因 step 定向回退、anti_patterns.yaml 反馈环

### 4.3 正确性定义（无标准答案，只有 NL + schema + FK 图）
1. **完整性**：NL 所有信息点落库
2. **定位正确**：值落对表对列
3. **无灌值污染**：数值列无中文/碎片；字符串列无截断特征
4. **必填完整**：schema required_fields 无空缺
5. **FK 链 + 占位符接通**：FK 列值存于目标表；占位符 `<xxx>` 无残留

### 4.4 校验分层（漏斗过滤）
- **L0 规则闸**（零 LLM）：类型/截断/FK/占位符/必填/PK —— 覆盖 80% 关切
- **L1 AI 判官**（1 次 LLM）：NL 覆盖度/值合理性
- **L2 人工 gate**：L0/L1 疑点时触发仲裁

## 五、已完成的改动（9 处，已编译/验证）

### P0（高危，3 处）
- **改动4 P0-1**：`agent.py:_run_add` fields 路径（~L3810）—— 移除无类型检查的叙述跳过块，改靠 match + `_coerce_value`（enum 联动）。注释 `§P0-1 修复`
- **改动1 P0-2**：`parse_agent.py:_scrub_narrative_scalar`（~L263-321）—— 清空前加 enum 联动 + 中文标点判定（无标点非明显叙述则保留）。注释 `§P0-2 修复`
- **改动8 P0-3**：`agent.py:_phase_execute`（~L6895）—— 空内容短路带 fields-empty 前提：fields 空则 rollback + `res.ok=None`（三态）；非空则降级软 ask。注释 `§P0-3 修复`

### P1（中危，6 处）
- **改动5**：`agent.py:_run_add` coerce/id_scope 失败（~L3927）→ `add_thinking` + `needs_user_fill.append` + `failed.append`。注释 `§P1-5 修复`
- **改动6**：`agent.py:_run_add` match 失败（~L3832）→ 同上。注释 `§P1-6 修复`
- **改动6 ratio**：`agent.py:_run_add` post-loop（~L3948）—— `len(failed) > _total_fields * 0.5` 则 `res.add("field_fail_ratio", False)` 并 return
- **改动7**：`agent.py:_run_add` path2（~L4007）coerce/id_scope → `add_thinking` + `needs_user_fill.append`。注释 `§P1-7 修复`
- **改动2**：`decompose_agent.py:_col_type_for`（~L618-660）—— 全表 fallback + 类型歧义守护（多表类型不一致则返空保守）
- **改动3a/3b**：`decompose_agent.py:_splitter_baseline` path b（~L346-395）—— 类型感知提取（num 列提数字/str 列提引号内容）+ 放宽 intent 生产（无信号才跳过）

## 六、chilong20 基线（真实进度）

- failures=1（真失败，reward 灌值被 ratio 阈值正确暴露，非"假0"）
- reward 行 delta +0（碎片未写 DB，L0 风险点已防住）
- 灌值守卫清空=1（中文未被批量杀）
- VERDICT：5 OK + 1 FAIL（墙钟 292.8s 超 180s）
- 历史：failures 9→7→1→0(假)→1(真)

## 七、当前待解问题（卡点定位）

### 7.1 reward 灌值根因（真失败）
chilong20 暴露：Reward 行105 写入 `={2: '叫焚天赤龙，model_', 42: '包 30010，输了和平局都不给。'}` —— **Step1 切分错**：把叙述碎片当 reward_id 列值。

### 7.2 墙钟 292.8s 超 180s 目标
LLM 调用慢，StepAIEnhancer timed out（log L148）。

### 7.3 L0 规则闸未落地
正确性校验仍靠 VERDICT 离线判，未进流水线 Step4 写后校验 + 回退循环。

## 八、文件路径表

| 文件 | 角色 |
|---|---|
| `server/agent/excel/core/agent.py` | 主执行（_run_add/_phase_execute/_do_append/ok_from_steps） |
| `server/agent/excel/parse_agent.py` | Step1（_scrub_narrative_scalar/_assemble/parse_baseline） |
| `server/agent/excel/subagent/decompose_agent.py` | 分解（_col_type_for/_splitter_baseline） |
| `server/agent/excel/core/pipeline/step4_conclude_subagent.py` | Step4 汇总 |
| `server/agent/excel/core/enum_resolver.py` | 枚举解析 |
| `server/agent/excel/core/execute_dispatch.py` | 分发（L63 `res.ok = res.ok_from_steps()`） |
| `server/tests/run_cross_table_fullchain_real.py` | runner（pk_conflict→accept_suggest，其他→skip） |
| `temp.md` | 改动全记录（锚点/风险/优先级/验证方法） |

## 九、验证命令

```cmd
cd c:\Users\wuzhixian\Desktop\testtest\server
python -u -m tests.run_cross_table_fullchain_real > tests\reports\_probe_chilong21.log 2>&1
```
runner 策略：pk_conflict → accept_suggest；其他 → skip（避免阻塞）

## 十、下一步任务

**全链路寻找卡住的地方**，重点：
1. Step1 切分：reward 叙述碎片为何被当 reward_id 列值？`_scrub_narrative_scalar` + `_splitter_baseline` path b 哪里漏判？
2. 占位符回填链：`<new_combat_id>` 等为何悬空（chilong17 log L88）？producer 失败还是 consumer 序错？
3. L0 规则闸：哪 6 条规则（R1-R6）能落地到 Step4？现有代码有无钩子？
4. 墙钟：哪步最慢？StepAIEnhancer 超时根因？
5. 失败闭环：`failed.append` 后 Step4 是否真入 failure 清单？回退循环是否触发？

定位后按根因定向修复（回 Step1/Step上限 3 次），保持通用（不硬编码）。

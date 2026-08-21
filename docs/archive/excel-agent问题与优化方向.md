# Excel-Agent 全流程问题诊断与优化方向

> 用例：`quest_npc_double_option.json[0]`（新增 NPC + 双选项对话 + 采集支线 + 完成奖励 + 回城卷轴奖励，期望 7 条，实跑 matched=1，quest.xlsx / reward.xlsx 全丢失）
> 结论先行：**该 excel-agent 在"意图理解 + 表定位"环节存在系统性严重缺陷。当前是"规则优先、LLM 兜底"的反向架构，规则既过强又过浅，LLM 又被规则误判后短路掉、或一旦触发就慢得不可用。**

---

## 0. 一个关键结论：这次到底有没有调用 LLM？

你问的核心问题。答案是 **取决于走哪条分支**，而且两条分支都有问题：

| 运行 | 意图理解是否调 LLM | 结果 |
|---|---|---|
| **本次跑通的那次**（8 子任务，`[Step5 16:06:28]`） | **完全没调 LLM**，纯规则路径 | 跑得很快（秒级）但**结果错**：quest/reward 全丢，matched 仅 1/7 |
| **卡死在 `[step3]` 的那次**（156s、143.8k Tokens、被中止） | **调了 LLM，且上下文巨大** | 慢、贵、甚至卡死 |

为什么同一个用例会出现两种命运？因为入口的 `LocatorAgent.locate`：当规则给出"一个高置信候选"时，`is_cross_table=False`，整条 LLM 链被跳过（详见根因 R1/R4）；而当规则恰好给出 ≥2 候选时，又触发 `DecomposeAgent` + `parse_multi` 巨型 LLM 调用（根因 R7）。两条路径都不可靠。

### 0.1 "跑通那次"没调 LLM 的证据链

`agent.py:3662-3683` 三 agent 链触发条件：`CODEMAKER_AGENT_CHAIN=1 && _locator_agent && _decompose_agent` 且 `locator_result.is_cross_table`（`subagent/locator_agent.py:84-86` = `len(candidates)>=2 or fk_edges`）。

对本用例输入，`TableLocator` 5 级命中：
- 2 级别名：`"奖励" → formula_nested.xlsx`（`alias_mapping.json:34`），命中"完成奖励"，置信 **0.90**
- 5 级列名：`reward.xlsx / Reward` 表头 `reward_id` 出现在文本里，置信 **0.60**
- quest：无任何命中（"支线/采集/完成奖励/目标类型/目标数据" 都不匹配任何 alias、表头、regex，详见 R2/R3）

`TableLocator` 仅保留 best + 与 best 差距 ≤0.05 的 ambiguous（`table_locator.py:226-243`）。0.90 vs 0.60 差 0.30 → `ambiguous=[]`，`candidates=[formula_nested]` 单元素 → `is_cross_table=False` → **`DecomposeAgent`（唯一会调 LLM 的分解器）整条不执行**，`_llm_resolve` 也不触发（`locator_agent.py:136` 要求歧义或无命中）。

随后走 `agent.py:3685-3701` 规则安全网：`detect_cross_table_action` 命中 `npc_dialogue` 模板（`cross_table_splitter.py:253-256`），硬编码吐出 8 条 op（NPC/prefab/interaction/conv/option/spawn），**既不碰 quest.xlsx 也不碰 reward.xlsx**（根因 R6）。8≥2，`_llm_chain_decompose` 也不触发（`agent.py:3708` 要求 <2）。

→ 全程零 LLM、零意图理解、纯模板展开 → 7 条期望只 matched 1。

### 0.2 "卡死那次"调了 LLM 的证据

`[step3] service.chat` 卡 156s + 143.8k Tokens = 典型 `parse_multi` / `_llm_chain_decompose` 行为：`codemaker_parser.py:751` 单次 `client.prompt(timeout=90)`，新增链多次调用累积数分钟（`run_one_case.py:18` 注释自承）。上下文把全部表结构塞进去 → 上百 k token → 慢且易超时（根因 R7）。

---

## 1. 全流程逐环节问题诊断

```
用户输入
  │
  ├─ [意图理解] nl_parser / multi_intent_splitter / cross_table_splitter  ← 100% 规则（R2/R3/R6）
  ├─ [表定位]   LocatorAgent → TableLocator 5 级                           ← 规则过强、误短路（R1/R3/R4）
  ├─ [分解]     DecomposeAgent (LLM)  ← 仅 is_cross_table 时触发，且只能分解 locator 已选中的表（R5）
  ├─ [校验]     ValidatorAgent        ← 100% 规则，LLM 裁决未实现（R8）
  ├─ [排序执行] OperationOrchestrator._topo_order / _capture_produced   ← OK（这一段没问题）
  └─ [写库]     _phase_execute → _run_add                                  ← OK
```

坏在**前 4 环**（理解→定位→分解→校验），后面执行/拓扑/写库是健康的（这次 8 条 op 的 produces/consumes 占位替换、拓扑、回填都正确）。

---

## 2. 根因清单（带 `file:line` 证据，按严重度排序）

### R1【致命】别名污染：`"奖励" → formula_nested.xlsx`
- `alias_mapping.json:34`：`"奖励": "formula_nested.xlsx"` —— **错路由**。
- `alias_mapping.json:96`：只有 `"特殊奖励": "reward.xlsx"` 才对。
- 后果：输入"完成奖励"被 0.90 高置信路由到 `formula_nested`，**既误选目标表，又因 0.90≥阈值把 reward(0.60 列名命中)踢出候选集**，连带关闭歧义分支（根因 R4），让 LLM 连被咨询的机会都没有。

### R2【致命】quest 表完全不可发现
- 文本用词：`支线 / 采集 / 完成奖励 / 目标类型 / 目标数据 / reward_id 10088 / group_id 320`。
- 别名表 `alias_mapping.json:36-38,83` 只认字面 `任务 / 任务组 / 任务目标 / 剧情` —— 输入里**一个都没有**。
- `cross_table_splitter.py:277` 的 quest 检测要求 `新增/增加/添加` 前缀 + 紧跟 `任务`（`_ADD_PREFIX` + `(?:支线|主线…)?任务`），输入 `"接下采集支线'寒玉草采集'"` 既无前缀也无常量"任务" → 不命中。
- `skill_context.pre_route` 的关键词路由里**根本没有 quest**，连喂给 LLM 的提示词都不提它存在。
- → quest 在规则路径下**结构性不可达**。

### R3【严重】5 级列名匹配是单向精确子串
- `table_locator.py:184` 实现是 `if hn in text`：仅当"完整表头是输入的子串"才命中。
- quest 表头 `任务目标类型 / 任务目标参数`，输入写 `目标类型 / 目标数据` → 不命中（方向反了，且不容错）。
- → 真正该路由上来的表，恰恰因为措辞变体被漏。

### R4【严重】歧义消解短路把链整条关掉
- `subagent/locator_agent.py:146-149`：LLM 选定一个 stem 后，候选被过滤成 `{llm_stem} ∪ {conf≥0.90}`。
- `is_cross_table` 定义 `len(candidates)>=2 or fk_edges`（`locator_agent.py:84-86`）。
- 单候选 + 无 FK 边（FK 边要求两端 stem 都在候选里，`locator_agent.py:247`）→ `is_cross_table=False` → `agent.py:3665` 跳过 `DecomposeAgent`。
- → 一个高置信但**错误**的别名命中（如 R1 的 formula_nested），就能把后面整条 LLM 分解链一次性关掉。

### R5【严重】DecomposeAgent 不能发现表，只能分解已选中的表
- `subagent/decompose_agent.py:52-215`：prompt 约束"按候选表 schema 分解为每表一 op"，器物来自 `locator_result.candidates`。
- `subagent/locator_agent.py:217` 的提示词里**确实**写了"任务 → quest"路由线索，但该提示词只在歧义/无命中时（`locator_agent.py:136`）才喂给 LLM —— R1 的 0.90 别名让这条件永不成立，quest 的线索永远送不出去。
- → LOCATOR 召回 = 整条链的上限。漏一个，后面全漏。

### R6【严重】`npc_dialogue` 规则模板不覆盖 quest/reward 的"新增"
- `cross_table_splitter.py:739-888`（`_build_npc_dialogue_intents`）只产出 8 条 NPC/对话/选项/spawn op。
- `cross_table_splitter.py:1118`：quest 新增**要求 `任务ID\s*(?:为|是)?\d+`** —— 输入无"任务ID" → 不产 quest op。
- `cross_table_splitter.py:879-887`：reward 只做"改名称"，**从不新增 reward.xlsx 行**。
- 唯一会新建 reward 的 `_build_combat_reward_intents`（`:1320-1366`）被 `"战斗+奖励包+战场"` 门控（`:282-285`），本用例不匹配。
- → 规则安全网对本用例**正好是盲区**。

### R7【性能/成本】LLM 路径一旦触发就慢且贵
- `codemaker_parser.py:751`：`parse_multi` 单次 90s 超时；`run_one_case.py:18` 自承"新链多次累积数分钟"。
- `_build_prompt_with_skills` 把大量表结构塞进 prompt（`_table_index.json` 79MB，全表 schema 一起注入）→ 上百 k token。
- 复杂多表用例一次解析 90s，多 chain 步骤累加 → 156s 卡死、143.8k Tokens 就是这条。
- → LLM 不是不能用，是**上下文喂太胖 + 调用次数无控制**。

### R8【质量】ValidatorAgent 的 LLM 裁决未实现
- `subagent/validator_agent.py` docstring 称"LLM 仅在规则无法判定时裁决"，但全文件**无 `_call_llm` 调用**，`validate()` 是纯 Python（`_suppress_over_produce / _align_produces_labels / _validate_consumes_match / _validate_fk_coverage`）。
- → 没有任何语义级自洽兜底（如"reward_id 10088 不存在却要引用 / quest 未建却要挂"这种问题规则查不出来）。

---

## 3. 为什么"不应该全是规则理解"

当前架构的悖论：

1. **规则过强**：R1 的 0.90 别名一票否决式选错目标表，连歧义都没了 → 关掉 LLM。
2. **规则过浅**：游戏域语义（"支线/采集/提交/完成奖励/选项1/选项2"）无法穷举成 regex；自然语言变体无穷（"接下"/"接取"/"领取"），R2/R3/R6 教科书式漏。
3. **LLM 兜底位置错**：LLM 被放在规则之后、且只能在 `is_cross_table` 真（=规则多候选）时才介入。可恰恰是规则"自信地选错"的单候选场景（R1/R4）最需要 LLM 介入 —— 而 LLM 在这里被关掉了。
4. **LLM 触发后上下文病态**：一次性喂全表 schema（R7）→ 慢/贵/易超时。

→ 正确的架构应是"**复杂意图 LLM 主、简单意图规则主**"，而不是"规则主、LLM 兜底"。规则只适合 (a) 单动词单表（"修改朱雀成长率为 1.5"），(b) 已固化的硬模板链（pet_evolve）。凡含"对话/选项/支线/奖励/采集/Spawn/多 id 引用"等语义聚合信号，就该走 LLM。

---

## 4. 优化方向（按 成本/收益 排序）

### P0 ｜ 数据层快速修复（半天内可做，命中率立竿见影）
1. **修 `alias_mapping.json`**：
   - 改 `"奖励": "reward.xlsx"`（修正 R1）。如确有 formula_nested 业务也别用裸"奖励"，换成 `"段位奖励"/"赛季奖励"` 等带限定词。
   - 增补 `"支线"/"主线"/"任务线"/"采集任务"/"提交任务" → quest.xlsx`；`"完成奖励"/"任务奖励" → reward.xlsx`（覆盖 R2 的措辞变体）。
2. **`table_locator.py:184` 改双向 + 模糊**：`if hn in t or t_seg in hn` 或对表头做短分词后 Jaccard，让"目标类型"匹配"任务目标类型"（修 R3）。
3. **`cross_table_splitter.py:277` 放宽 quest 门**：`_ADD_PREFIX` 可选 + `支线|主线|日常|任务` 任一即触发；`_build_quest_intents:1118` 把"任务ID"硬要求改成"未指定则自动分配"（修 R2/R6）。

### P1 ｜ 定位短路修复（1 天）
4. **`subagent/locator_agent.py:84-86 is_cross_table` 扩展**：新增"复杂度信号"触发链 —— 输入含 ≥2 个 id-like 引用（`\bid_\w+\s*\d+`）或引号名 ≥2 个或含对话动词（点击/选项/对话/采集/提交/接）时，**即便单候选也强行进入 DecomposeAgent**（破 R4）。
5. **`locator_agent.py:146-149` 取消 0.90 硬阈值过滤**：保留所有规则候选 + LLM 候选并集交 DecomposeAgent 决策，别在定位阶段就砍表。
6. **`_collect_fk_edges` 放宽**：reward_id/quest_id 等列名引用"目标表中未必当前候选"时，**朝向式扩表**（命中 `reward_id 10088` ⇒ 候选补 reward.xlsx）—— 关系图驱动的反向发现（补 R5）。

### P2 ｜ LLM 优先 + 上下文瘦身（2-3 天，核心重构）
7. **新增"表集合规划"前置阶段**（轻量 LLM 调用，只输出 `[{table, sheet, action}]` 不出 fields）：复杂输入先跑它定 table-set，再定位/填字段。让 LLM 在意图理解主路径而非兜底。
8. **DecomposeAgent 解耦 locator 召回**：允许 LLM 在候选之外提议表（用 `_table_index.json` + alias 校验防幻觉），补上被漏的 quest/reward。
9. **`_build_prompt_with_skills` 上下文瘦身**：只注入规划出的聚焦表 schema（不是全表）+ row_aliases + FK 边。token 从 100k+ 压到 ~10-20k（修 R7），单次调用回到 30-45s 内。
10. **按段并行 parse 替代单次 parse_multi**：`multi_intent_splitter` 已有切分，复杂句切完各段小 prompt 并发，避免 90s 巨型调用。

### P3 ｜ 校验兜底（1 天）
11. **实现 `ValidatorAgent` 的 LLM 裁决**：`reward_id`/`quest_id` 引用前向校验（目标行不存在则报错/自动补建），语义自洽网（补 R8）。
12. **`required_fields.yaml` 落地**：README 宣称但实际缺失；quests/Reward 必填项缺失要能告警。

---

## 5. 验证建议

- 在跑 `quest_npc_double_option.json[0]` 时加 `CODEMAKER_PARSE_MULTI_TIMEOUT=30` + 打印每次 LLM prompt 长度，定位是否还 100k+。
- 用 `server/tests/run_one_case.py` 的 monkeypatch（已内置 `[LLM]` 拦截 `_patched_prompt` at `run_one_case.py:279-300`）逐用例确认"调了几次 LLM、各次 prompt 大小、各耗多久"。跑通那次预期看到 **0 条 `[LLM]`**（应证 R1/R4 短路）；卡死那次应看到 1 条巨型 `[LLM]`（应证 R7）。
- 修完 P0 后，quest/reward 应至少进入候选集（哪怕字段填错），match_case 的 `missing` 变 `partial`；修完 P2 后应能稳定 `matched=7`。

---

## 附：关键代码引用索引

| 环节 | 文件:行 |
|---|---|
| 链开关/触发 | `agent.py:3661, 3662-3683` |
| is_cross_table 判定 | `subagent/locator_agent.py:84-86, 136-149` |
| 表定位 5 级 | `table_locator.py:85-244`（5 级单方向匹配 :184） |
| 别名反查 | `alias_mapping.json:34(奖励→formula_nested/错), :96(特殊奖励→reward), :36-38(任务→quest)` |
| LLM 路由线索（埋了没用到） | `subagent/locator_agent.py:201-220`（含"任务→quest"但只在歧义时喂） |
| 规则安全网 detect | `cross_table_splitter.py:230-291`（quest 门 :277） |
| npc_dialogue 模板 | `cross_table_splitter.py:739-888`（quest 仅 :1118, reward 仅改 :879-887） |
| DecomposeAgent LLM | `subagent/decompose_agent.py:52,83` / `subagent/base.py:150,173` |
| parse_multi LLM | `codemaker_parser.py:735-779`（90s 超时 :751） |
| ValidatorAgent 纯规则 | `subagent/validator_agent.py:66-107`（无 LLM） |
| produces 推断 | `produces_inference.py:89-178` / `agent.py:3930` |
| Step5 执行 | `agent.py:4003-4089` / `_phase_execute:4929` |
| 拓扑/产出捕获 | `operation_orchestrator.py:267(_topo_order), 409(_capture_produced), 388(_resolve_placeholders)` |

---

## 6. P0 落实记录（已实施 + 已验证）

### 6.1 已改动文件
| 文件 | 改动 | 根因 |
|---|---|---|
| `alias_mapping.json` | `"奖励": "formula_nested.xlsx"` → `"reward.xlsx"`（错路由修复）；新增 `完成奖励/任务奖励→reward`、`支线/主线/采集任务/采集支线→quest`、`NPC/商人/守卫→entity_prefab` | R1/R2 |
| `table_locator.py` | `_level5_column` 增 `_column_in_text` 双向+窗口公共子串（`目标类型`↔`任务目标类型`），长表头退化为前/后缀窗口 | R3 |
| `subagent/locator_agent.py` | `import re`；新增 `_is_complex_input`（对话/选项/支线/采集/完成奖励 或 ≥2 id引用/≥2引号名）；复杂输入走 `_expand_by_fk` 关系图扩表（任一端 stem 在候选内则补对端），并跳过 LLM 收敛避免歧义短路 | R4/R5 + R7(避免 40 表膨胀) |

> 取消了原本计划的 `cross_table_splitter.py` quest 门放宽（`:277`）——因为复杂输入现在经 `_is_complex_input` 直接走 LocatorAgent→DecomposeAgent(LLM) 链，`detect_cross_table_action` 规则闸门不再被命中（被 `agent.py:3685 if not cross_intents_nl` 短路）。quest 表的发现交给 LocatorAgent(alias 支线→quest) + DecomposeAgent，符合"复杂意图 LLM 主"方向，无需再放宽规则门。

### 6.2 locator 级验证（smoke，已通过）
对 `quest_npc_double_option.json[0]` 输入运行 `LocatorAgent.locate`：
- alias 命中：`奖励→reward、完成奖励→reward、采集支线→quest、支线→quest、NPC→entity_prefab`（**formula_nested 不再误入**）
- `LocatorAgent.locate` 候选 stem = `['entity_prefab','interaction','item','quest','reward','spawn_quest_entity','spawn_world_entity']`（**7 个，非 40 噪声**）
  - 其中 `interaction/spawn_world_entity/spawn_quest_entity/item` 经 FK 关系图扩展（entity_prefab↔interaction、interaction↔reward、spawn_world_entity↔entity_prefab、spawn_quest_entity↔quest）
- `is_cross_table=True`、`fk_edges=5` → **DecomposeAgent(LLM) 将被触发**（这是本次修复的枢纽：原来因 R1 别名误路由被短路成单候选导致 is_cross_table=False）
- 简单输入无回归：`查看灵兽朱雀→候选 pet 单条 cross=False`、`修改朱雀成长率→complex=False 不走扩表`。

### 6.3 仍未解决的 [step3] 卡死（根因定位，超出 excel-agent 代码）
E2E 跑 `run_one_case.py` 仍卡在 `[step3] service.chat` 且**在 LocatorAgent.locate 被调用之前**（无 `[DUMP1]`/`[sub:定位]`/`[LLM]` 输出）。逐层排查结论：
- `CodemakerClient.create_session(directory=testtest)` 实测 **0.0s 返回 ok**（codemaker serve 不在创建会话时预索引）→ **会话创建非卡点**。
- `codemaker_parser._build_prompt_with_skills` 实测复杂输入 prompt 仅 **9359 字符 ≈ 9573 tokens**（简单输入 ~8373 tokens）→ **parse_multi 上下文非 143.8k 元凶**。
- 你原始日志头部 `CodeMaker 文件读取 用户中止回答 | 156s | 143.8k Tokens` 中"文件读取"是关键：143.8k token = excel-agent 这边 prompt 才 ~9.6k，**多出的 13 万 token 来自 codemaker serve 端**——`client.prompt` 命中的是 codemaker serve 的**带工具的 agent 端点**，每次会话被 project directory 上下文触发其内部 agent 读资源/索引文件(79MB `_table_index.json` 等)，单次 156s+、124k token 起跳。

> ⚠ 这是 **codemaker serve 端行为**，不是 excel-agent 代码 bug，excel-agent 无法单边修复。需在 serve 侧：用"纯文本补全"模型/端点（不开 file 工具 / MCP）、或关闭 serve 端的 directory 自动上下文、或对 excel-agent 的 parse/decompose 调用走非 agentic 通道。**建议下一步先确认 codemaker serve 的 `/session/{id}/message` 是否带文件工具、能否配置为无工具纯补全。**

### 6.4 待 E2E 确认项
- 待 codemaker serve 侧 [step3] 卡死缓解后，重跑 `run_one_case.py` 应见：`[DUMP1]` 7 候选→`[DUMP2] DecomposeAgent` 产含 quest/reward 的意图→`[MATCH]` 中 quest/reward 由 missing 变 partial/matched。
- 若 DecomposeAgent 对 dialog(InteractionConv/Option 多 sheet) 的 produces/consumes 连线不如 `npc_dialogue` 规则模板精细，dialog 的 field_score 可能下降——届时考虑 P1：dialog 子链交规则模板、quest/reward 交 LLM，做混合合并（agent.py 多源意图合并）。

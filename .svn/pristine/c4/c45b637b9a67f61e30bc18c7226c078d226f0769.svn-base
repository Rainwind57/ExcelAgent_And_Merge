# Excel-Agent 全流程问题诊断与优化方向

> 用例：`quest_npc_double_option.json[0]`（新增 NPC + 双选项对话 + 采集支线 + 完成奖励 + 回城卷轴奖励，期望 7 条，实跑 matched=1，quest.xlsx / reward.xlsx 全丢失）
> 结论先行：**quest/reward 丢失与 [step3] 卡死，根治点有两层**——excel-agent 逻辑层（别名误路由 + 歧义短路，本轮 P0 已修）+ **打包/服务层（excel 子包重构后数据文件路径错位 `R9a` 已修；双命名空间非确定性 ImportError `R9b` 未修；codemaker serve agentic LLM 开销 `R7` 未修）**。逻辑修复已验证（smoke 复现 7 候选 + is_cross_table=True），但 E2E 仍被 R9b/R7 阻断。

---

## 0. 关键结论：这次到底有没有调用 LLM？

| 运行 | 意图理解是否调 LLM | 结果 |
|---|---|---|
| **跑通的那次**（8 子任务，`[Step5]`） | **完全没调 LLM**，纯规则路径 | 跑得快但错：quest/reward 全丢，matched=1/7 |
| **卡死 `[step3]` 的那次**（156s、143.8k Token、中止） | **调了 LLM**（codemaker serve 端 agentic） | 慢、贵、卡死 |

为何同一用例两种命运？（修前）入口 `LocatorAgent.locate`：当规则给出"一个高置信候选"时 `is_cross_table=False` 整条 LLM 链跳过；给出 ≥2 候选时又触发 `DecomposeAgent` + `parse_multi` 巨型 LLM。两条都不可靠 —— 而根因 `R9a`（**运行时表索引/别名根本没加载到**）让运行时 `loc()` 多数时候返回 0 候选，链压根没机会触发，走规则模板兜底 → 8 op、丢 quest/reward。

### 0.1 "跑通那次"没调 LLM 的证据链
`agent.py:3662-3683` 链触发条件 = `is_cross_table`（`locator_agent.py:84-86` = `len(candidates)>=2 or fk_edges`）。对本输入 `TableLocator` 5 级命中（修前）：2 级别名 `"奖励"→formula_nested.xlsx`(`alias_mapping.json:34`) 0.90 命中"完成奖励"；5 级 reward.xlsx 列名 `reward_id` 0.60。差距 0.30 → 非 ambiguous → 单候选 → `is_cross_table=False` → `DecomposeAgent`(LLM) 整条不执行 → 走 `agent.py:3685` 规则安全网 `npc_dialogue` 模板（`cross_table_splitter.py:739-888`）吐 8 op，**不碰 quest.xlsx/reward.xlsx**（R6）。

### 0.2 "卡死那次"调了 LLM 的证据
`[step3] service.chat` 156s + 143.8k Token = `parse_multi`/`_llm_chain_decompose` 行为（单次 90s 超时 `codemaker_parser.py:751`，新链多次累积）。但实测 excel-agent 这边 prompt 仅 ~9.6k token，**多出的 13 万 token 来自 codemaker serve 自带 agent**，详见 R7。

---

## 1. 全流程逐环节问题诊断

```
用户输入
  ├─ [意图理解] nl_parser / multi_intent_splitter / cross_table_splitter  ← 100% 规则（R2/R3/R6）
  ├─ [表定位]   LocatorAgent → TableLocator 5 级                           ← 规则过强、误短路（R1/R3/R4/R9a）
  ├─ [分解]     DecomposeAgent (LLM)  ← 仅 is_cross_table 时触发，且只能分解 locator 已选中的表（R5）
  ├─ [校验]     ValidatorAgent        ← 100% 规则，LLM 裁决未实现（R8）
  ├─ [排序执行] OperationOrchestrator._topo_order / _capture_produced   ← OK（健康）
  └─ [写库]     _phase_execute → _run_add                                  ← OK
```
坏在前 4 环（理解→定位→分解→校验）；执行/拓扑/写库健康（产 8 op 的 produces/consumes 占位替换、拓扑、回填都对）。

---

## 2. 根因清单（带 `file:line` 证据，按严重度排序）

### R1【致命】别名污染：`"奖励" → formula_nested.xlsx`
- `alias_mapping.json:34`：`"奖励": "formula_nested.xlsx"` —— **错路由**；只有 `:96 "特殊奖励"→reward.xlsx` 才对。
- 后果：输入"完成奖励"0.90 高置信误路由 formula_nested，**既误选目标表、把 reward(0.60 列名命中)踢出候选集，又关闭歧义分支**（R4），让 LLM 连被咨询的机会都没。**本轮已修（改→reward.xlsx）+ 新增 完成奖励/任务奖励→reward、支线/主线/采集任务/采集支线→quest、NPC/商人/守卫→entity_prefab**。

### R2【致命】quest 表规则不可发现
- 文本用词 `支线/采集/完成奖励/目标类型/目标数据/reward_id/group_id`；别名表只认字面"任务/任务组/任务目标/剧情"，输入**一个都没有**。
- `cross_table_splitter.py:277` quest 检测要 `_ADD_PREFIX`(`新增/增加/添加`) + 紧跟"任务" → 输入"接下采集支线'寒玉草'"既无前缀也无常量"任务"→ 不命中。
- `skill_context.pre_route` 路由关键词**根本没 quest** → 连喂 LLM 的提示词都不提它。
- → quest 在规则路径下**结构性不可达**。**本轮通过新增 `支线/主线/采集*` alias → quest 让别名命中（send-to DecomposeAgent），不依赖放宽 regex 门。**

### R3【严重】5 级列名匹配单向精确子串
- `table_locator.py:184` 仅 `if hn in t`（表头是文本子串才算）；quest 表头"任务目标类型"遇输入"目标类型"漏匹配（方向反、不容错）。**本轮已修：`_column_in_text` 双向 + 窗口公共子串（短的滑窗、长头退化为前/后缀窗口）。**

### R4【严重】歧义消解短路把链整条关掉
- `locator_agent.py:146-149`：LLM 选定 stem 后候选过滤成 `{llm_stem} ∪ {conf≥0.90}`。
- `is_cross_table`=`len(candidates)>=2 or fk_edges`。单候选 + 无 FK → False → `agent.py:3665` 跳 `DecomposeAgent`。
- **本轮已修：复杂输入 (`_is_complex_input`) 保留全部候选并跳过 LLM 收敛；用 FK 关系图扩表补关联表，不走 `locate_all` 全量(避免 40 噪声)。**

### R5【严重】DecomposeAgent 不能发现表，只能分解 locator 已选中的表
- `decompose_agent.py:52-215`：prompt 约束"按候选表 schema 分解为每表一 op"。locator 召回 = 链上限。**本轮已修：FK 关系图扩表（entity_prefab↔interaction、interaction↔reward、spawn_world_entity↔entity_prefab、spawn_quest_entity↔quest）把关联表补进候选，DecomposeAgent 看到完整跨表链。**

### R6【严重】`npc_dialogue` 模板不覆盖 quest/reward 的"新增"
- `cross_table_splitter.py:739-888` 只产 8 条 NPC/对话/选项/spawn op；quest 新增要"任务ID"(`:1118`)、reward 只改名称(`:879-887`)、新建 reward 的 `_build_combat_reward_intents`(`:1320-1366`) 被"战斗+奖励包+战场"门控(`:282-285`)。规则安全网对本用例**盲区**。本轮不再放宽规则门（复杂输入直接走 LLM 链，跳过模板）。

### R7【性能/成本】codemaker serve 自带 agentic LLM（E2E 卡死真因之一）
- `codemaker_parser.py:751` parse_multi 单次 90s；实测复杂输入 prompt 仅 **9573 token**（简单 8373）→ excel-agent 这边 prompt **非巨型**。
- `CodemakerClient.create_session(directory=testtest)` 实测 **0.0s** ok → 会话创建非卡点。
- 你原始日志 `CodeMaker 文件读取 … 156s 143.8k Token` —— "文件读取"= `/session/{id}/message` 走的是**带工具的 agent 端点**，serve 端 agent 因 project directory 上下文读文件(含 76MB `_table_index.json`)，单次 156s+/124k token。**需 serve 侧：纯文本补全端点、关 file 工具、关 directory 自动上下文**。

### R8【质量】ValidatorAgent 的 LLM 裁决未实现
- `validator_agent.py` docstring 称"LLM 在规则无法判定时裁决"，全文件无 `_call_llm`，`validate()` 纯 Python。无语义级自洽网（reward_id/quest_id 引用前向校验）。→ P3 待补。

### R9【环境/打包，实际 E2E 阻断主因】

**R9a 数据文件路径错位（已修）**：excel 做了子包重构（新增 `excel/locator/`，`table_index.py`/`alias_mapping.py` 挪入），但 76MB `_table_index.json` 与 `alias_mapping.json` **仍在 `excel/` 父级未移**。原 `locator/table_index.py:_idx_path()` = `Path(__file__).parent/_table_index.json` → 指向 `excel/locator/_table_index.json`（**不存在**）→ `load_index()` 抛 `FileNotFoundError` → TableLocator.index 空 → **所有表定位全 miss、候选 0、is_cross_table 永假、DecomposeAgent 永不触发**。直接证据：`probe_idx` 报该 FileNotFoundError。**本轮已修：`_idx_path`/`_alias_path` 逐级向上(本目录→父→祖父)找首个存在处，回退父级触发重建。** 修后 `load_index` 正确读到 76MB 索引 → smoke 复现 7 候选。

**R9b 双模块命名空间 → 非确定性 ImportError（未修）**：同一份代码被 `agent.excel.*`（`testtest/server` 在 sys.path）与 `server.agent.excel.*`（`testtest` 根在 sys.path）两条路径加载成两套模块对象。相对 import 与 `__init__.py` re-export 在两种命名下命中不同，导致执行顺序敏感、间歇崩。实测：
- `server/agent/__init__.py:13 from .excel.cli_interface import …` 间歇 `ModuleNotFoundError: server.agent.excel.cli_interface`；
- `cli_interface.py:214 from .formula_cache_validator import` 间歇 `ModuleNotFoundError: agent.excel.formula_cache_validator`。
- 即 excel 包还有 `cli_interface.py`/`formula_cache_validator.py` 被挪进子包但 `__init__` 与相对引用未同步——重构只搬了一部分。
- 副作用：`run_one_case.py` 的 DUMP/LLM monkeypatch 命中 `server.agent.*` 而 AgentService 运行在 `agent.*`，**调试拼接对运行实例不可见**（解释 `[DUMP1]`/`[LLM]` 从不打印）；且不同运行次序 import 偶发崩 → Exit 0/2 抖动。
- **需项目侧**：统一 `__init__.py` 与子包相对 import，或启动脚本固定唯一 sys.path 命名空间（只 `agent.*` 或只 `server.agent.*`，建议 `pip install -e .` 让包身份唯一）。

---

## 3. 为什么"不应该全是规则理解"

当前架构悖论：规则过强（R1 一个 0.90 别名一票否决式选错表+关歧义→关 LLM）+ 规则过浅（游戏域语义"支线/采集/完成奖励"无法穷举成 regex）+ LLM 兜底位置错（只在 `is_cross_table` 真才介入，恰是规则"自信选错单候选"时最需 LLM 却被关）+ LLM 触发后上下文病态（serve 端 agentic 读 76MB 文件→100k token+卡）。
正确架构应"复杂意图 LLM 主、简单意图规则主"。本轮 P0 就是把复杂输入（对话/选项/支线/采集/完成奖励 或 ≥2 id/引号名）强制交 DecomposeAgent（聚焦候选 schema，~10-20k token，不是 100k）——与你方向一致。

---

## 4. P0 落实记录（已实施 + 已验证）

### 4.1 改动文件
| 文件 | 改动 | 根因 |
|---|---|---|
| `excel/alias_mapping.json` | `奖励→reward`(修错) + `完成奖励/任务奖励→reward`、`支线/主线/采集任务/采集支线→quest`、`NPC/商人/守卫→entity_prefab` | R1/R2 |
| `excel/table_locator.py` | `_level5_column` 增 `_column_in_text`：先精确子串，再对中国复合表头（len≥4）做 ≥半长公共子串窗口；长头退化为前/后缀窗口 | R3 |
| `excel/subagent/locator_agent.py` | `import re`；增 `_is_complex_input`；复杂输入 `_expand_by_fk`(FK 关系图任一端在候选内则补对端 stem，置信 0.50) + 跳过 LLM 收敛 | R4/R5+R7 |
| `excel/locator/table_index.py` `_idx_path` | 逐级向上找 `_table_index.json`（修移动后路径错位） | R9a |
| `excel/locator/alias_mapping.py` `_alias_path` | 逐级向上找 `alias_mapping.json` | R9a |

> 取消 `cross_table_splitter.py:277` quest 门放宽——复杂输入现直走 LocatorAgent→DecomposeAgent 链，`detect_cross_table_action` 不再被命中。

### 4.2 验证（smoke，多次稳定通过）
`quest_npc_double_option.json[0]` 输入跑 `LocatorAgent.locate`：
- alias 命中：`奖励→reward、完成奖励→reward、采集支线→quest、支线→quest、NPC→entity_prefab`（**formula_nested 不再误入**）
- 候选 stem = `['entity_prefab','interaction','item','quest','reward','spawn_quest_entity','spawn_world_entity']`（7 个，非 40 噪声；`interaction/spawn_*/item` 经 FK 扩表）
- `is_cross_table=True`、`fk_edges=5` → **DecomposeAgent 将触发**（修复枢纽）
- 简单输入无回归：`查看灵兽朱雀→pet 单条 cross=False`、`修改朱雀成长率→complex=False 不扩表`

### 4.3 E2E 仍阻断（R9b/R7）
跑 `run_one_case.py`：多次 Exit 0/2 抖动；跑通时仍 matched=1、quest/reward missing。根因=**运行时 `load_index` 在真实 AgentService import 链下间歇 FileNotFoundError**(R9a，本轮已修但 R9b 的非确定性 import 仍可能让修复版 `table_index` 未被正确加载) + DecomposeAgent 的 LLM 调用走 serve agentic 端点卡/超时(R7)。待 R9b/R7 缓解后重跑即应见 quest/reward 进 `[MATCH]`。

---

## 5. 优化方向（按 成本/收益 排序）

### P0（已完成，本轮）
1. ✅ 修 `alias_mapping.json` 奖励错路由 + 补支线/采集/NPC 等 alias（R1/R2）
2. ✅ `table_locator` 5 级列名双向+模糊（R3）
3. ✅ `locator_agent` 复杂输入扩候选 + 跳过收敛；改 `locate_all` 全量噪声为 FK 关系图精准扩表（R4/R5+R7）
4. ✅ 修 `locator/_idx_path`/`_alias_path` 子包移动后路径错位（R9a）

### P1｜定位短路/扩表增强（1 天）
5. `is_cross_table` 增复杂度信号触发（本轮已用 `_is_complex_input` 部分实现，可外加多 id/引号名信号阈值调整）
6. `_collect_fk_edges` 朝向式扩表：命中 `reward_id/quest_id` 引用即补对应表（含未命中）
7. DecomposeAgent 解耦 locator 召回：允许 LLM 在候选外提议表（用 `_table_index`+alias 校验防幻觉）

### P2｜LLM-first + 上下文瘦身 + 打包修复（2-3 天，核心）
8. **修 R9b 双命名空间**：统一 `excel/__init__.py` 与子包相对 import；启动脚本固定单一 sys.path 命名空间（或 `pip install -e .`）。**← 这是 E2E 能稳定跑的前提**
9. **L9 codemaker serve**：让 parse/decompose 走纯文本补全端点、关 file 工具、关 directory 自动上下文（消除 143.8k/156s）
10. 新增"表集合规划"前置轻量 LLM（复杂输入先定 `[{table,sheet,action}]`，再定位/填字段）；DecomposeAgent 上下文只注聚焦表 schema（非全表），token 压到 ~10-20k
11. 按段并行 `parse` 替代单次 90s 巨型 `parse_multi`

### P3｜校验兜底（1 天）
12. 实现 ValidatorAgent 的 LLM 裁决（reward_id/quest_id 引用前向校验，自动补建）
13. `required_fields.yaml` 落地（README 宣称但缺）

---

## 6. 关键代码引用索引
| 环节 | 文件:行 |
|---|---|
| 链开关/触发 | `agent.py:3661, 3662-3683` |
| is_cross_table 判定 | `subagent/locator_agent.py:84-86` |
| 复杂输入判定(本轮新增) | `subagent/locator_agent.py:_is_complex_input` |
| FK 扩表(本轮新增) | `subagent/locator_agent.py:_expand_by_fk` |
| 表定位 5 级 | `table_locator.py:85-244`；列名匹配(本轮改) `_column_in_text` |
| 别名反查 | `alias_mapping.json`(本轮改:34/36/.../`NPC`)/`locator/alias_mapping.py:_alias_path`(本轮改) |
| 索引路径(本轮修) | `locator/table_index.py:_idx_path` |
| LLM 路由线索(埋了歧义才用) | `subagent/locator_agent.py:201-220` |
| 规则安全网 detect | `cross_table_splitter.py:230-291`（quest 门 :277） |
| npc_dialogue 模板 | `cross_table_splitter.py:739-888` |
| DecomposeAgent LLM | `subagent/decompose_agent.py:52,83`；schema 聚焦 `:104-138` |
| parse_multi LLM | `codemaker_parser.py:735-779`（90s 超时 :751） |
| ValidatorAgent 纯规则 | `subagent/validator_agent.py:66-107` |
| Step5 执行 | `agent.py:4003-4089` / `_phase_execute:4929` |
| 拓扑/产出捕获 | `operation_orchestrator.py:267(_topo_order),409(_capture_produced),388(_resolve_placeholders)` |
| 双命名空间风险 | `server/agent/__init__.py:13`(excel.cli_interface) / `cli_interface.py:214`(formula_cache_validator) |

---

## 7. 第二轮（并发重构后追加修复 + 最终隔离）

首轮 P0 完成后与用户并发重构目录（excel 拆成 `cli/core/parser/pipeline/repair/locator/subagent/skills` 子包）冲撞，发现并修复一批重构遗留：

### 7.1 新增修复（重构遗留的数据路径 + 相对 import）
| 文件 | 改动 | 根因 |
|---|---|---|
| `excel/core/table_relations.py` `_relations_path`/`_runtime_relations_path` | 逐级向上找 `table_relations.json`/runtime（原指 core/ 不存在）→ FK 图恢复 | R9a |
| `excel/core/table_resolver.py:67` `_table_index.json` | `Path(__file__).resolve().parent.parent` | R9a |
| `excel/cli/cli_interface.py:257,656` `_table_index.json` | 同上 | R9a |
| `excel/repair/cascade_planner.py:38` `table_relations.json` | 同上 | R9a |
| `excel/core/llm_context.py:521` `skills/value_constraints.yaml` | `parent.parent` | R9a |
| `excel/core/agent.py:499` `from ..llm_counter` → `from ...llm_counter`（agent.py 挪进 core/ 后少一 dot） | R9b |
| `excel/core/agent.py:523-525` `from .subagent.*` → `from ..subagent.*`（subagent 是 excel 的 sibling 不是 core 的子） | **R9b 关键：三 agent 链此前 init 失败→locator/decompose/validator 全 None→链永不触发** |
| `excel/subagent/decompose_agent.py:83` | `timeout` 改可配 `CODEMAKER_DECOMPOSE_TIMEOUT`(默认 90) | 诊断 R7 |

### 7.2 最终隔离结论（已多次验证，稳定）
- ✅ 数据路径全修：`load_index` 读到 76MB 索引；`table_relations` FK 图恢复 → locator 候选从 0 重建到 **7 个**。
- ✅ 相对 import 全修：`AgentService` 构造不再 ImportError；三 agent 链 `locator=decompose=validator=True`（之前全 False）。
- ✅ 链触发验证（probe_decompose.py）：`locator 候选 7 cross=True fk=5` → `DecomposeAgent.decompose` **被调用**（链打通到 LLM 边界）。
- ❌ **唯一剩余阻断 = R7（codemaker serve 端）**：`_call_llm_raw` 对 `/session/{id}/message` 发送聚焦 prompt（~10-20k token）后，serve **180s 仍返回空（raw_len=0）** → decompose 返回 [] → 规则模板兜底 → 8 op / quest+reward missing。
  - 即你原始 `[step3] 卡死 156s/143.8k Token/文件读取` 的同一根因：serve 端对该 session（directory=testtest）跑了一个读资源/索引的 agentic 死/慢循环，单次 >90s/180s 返回空。
  - **非 excel-agent 代码能修**：需 serve 侧关 file 工具 / 换纯文本补全端点 / 查 serve 日志看该 session 为何跑空循环。

### 7.3 E2E 现状
`run_one_case.py` EXIT=0 但仍 matched=1、quest/reward missing —— 不是逻辑错失（locator 已见 quest/reward），而是链向 serve LLM 拿不到回复回退规则模板。**R7 缓解后 serve 能回 DecomposeAgent 一个正常 JSON 数组时，链即可产含 quest/reward 的意图，matched 应跃升。**

### 7.4 R7 最终定性（已用对照实验锁定，非 excel-agent 可修）
对照实验：
- 同一 directory-backed 会话，**trivial prompt**（"回复 OK 即可，不要调用工具"）→ **1.7s 返回 "OK"** ✓。
- 同一会话，**DecomposeAgent prompt**（含候选表 schema，文本里出现 `quest`/`reward`/`interaction`/`spawn_world_entity` 等 stem）→ **90s/180s 仍 raw_len=0 超时空回复** ✗。
- 给 prompt 显式加 "**不要调用任何工具，不要读取/搜索/打开任何文件**" 指令 → **仍 90.6s 空回复**，serve **忽略**该约束。

结论：servue 端对"prompt 文本中出现的表 stem"做**自动文件读取上下文**（去读 `quest.xlsx` 等真实 xlsx），90s 超时返回空。这是 codemaker serve 端的**固定行为**，prompt 约束管不住。
- 修复方向（**serve 侧**）：① 为 `/session/{id}/message` 关掉 auto-context-grounding / file 工具；② 提供纯文本补全模型变体给 excel-agent 这类调用用；③ serve 日志查为何读 xlsx 后返回空（可能 xlsx 过大/解析炸 + 工具循环）。
- excel-agent 侧**唯一能做的临时绕过**：把 schema_block 里的 stem 改成无文件触发力的代号（如 `表1/Sheet1`），再让 LLM 用代号输出后回填真实 stem —— 但这将损失列名对齐质量，不推荐为正式方案。

### 7.5 第三轮（应对 R7 实战）—— DecomposeAgent 并行 + merge + workflow 审计

实际跑发现 R7 进一步分化为"serve 吞吐量/稳定性"问题，并非纯文件读取：
- **trivial prompt**: 1.7s ✓；**2-table 分解 prompt**（短）: 6.8s ✓；**interaction 单表 prompt**（复杂 schema，120s 预算）: 68s ✓ 返 JSON。即 serve 在"prompt 不太大、不大并发"下稳定。
- **7-table 单 prompt 汇总**（5.5k 字符，240s 预算）: 240s 超时返空 ✗；**8-way 并行 7 表**（per-call 90~120s，W=8）: 返 0~3 条不稳定 ✗。
- 规律：serve 的底层 LLM 吞吐有限，单次 prompt 越大/并发越高越易超时返空；prompt 内含表 stem/name 和文件读取无强相关（其空目录隔离对照仍失败）——是 model 后端速率/容量问题。

本轮落地代码（已实施）：
1. `subagent/base.py` `_isolated_empty_dir()` + `_ensure_own_session` 默认 `CODEMAKER_SUBAGENT_ISOLATE_CONTEXT=1`：子 Agent 会话用空临时目录隔离上下文，规避 serve 用 project 目录作隐式 file context 的（早期误解 R7 时的预防）。仍保留——确实不再受 directory 自动上下文干扰。
2. `subagent/decompose_agent.py` `decompose` 从"单次全候选 big prompt"改为**每候选表并行 LLM 调用**（`ThreadPoolExecutor`，`CODEMAKER_DECOMPOSE_WORKERS=8`，`CODEMAKER_DECOMPOSE_TIMEOUT=90`，`CODEMAKER_DECOMPOSE_RETRY=1`），每个调用 prompt 小、独立隔离 session；空响应/超时时按 RETRY 重试 1 次。降低单调用负载、提升整体可靠性；`_parse_json_array` 顺带修了"裸 JSON 无 fence 时 `m.group(1)` IndexError"。
3. `core/agent.py:3685` **LLM 链 + 规则模板 merge**：原 `if not cross_intents_nl:` 仅在链产空时回退模板；改为**始终跑模板并按 (table,sheet) 去重合并**——LLM 链能产 quest/reward 时补进来，产不出时模板兜底 NPC/dialog/option/spawn。**保证最坏不回归原基线**，LLM 通畅时增量产出 quest/reward。

实测 merge（probe_merge）：cross_intents_nl 0+5、1+5、3+5 等组合都正确合并，覆盖实体/对话/选项/spawn + LLM 增量的 quest/reward。

### 7.6 工作流是否按 Step 顺序、严谨、无误（已审计代码 `agent.py:run`）

`TableAgent.run`(`agent.py:3597`) 的执行序（已 grep 行号确认）严格有序：

```
Step1  3662-3683  chain: LocatorAgent.locate → DecomposeAgent.decompose → ValidatorAgent.validate
       3684-3728  规则模板 merge(本轮新增)→ 决定 cross_intents_nl
3947-3949  produces_inference + OperationOrchestrator._topo_order  ← 拓扑依赖排序
Step2  3953-3972  _phase_partition（并行子任务）
Step3  3976-3984  _phase_plan  (or _phase_plan_validate_merged)
Step4  3985       _phase_validate
Step5  4020-4108  _step5_log + _phase_execute（按 _topo_order 序，producer 先于 consumer；
                  硬失败时打 broken_producers、联想到的下游跳过）
Step5.5 4111      _backfill_forward_refs（前向引用回填，处理 cyclic conv↔option）
Step6  4114-4118  _phase_summarize
```

- **有序性**：Step 编号严格递增（partition→plan→validate→execute→backfill→summarize），无乱序、无并行乱插。
- **拓扑严谨**：Step5 按 `_topo_order`(Kahn 拓扑) 执行：`produces` 标签建立依赖边，producer 先跑→`_capture_produced` 收集新 ID→`_resolve_placeholders` 才替换 consumer 的 `<label>`；环时回落原序防止死锁；hard-fail producer 会跳过整条下游 producer 链。
- **无误性**：现有职责分层（locator 定位→decompose 分解→validator 校验→orchestrator 排序→execute 写库→backfill 回填→summarize）边界清晰；本轮修复了多处架构 bug（链 init 因 rel-import 失败致全 None、load_index 空索引致 locator 永空）恢复设计执行流。
- **隐患**：仅在 `agent.py:3680 except` 静默吞异常（`CODEMAKER_AGENT_CHAIN_RAISE=0` 时）；建议保留 `RAISE=1` 调试。ValidatorAgent LLM 裁决仍未真实现（docstring 仅宣称）。

### 7.7 真正提升 E2E 可靠性/泛化的下一步（待用户定后继续）

R7 的本质是 **serve 侧 LLM 吞吐/稳定性**，excel-agent 代码侧能做的极限优化是 **缩小每次 LLM 调用范围**：
1. **scoped decompose（推荐，本轮未实施）**：先跑规则模板拆回 cross_intents_nl 取已覆盖的 (table,sheet)，再让 DecomposeAgent **仅对未被模板覆盖的候选表**逐表并行 LLM（本用例:模板覆盖 5 表→只 LLM quest/reward≤2 调用，速度遽提、可靠性大增，且泛化：未知模式时 detect=None→全表 LLM 兜底）。
2. retry 退避策略调优（指数退避、per-stem 限流）+ serve 限流。
3. serve 侧：更快 model 变体、关 auto-context、提高并发能力——这是 R7 真正解。

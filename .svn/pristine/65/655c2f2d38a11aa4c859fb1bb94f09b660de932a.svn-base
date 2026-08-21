# 案例记录：quest_npc 对话链优化全过程

> 特殊案例的优化过程记录，用于复盘与回归验证。通用原则见 `cross_table_chain_principles.md`。
> 输入：`downloads/quest_npc_chain.json`（NPC+多轮对话+接任务+刷怪+改奖励，11 步预期）。

## 0. 案例输入

```
新增一个任务NPC叫'寻宝老人'，model_id 1019，放在space_id 10001坐标(120,0,80)，
玩家点击后弹出对话：老人说'年轻人，老朽有一事相求——我祖传的玉佩被山贼头目夺走，
能否帮我寻回？'选项1'我帮你寻回'，选项2'我现在没空，稍后再来'。
点击'我帮你寻回'后老人继续说'多谢！那山贼头目盘踞在space_id 10008，请击杀他取回玉佩，
必有重谢。'，再点'我这就去'接下任务。配置对应支线任务'寻回玉佩'，任务ID 250020，
任务组group_id 250，描述'帮寻宝老人从山贼头目处夺回祖传玉佩'，目标类型Combat，
目标数据'combat_id:[25002001],npc_id:5025,count:1'，完成奖励reward_id 10090。
在space_id 10008坐标(50,0,60)刷新山贼头目(npc_id 5025)供玩家击杀。
同时把reward_id 10090的名称改为'寻回玉佩奖励'。
```

涉及 6 张表 / 11 步预期 / 7 个 produces 占位符闭环。

## 1. 优化历程（按发现顺序）

### 阶段 1：Step5 事务卡死（ok=None 中断整链）

**现象**：11 步只命中 1 步，producer 产出率 0.10，引用一致 0.06。Agent 假报 ok=True。

**根因**：D2 规则把 ok=None（验证未结论）当失败 → `transaction_failed=True` → 后续全跳过 +
G8 回滚全部前序。嵌套字段（`effect.data.3006.conv_id`、`options[0]`）验证常不结论。

**修复**（`agent.py` Step5 循环 + `operation_orchestrator.py`）：
- ok=None → 软通过（捕获 produced，继续）
- 仅 ok=False 标 `broken_producers`，跳过仅依赖项
- G8 仅回滚直接依赖前序
- `_capture_produced` 门控 `is False`（原 `not ok`）

**结果**：引用一致 0.06→0.31，producer 0.10→0.50。

### 阶段 2：Parser 拆分失败（_CONV_RE 漏匹配）

**现象**：复杂指令只拆 6 段（非 11），conv/option 行全 missing。

**根因**：`_CONV_RE` 要求"对话内容"前缀，"弹出对话：老人说'...'"漏匹配 →
`_extract_npc_dialogue` 返回 None → splitter 退场 → 回退 LLM 粗拆。

**修复**（`cross_table_splitter.py`）：
- `_CONV_RE` 兼容 `对话[：:]` / `弹出对话：` / `老人说`
- `_BRANCH_OPT_RE` 兼容 `点击'X'后继续说'Y'`
- 扩展 `_extract_npc_dialogue` 提取 quest + reward_modify
- 扩展 `_build_npc_dialogue_intents` 发 quest/spawn_quest/reward 意图

**结果**：拆出 10 个结构化意图，对话链 6 步占位符内联解析成功。

### 阶段 3：AI 二次确认覆盖 splitter 路由

**现象**：spawn_world_entity 意图被路由到 entity_prefab，spawn_quest_entity 到 pve_combat_npc。

**根因**：`_phase_partition` 的 AI 二次确认对 splitter 意图也跑，AI 看 raw="刷新 寻宝老人"
误判回 entity_prefab，覆盖规则 0a 精确 stem 命中。

**修复**（`agent.py`）：`extras["source"]=="splitter"` 跳过 AI 二次确认。

**结果**：定位命中率 0.64→0.91。

### 阶段 4：字段值格式不匹配

**现象**：quest "列名不存在：rewards"（值格式问题被 verify-repair 放大）。

**根因**：splitter 写 `rewards="[10090]"`、`pos_list="[[50,0,60]]"`，表头期望裸值/逗号分隔。

**修复**（`cross_table_splitter.py`）：去括号 → `rewards="10090"`、`pos_list="50,0,60"`。

**结果**：quest + spawn_quest 写入成功。

### 阶段 5：_clean_quotes 破坏 splitter regex（最隐蔽）

**现象**：splitter 独立测试产 branch_conv，但运行时产 reward_conv（branch 丢失）。

**根因**：`run()` 入口 `text = _clean_quotes(text)` 剥成对引号 → splitter 的
`_BRANCH_OPT_RE`（依赖引号定界）漏匹配 → branch=None → 误走 `elif reward_opt_idx` 奖励路径。

**修复**（`agent.py`）：保留 `orig_text`，splitter 的 detect/split 用 `orig_text`，
清洗后 `text` 供其余路径。

**结果**：引用一致 0.69→0.875（14/16），steps 2-5 全 matched。

### 阶段 6：extra_intents 把描述性子句误 parse 成垃圾 modify

**现象**：13 子任务（多 3 个垃圾 modify），ok=False，多余写入 7。

**根因**：`extra_intents`（multi_intent_splitter）把描述性子句（"再点'我这就去'接任务"/
"刷新山贼头目"）parse 成 modify/set 写入 space/combat/interaction 表 → 硬失败 + 多余写入。
splitter 已发 reward_modify 时，extra_intents 重复 + 产垃圾。

**修复**（`agent.py`）：splitter 已发 modify/set/delete 意图时，跳过 extra_intents
（`_splitter_covered_modify` 门控）。

**结果**：ok=False→True，多余写入 7→5，耗时 240s→110s。

### 阶段 7：行索引抢匹配覆盖 splitter 精确 stem（reward modify 路由错）

**现象**：reward modify 路由到 item.xlsx/ItemBase（locator "10090" 在 item 行索引命中），
step11 missing。

**根因**：`_resolve_table` 策略1（行索引）先于策略0a（精确 stem），splitter 的 reward
modify intent table_hint="reward" + locator_value="10090" → 行索引在 item 表命中 10090
→ 返回 item.xlsx，覆盖正确 table_hint。

**修复**（`agent.py`）：splitter source 意图精确 stem 命中优先于行索引（_resolve_table
顶部加 splitter 早期精确 stem 检查）。

**结果**：定位 0.91→1.00，异表写入 1→0，step11 reward ✅ matched。

### 阶段 8：_clean_header 不剥内联括号注释（quest rewards 列失配）

**现象**：quest step "未找到列[rewards]"，verify-repair 3 轮失败，ok=False。

**根因**：`_clean_header` 只剥 `:` / `\n`，不剥内联全角括号注释 →
"获得奖励1（多个奖励用英文逗号隔开）" 清洗后仍含括号 ≠ alias 目标 "获得奖励1" →
阶段1精确命中失败。

**修复**（`column_matcher.py`）：`_clean_header` 增剥 `（` / `(` 内联括号注释。

**结果**：ok=False→True，耗时 110s→88s。

### 阶段 9：option_go 接任务选项 + branch_conv 原文尾保护（R7）

**现象**：残留引用一致 0.875 < 0.9——step7 option_go（"我这就去"接任务）splitter 未发；step6 branch_conv 文本尾 `。` 被 rstrip 致 eval 严格匹配字段分 0。

**根因**：
- `_BRANCH_OPT_RE` 只匹配"点击'X'后继续说'Y'"，未捕获后续"再点'我这就去'接下任务"→ option_go 选项缺失
- `_extract_npc_dialogue` branch 块 `bm.group("branch_conv").strip().rstrip('。')` 剥掉预期 prompt_text 尾 `。`

**修复**（`cross_table_splitter.py`）：
- 新增 `_GO_OPT_RE`（`(?:再|然后)?点(?:击)?['"](?P<go_opt>[^'"]+)['"](?:后)?接[下]?任务`）
- `_extract_npc_dialogue` 捕获 `go_opt` + branch_conv 去 `rstrip('。')`
- `_build_npc_dialogue_intents`：branch 块在 go_opt+quest 存在时加 `选项1=<option_go_id>`；其后产 option_go intent（`选项内容`+`function_type=2`+`data.2.quest_id`，produces=`option_go_id`）

**结果**（unit + trace 双证）：
- splitter intent 数 10→11（对齐预期 11 步）
- option_go 命中 opt_text="我这就去"，branch_conv 保尾 `。`
- trace：option_go row27 `{3:'我这就去',5:'2',6:'250020'}` ok=True produces=23；branch_conv row24 prompt_text=`...必有重谢。` 选项1=23（引用闭环）
- **引用一致未冲 0.9（R7 实跑 0.62）**：被 pre-existing column_matcher 嵌套字段 bug 阻断（option_accept `option_function.data.1.conv_id` 拆末段"conv_id"未匹配表头整名 + verify-repair LLM 识别语义等价列但 fix_fields 未真替换），与本次改动无关。详见 `优化全过程.md` R7.4。

### 阶段 10：column_matcher 嵌套字段整名匹配（R8，引用一致冲 0.9 达标）

**现象**：阶段9 option_go/branch_conv 修复达成，但引用一致卡 0.62。trace 锁定真 blocker：option_accept `option_function.data.1.conv_id` column_matcher 拆末段"conv_id"对 row1 中文表头"1:新对话ID"匹配失败→硬失败→主对话 step3 被事务跳过→引用一致 -2。quest `npc_ids` 同源。

**根因**：splitter 写点分规范名（`option_function.function_type`/`option_function.data.1.conv_id`），`read_header` 返回 row1 中文（选项功能/1:新对话ID），`_translate_dotted_keys` 取末段（function_type/conv_id）对中文表头匹配 None。row2 规范名（`option_function.function_type:int`）未被 matcher 利用。

**修复**（`cli_interface.py` + `agent.py`，原则9）：
- cli 加 `read_type_row(path,sheet)` 返 row2 规范名（与 row1 列对齐）
- agent 加 `_type_aliases(path,sheet,headers)` 缓存方法，建 {row2规范名: row1表头} map
- `_translate_dotted_keys` 加 type_aliases 参数：精确命中（`option_function.function_type`→选项功能）+ 前缀2段命中（`option_function.data.2.quest_id`↔`option_function.data.1.conv_id` 共享 [option_function,data]→同列族 c6）
- 3 调用点（_run_add/_run_modify/backfill）传 type_aliases

**结果**（R8 实跑）：
- **引用一致 0.62→1.00（16/16，达标 ✓）**
- 定位 1.00 / 覆盖 0.82→0.91 / 精准 0.56→0.76 / 耗时 134→115s
- trace：option_accept row28 `{3:'我帮你寻回',5:'1',6:'20'}` ok=True produces=24（R7 失败步）；quest row34 全7字段 ok=True；主对话 row25 options[0]=24 options[1]=22（解除跳过）
- 残留：reward modify step11 仍失败（locator_field="id" 但 matcher 搜名称列→未命中 10090，locator 列选择 bug，不影响引用一致 1.00）

演进

| 阶段 | 引用一致 | producer | 定位 | 覆盖 | ok | 耗时 |
|---|---|---|---|---|---|---|
| 原始 | 0.06 | 0.10 | 0.09 | 0.00 | 1.0(假) | 299s |
| 阶段1 Step5修复 | 0.31 | 0.50 | 0.64 | 0.27 | 0.0(诚实) | 279s |
| 阶段2 Parser修复 | 0.50 | 0.70 | 0.73 | 0.36 | 0.0 | 204s |
| 阶段3 路由修复 | 0.69 | 0.90 | 0.91 | 0.73 | 1.0(真) | 144s |
| 阶段4 值格式 | 0.69 | 0.90 | 0.91 | 0.73 | 1.0(真) | 144s |
| 阶段5 原文修复 | 0.875 | 0.90 | 0.91 | 0.64 | 0.0* | 240s |
| 阶段6 extra_intents门控 | 0.875 | 0.90 | 0.91 | 0.73 | 1.0 | 110s |
| 阶段7 splitter stem优先 | 0.875 | 0.90 | **1.00** | **0.91** | 0.0** | 178s |
| 阶段8 _clean_header括号 | 0.875 | 0.90 | **1.00** | **0.91** | **1.0** | **88s** |
| 阶段9 option_go+branch_conv(R7) | 0.62 | — | 1.00 | 0.82 | False | 134s |
| 阶段10 type_aliases(R8) | **1.00** | — | 1.00 | 0.91 | False* | 115s |

*阶段10 ok=False 仅 reward modify locator 列选择 bug（step11），引用一致已 1.00 不受影响。

**当前达标**：引用一致 1.00（≥0.9 ✓）、定位 1.00（≥0.9 ✓）、覆盖 0.91（≥0.85 ✓）。残留：reward modify locator 列选择 + 精准 0.76（字段命名差异）。

## 3. 改动文件清单

| 文件 | 改动 | 对应原则 |
|---|---|---|
| `operation_orchestrator.py` | `_capture_produced` 门控 `is False` | 原则3 |
| `agent.py` Step5 循环 | ok=None 软通过 + 仅跳依赖 + G8 仅回滚直接依赖 | 原则2 |
| `agent.py` `_phase_partition` | splitter source 跳过 AI 二次确认 | 原则4 |
| `agent.py` `_do_append` + `_backfill_forward_refs` | 行号记录 + 前向引用回扫兜底 | 原则5 |
| `agent.py` `run()` | 保留 orig_text 供 splitter | 原则1 |
| `agent.py` fast_path extra_intents | splitter 已发 modify 时跳过 extra_intents | 原则8 |
| `agent.py` `_resolve_table` | splitter source 精确 stem 优先于行索引 | 原则4 |
| `cross_table_splitter.py` | `_CONV_RE`/`_BRANCH_OPT_RE` 变体覆盖 + quest/reward 提取 + 值格式 | 原则6/7 |
| `column_matcher.py` | `_clean_header` 剥内联括号注释 | 原则6 |

## 4. 残留问题（阶段10 后）

- **引用一致 1.00 已达标 ✓**（阶段10 type_aliases 修复）：option_go/branch_conv（阶段9）+ column_matcher 嵌套字段整名匹配（阶段10）共同达成
- **reward modify locator 列选择 bug**（step11）：`locator_field="id"` 但 matcher 在 `名称` 列搜 10090→未命中。ok=False 唯一来源，不影响引用一致 1.00
- **精准 0.76 < 0.9**：quest/spawn field_score 低——eval 字段名严格映射（splitter 用 `name` 而非 `quest_name` 等命名差异）。type_aliases 修复后字段写入完整，但 eval 按预期字段名比对仍失配
- **多余写入 4**：on-table（异表 0），疑似 ai_plan surgical 合并引入（未查）

## 5. 后续方向

1. **reward modify locator 列选择**：locator_field 显式指定时在该列搜，勿回退名称列（修后 ok=True，链完整）
2. **verify-repair Level2 fix_fields 应用校验**（原则9 part 2）：LLM reason 识别语义等价列但 fix_fields 未真替换 key，加 post-LLM validator。type_aliases 修复后此路径少触发
3. **精准冲 0.9**：splitter 字段名对齐 eval 预期（`name`→`quest_name` 等）或 eval 加字段名 alias 容忍
4. **跨链可扩展性**（R8 暴露）：splitter 补 pet/evolve + mail 模板；修 item 模板 ItemBase 字段对齐（"未找到列[物品编号]/[列引用]"）
5. 查多余写入 4 来源（ai_plan surgical 合并是否引入重复 op）

## 6. 回归验证命令

```bash
cd server
uv run python -m tests.task_chain_eval --cases-file ../downloads/quest_npc_chain.json --out tests/reports/quest_npc
# 报告：tests/reports/quest_npc/task_chain_eval_latest.{md,json}
```

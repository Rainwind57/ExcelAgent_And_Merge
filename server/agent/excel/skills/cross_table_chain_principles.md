# 跨表任务链处理原则（通用，多案例适用）

> 从 quest_npc 等案例抽取的**可迁移原则**，适用于所有"单指令多表 DAG"场景
> （NPC+对话、进化链、道具跨 sheet、任务+刷怪、邮件+模板 等）。
> 非案例记录，是排障与优化的通用判据。案例细节见 `case_*.md`。

## 原则 1：规则拆分器（splitter）必须用原文，不能用清洗后文本

**问题**：`_clean_quotes` 剥成对引号后，splitter 依赖引号定界的 regex（对话内容、选项文本、
分支跳转）全部漏匹配 → 结构化提取失败 → 回退 LLM 粗拆 / 误走分支。

**原则**：splitter 的 `detect_cross_table_action` + `split` 必须传**原始文本**（引号未剥）。
清洗后文本供 parse_multi / 其余路径。两者解耦。

**判别**：若 splitter 独立测试通过但运行时失败，第一步查输入是否被 `_clean_quotes` 改写。

**延伸（R7）**：原文保护不止输入文本，也包括**提取出的字段值**。`branch_conv` 等引号内文本若 `rstrip('。')` 剥掉尾标点 → eval 严格文本匹配字段分 0。提取值保留原文尾标点，勿 rstrip 句末 `。`。

## 原则 2：跨表链事务语义——验证不结论 ≠ 失败

**问题**：复合链单步验证常不结论（嵌套字段 `effect.data.N.conv_id`、`options[N]`、
数组字段），若 ok=None 当失败 → `transaction_failed` → 后续全跳过 + 全量回滚 → 整链卡死。

**原则**：
- `ok=True` → 成功，捕获 produced，继续
- `ok=None`（验证未结论/重试中）→ **软通过**：行可能已写入，捕获 produced，不中断
- `ok=False`（硬失败，非 partial）→ 标 broken_producer，**仅跳过依赖它的后续**，独立任务继续
- G8 回滚**仅回滚失败步直接依赖的前序 op**，非全部

**判别**：链整体失败但只有 1-2 步真失败 → 查是否把 ok=None 当失败牵连了独立任务。

## 原则 3：producer 主键回传门控放宽

**问题**：add 写了行但 `produced` 没填 → 下游占位符全悬空。原门控 `not getattr(res,"ok",False)`
把 ok=None 当失败跳过捕获。

**原则**：`_capture_produced` 门控改为 `getattr(res,"ok",None) is False`——
ok=True/None 均尝试捕获，仅 ok=False 跳过。result_rows 无 PK 自然 return，不会误捕。

**判别**：producer 产出率低（<0.5）但行实际写入了 → 查捕获门控是否过严。

## 原则 4：高置信规则路由不应被 AI 低置信推断覆盖

**问题**：splitter 意图 table_hint 是规则模板显式设定（高置信），但 `_phase_partition`
的 AI 二次确认（`ai_confirm_table`）仍跑，AI 看 raw 文本（如"刷新 寻宝老人"）误判回
entity_prefab，覆盖正确路由 → 字段写到错表 → "列名不存在"硬失败。

**原则**：`extras["source"]=="splitter"` 的意图**跳过 AI 二次确认**。规则精确 stem 命中
（策略 0a）置信度高于 LLM 对描述性 raw 的猜测。

**判别**：table_sheet_miss 多 + 意图 raw 含描述性词（"刷新/点击/对话"）→ 查 AI confirm
是否覆盖了 splitter 的正确 table_hint。

## 原则 5：占位符闭环靠 topo 序 + 内联替换，backfill 仅兜底

**问题**：循环依赖链（conv↔option）topo 检测到环 → 回退原序 → 前向引用占位符悬空。

**原则**：
- `_compute_deps` 按 produces 标签建依赖边
- `_topo_order` 把无依赖 leaf 排最前，producer 总在 consumer 前
- 主循环每步写前 `_resolve_placeholders` 用已 produced 内联替换
- topo 正确时占位符内联解析，**无需 backfill**
- `_backfill_forward_refs` 是兜底：主循环后回扫含 `<...>` 的字段补写，仅在 topo 失败时触发

**判别**：ref_broken 多 → 先查 topo 序是否把 producer 排在 consumer 前，再查 produces
标签是否传递（SplitIntent→NLIntent extras["produces"]）。

## 原则 6：字段值格式必须匹配表头期望

**问题**：splitter 写 `rewards="[10090]"`、`pos_list="[[50,0,60]]"`，但表头说明
"多个用英文逗号隔开"期望裸值或逗号分隔，无括号 → verify-repair 多轮失败。

**原则**：写值前对照表头括号内的格式提示：
- "多个用英文逗号隔开" → `10090` / `10090,10091`（无括号）
- "坐标" → `50,0,60`（非 `[[50,0,60]]`）
- 数组字段（如 `pos_list` 本身是 list 类型）→ 按表 schema

**判别**：field_error + verify-repair 多轮仍失败 → 查值格式是否带多余括号。

## 原则 7：parser regex 必须覆盖自然表述变体，非仅 canonical 形式

**问题**：`_CONV_RE` 只认"对话内容"，"弹出对话：老人说"漏匹配 → 整链退化为 LLM 粗拆。

**原则**：regex 设计覆盖自然语言变体：
- 对话触发：`对话[：:]` / `弹出对话` / `老人说` / `NPC说`（非仅"对话内容"）
- 分支触发：`点击'X'后继续说'Y'` / `选'X'就跳到新对话说'Y'`（非仅后者）
- 接任务触发（R7）：`再点'X'接[下]任务` → option_go（function_type=2 + quest_id）。分支对话后常跟"再点'X'接任务"，需独立 regex 捕获，否则 option_go 选项缺失致引用链断
- 实体头：`任务NPC` / `商人` / `守卫` / `传送员` 等可交互实体类

**判别**：splitter 独立测试对 canonical 输入通过但对自然变体失败 → 补 regex 变体覆盖。

## 原则 8：混合指令（跨表 + 非跨表子句）需分段处理

**问题**：整句"新增NPC+对话+任务+刷怪+改奖励"中，"改奖励"是非跨表 modify 子句，
若整句吞进 splitter → modify 丢失；若整句走 parse_multi → 跨表部分超时/粗拆。

**原则**（G7 混合句修复）：
- splitter 产出跨表部分 op
- `multi_intent_splitter` 拆出非跨表子句（modify/set/delete），独立 parse 合并
- 限制：只追加非 add 子句，避免描述性子句被重复 parse 成 add

**判别**：混合指令的 modify/delete 子句丢失 → 查是否被跨表 splitter 吞掉。

## 原则 9：嵌套字段名（含点号）整名匹配表头，verify-repair fix_fields 必须真应用

**问题**（R7 暴露）：表头本身含点号（如 `option_function.function_type:int`、`option_function.data.1.conv_id: int`），但 column_matcher 把字段名拆 `.` 取末段（"function_type"/"conv_id"）→ 未匹配表头整名 → "列名不存在"硬失败。verify-repair Level2 LLM 识别出语义等价列（reason 里说对），但 `fix_fields` 输出未实际替换原 key → 重跑仍失败。同 run 内同字段名一处成功一处失败（LLM 方差）。

**原则**：
- column_matcher 对含点号字段名**先整名匹配表头**（表头 `option_function.data.1.conv_id` 与字段 `option_function.data.1.conv_id` 整名相等），整名未中再拆末段 + alias
- verify-repair Level2 `fix_fields` 应用后**校验 keys 实际变化**：若 LLM reason 识别了语义等价列但 fix_fields 仍含原失败 key → 视为未应用，加 post-LLM validator 强制替换或回退规则 alias
- locator_field 显式指定时（如 `locator_field="id"`）**在该列搜**，勿回退到名称列

**判别**：`match_field 未找到列[X]` 但表头确有 `a.b.X` 整名 → 查 column_matcher 是否拆了末段。verify-repair reason 说"语义等价列 Y"但重跑仍"未找到列[X]" → 查 fix_fields 是否真替换。

## 原则 10：produces 赋值应由关系图驱动，而非 per-template 硬编码

**问题**（R8b 暴露）：splitter 的 `_build_*_intents` 每种链型硬编码 `produces="new_X_id"` + consumer 字段 `<new_X>` 占位符。新链型（pet/mail/...）无模板 → produces 缺失 → `_compute_deps` 无依赖边 → 通用 topo 引擎虽链型无关但无边可建 → 引用一致 0.00。每加一种链型就手写一个 `_build_*` 是过拟合温床。

**原则**：通用 topo 引擎（`_compute_deps`/`_topo_order`/`_capture_produced`/`_resolve_placeholders`）本身链型无关，**缺口仅在 produces 赋值**。应由 `produces_inference` 层在 topo 前对 add 集合做 FK 推断：
- 被 relation `to` 端引用的 add → producer，挂 `produces=new_{stem}_id`（不覆盖 splitter 已标注）
- consumer add 的 FK 字段指向同指令 producer → 字段值替换 `<producer_label>`（仅 consume-eligible：空/`<auto>`/占位符，不覆盖显式已存在 id）

**数据依赖**：`table_relations.json` 声明式 FK（schema 数据，非正则/模板）。新链型只需加 FK 数据，不改代码。

**判别**：跨表链引用一致 0.00 且 producer add 本身成功 → 查 produces 是否标注（splitter 模板漏标 or 无模板链走 LLM 未标）→ 查 `produces_inference` 是否运行 + FK 数据是否齐全。

**边界**：推断层是**必要非充分**基础设施——闭环要求 producer add 写入成功（占位符有源）。producer 写入失败属字段匹配层（原则9 + LLM 字段质量），需分层治理。

## 原则 11：LLM 为链分解主路径，schema 注入约束输出，规则作安全网

**问题**（R8g 暴露）：splitter per-template 是过拟合温床（原则10 已述），但 produces-inference（规则）只能给**已存在的 intent** 补 produces/consumes，不能**创造**缺失的子表 intent。pet 缺 PetEvolveData、mail 产空——规则无法补，必须 LLM 分解。但裸 parse_multi 产单 intent（非链分解）+ 字段名自造失配。

**原则**：跨表链分解以 **LLM 为主路径**，规则作安全网：
- LLM 链分解器（`_llm_chain_decompose`）：候选表（detect hint + relation graph 关联）→ 注入每表 row1+row2 schema + FK 链 → LLM 产 JSON 数组，每元素一原子 op，**fields 用真实表头列名**、produces/consumes 显式标注
- splitter 模板降为 **fast-path**（产 ≥2 intent 时用，如 quest_npc/item）；不完整（<2）时 LLM 接管
- produces-inference（规则）作安全网：LLM 漏标 produces 时补
- verify-repair validator（原则9 part2）作字段匹配保证

**LLM 权重 > 规则**：链分解（哪些表、什么 op、字段映射）LLM 比手写正则强；规则只做确定性兜底（produces 闭环、字段列名校验）。这是泛化的关键——新链型无需手写模板。

**判别**：新链型无 splitter 模板 → 走 LLM 链分解（detect 命中但 splitter 产 <2）；若 LLM 产 ≥2 op 且字段匹配 → 泛化成功。若 LLM 过产/字段失配 → 调 prompt 约束（非加模板）。

## 通用排障决策树

```
跨表链整体失败？
├─ producer 产出率低（<0.5）
│  ├─ splitter 是否用了原文（非 _clean_quotes 后）？→ 原则1
│  ├─ _capture_produced 门控是否过严（ok=None 被跳）？→ 原则3
│  └─ splitter 是否触发（detect_cross_table_action 返回值）？→ 原则7
├─ table_sheet_miss 多
│  ├─ splitter 意图被 AI 二次确认覆盖？→ 原则4
│  └─ table_hint 精确 stem 是否命中（策略 0a）
├─ 整链卡死（单步失败牵连全部）
│  └─ ok=None 是否被当失败？事务是否全量回滚？→ 原则2
├─ ref_broken 多
│  ├─ topo 序是否 producer 在 consumer 前？→ 原则5
│  ├─ produces 标签是否传递到 NLIntent.extras？
│  └─ _lookup 是否匹配（new_xxx ↔ xxx 去/补前缀）
├─ field_error 多
│  ├─ 值格式（括号/逗号）？→ 原则6
│  ├─ 嵌套字段名（含点号）整名是否匹配表头？→ 原则9
│  ├─ verify-repair reason 识别等价列但重跑仍失败？→ fix_fields 未应用（原则9）
│  ├─ column_aliases 是否覆盖该键
│  └─ int 列写占位符（应靠 topo 解析，非硬写）
└─ 混合指令 modify 子句丢失
   └─ 是否被跨表 splitter 吞掉？→ 原则8
```

## 度量基线（跨链通用合格线）

| 指标 | 合格线 | 说明 |
|---|---|---|
| 引用一致率 | ≥0.9 | 占位符闭环成立比例（跨表链核心） |
| producer 产出率 | =1.0 | produces 标注的步实际回传新 ID |
| 定位命中率 | ≥0.9 | table+sheet+操作类型命中 |
| 覆盖度 | ≥0.85 | expected 行操作产出比例（扣异表多余） |
| 字段精准率 | ≥0.9 | 被定位行字段值完全正确 |
| 多余写入 | 0 | 未被 expected 认领的行改动 |

## 适用场景清单

| 场景 | detect 返回 | 涉及表 |
|---|---|---|
| NPC+对话+选项 | npc_dialogue | entity_prefab / interaction(4 sheet) / spawn_world_entity |
| 传送/战斗/奖励 NPC | npc_teleport/combat/reward | entity_prefab / interaction / spawn_world_entity |
| 复合 NPC（对话+奖励+邮件） | npc_composite | + reward / mail |
| 灵兽+进化 | pet / evolve | pet / pet_evolve |
| 道具跨 sheet | item | item(ItemBase+Equipment/Potion/Fabao) |
| 邮件+模板 | mail | mail(MailTemplate+GlobalMail) |
| 任务+刷怪 | quest | quest / spawn_quest_entity |
| 门派神通+技能 | school_ability_spell | school_ability / school_talent / spell_group / spell |
| 战斗+奖励包 | combat_reward | combat / reward |
| 洞府建筑 | residence_building | residence_building / residence / interaction |

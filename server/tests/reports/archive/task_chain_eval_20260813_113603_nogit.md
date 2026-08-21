# task_chain 复合任务链评估报告（excel_LLM Agent 内环验证）

- 生成时间: 2026-08-13 11:36:03
- 样例来源: task_chain.json（3/3 条有效，0 条夹具排除）
- 评估对象: skill=on（TableAgent 全套：parse_multi + cross_table_splitter + OperationOrchestrator 占位符编排 + skill 配置）
- 执行方式: 进程内直接调用 AgentService（真实 codemaker serve LLM），每条链在 resources/ 临时沙箱副本真实执行，跑前/跑后 xlsx 行级差异作为 ground truth
- 核心增量: 链完整性 + 占位符引用一致性（consumer 引用字段 == producer 实际产出 ID）

## 一、总体指标

| 指标 | 说明 | 值 |
|---|---|---|
| 链完整率 | 整链所有 expected 步 status==matched | 0.0000 |
| truth_ok率 | 全步 row_located+字段满分+无异表多余写入 | 0.0000 |
| 引用一致率 | 占位符引用闭环成立的比例（task_chain 核心） | 0.6413 |
| producer产出率 | produces 标注的步实际回传新 ID 的比例 | 0.6822 |
| 定位功能 | 命中正确 table+sheet+操作类型 | 0.8826 |
| 覆盖度 | expected 行操作真正产出比例（扣异表多余） | 0.5848 |
| 精准程度 | 被定位行字段值完全正确比例 | 0.5802 |
| 严格通过率 | 整链 100%命中且无多余写入 | 0.0000 |
| 响应ok率 | Agent 自报告执行成功 | 0.0000 |
| 平均多余写入 | 未被 expected 认领的行改动 | 1.6667 |
| 平均异表写入 | 写到 expected 之外的表 | 0.3333 |
| 平均耗时(ms) | 单链端到端 | 330307.4 |
| P50/P95(ms) | | 159886.6 / 696785.6 |
| 总耗时(s) | | 990.9 |

## 二、失败模式归类（内环优化定位）

| 失败模式 | 计数 | 涉及链 | 优化方向 |
|---|---|---|---|
| parse_or_exec_failed | 0 | - | parse_multi 超时/LLM 不可用 → 增大超时/降级 splitter 兜底 |
| table_sheet_miss | 5 | 1,2,3 | 路由或 sheet 别名缺失 → 补 table_context/sheet_aliases skill |
| row_missing | 12 | 1,2,3 | add 未落行/modify 未定位行 → 查列定位与主键自增逻辑 |
| field_error | 14 | 1,2,3 | 字段值写错/枚举未解析/类型不符 → 补 column_aliases/enum_mappings |
| ref_broken | 4 | 1,2,3 | 占位符替换错误或 consumer 字段名错 → 修 OperationOrchestrator._capture_produced 列名派生 |
| producer_not_resolved | 22 | 1,2,3 | producer 新 ID 未回传 result_rows → 修 _append_row 主键回传/produces 标注 |
| extra_writes | 1 | 1 | 过度级联/误改它表 → 收紧 cascade_rules/反模式拦截 |
| precondition_missing | 0 | - | 夹具与配表不一致（非 Agent 缺陷）→ 同步测试夹具或配表 |

## 三、每条链详情

### 链 1: 新增一个任务NPC叫'药铺掌柜'，model_id 1020，放在space_id 10002坐标(110,0,40)，玩家点击后弹出对话：掌柜说'最近老朽药铺灵草告急，能否帮我采10株灵草？'选项1'好，我帮你采'，选项2'改日再来'。点击'好，我帮你采'后掌柜继续说'多谢！灵草长在space_id 10003，采满10株回来交给我。'，再点'这就去采'接下任务。配置对应支线任务'灵草采集'，任务ID 250021，任务组group_id 251，描述'帮药铺掌柜采集10株灵草'，目标类型Collect，目标数据'item_id:[5001],count:10'，完成奖励reward_id 10091。同时把reward_id 10091的名称改为'灵草采集奖励'。

- 响应ok: False | 链完整: False | 严格通过: False | truth_ok: False
- 定位 0.89 | 覆盖 0.67 | 精准 0.55 | 引用一致 0.77 (10/13) | producer产出 7/8
- 多余写入 1 (异表 1) | 耗时 696786ms
- 错误: 失败：transaction_rollback - 跳过：依赖的前序 producer {6} 已失败（独立任务不受影响）

| # | table.sheet | op | produces | 状态 | 字段分 | matched_op |
|---|---|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | new_prefab_id | 🟡 partial | 0.67 | 有 |
| 2 | interaction.xlsx.Interaction | add | new_interaction_id | ✅ matched | 1.00 | 有 |
| 3 | interaction.xlsx.InteractionConv | add | new_conv_id_1 | 🟠 located_only | 0.00 | 有 |
| 4 | interaction.xlsx.InteractionConvOption | add | option_accept_id | ✅ matched | 1.00 | 有 |
| 5 | interaction.xlsx.InteractionConvOption | add | option_decline_id | ✅ matched | 1.00 | 有 |
| 6 | interaction.xlsx.InteractionConv | add | new_conv_id_2 | ❌ missing | 0.00 | 无 |
| 7 | interaction.xlsx.InteractionConvOption | add | option_go_id | 🟡 partial | 0.67 | 有 |
| 8 | quest/quest.xlsx.Quest | add | new_quest_id | 🟡 partial | 0.29 | 有 |
| 9 | reward.xlsx.Reward | modify | - | ❌ missing | 0.00 | 无 |

占位符引用闭环校验：
| consumer步# | 字段 | 占位符 | producer步# | 期望ID | 实际值 | 闭环 |
|---|---|---|---|---|---|---|
| 1 | prefab_id | <new_prefab_id> | 1 | 10013112008 | 10013112008 | ✅ |
| 1 | interaction_id | <new_interaction_id> | 2 | 10065 | 10065 | ✅ |
| 2 | interaction_id | <new_interaction_id> | 2 | 10065 | 10065 | ✅ |
| 2 | effect.data.3006.conv_id | <new_conv_id_1> | 3 | 20 | 20 | ✅ |
| 3 | conv_id | <new_conv_id_1> | 3 | 20 | 20 | ✅ |
| 3 | options[0] | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 3 | options[1] | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 4 | option_id | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 4 | option_function.data.1.conv_id | <new_conv_id_2> | 6 | None | None | ❌ |
| 5 | option_id | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 6 | conv_id | <new_conv_id_2> | 6 | None | None | ❌ |
| 6 | options[0] | <option_go_id> | 7 | 23 | None | ❌ |
| 7 | option_id | <option_go_id> | 7 | 23 | 23 | ✅ |

### 链 2: 新增一个任务NPC叫'村长'，model_id 1005，放在space_id 10004坐标(200,0,100)，玩家点击后弹出对话：村长说'年轻人，村外山贼骚扰，去找守卫队长商议剿匪，他在space_id 10005坐标(30,0,70)。'选项1'我这就去'，选项2'没空'。点击'我这就去'后村长继续说'好，守卫队长正等着你，去space_id 10005找他。'，再点'明白'接下任务。在space_id 10005新增守卫队长NPC，model_id 1021，玩家点击守卫队长弹出对话：守卫队长说'村长派你来的？山贼头目盘踞在space_id 10006，请击杀他。'选项1'领命'，选项2'容我准备'。点击'领命'后守卫队长继续说'多谢！击杀山贼头目后回来领赏。'，再点'出发'接下任务。配置对应支线任务'剿匪令'，任务ID 250022，任务组group_id 252，描述'协助守卫队长击杀山贼头目'，目标类型Combat，目标数据'combat_id:[25002201],npc_id:5030,count:1'，完成奖励reward_id 10092。在space_id 10006坐标(60,0,50)刷新山贼头目(npc_id 5030)供玩家击杀。同时把reward_id 10092的名称改为'剿匪奖励'。

- 响应ok: False | 链完整: False | 严格通过: False | truth_ok: False
- 定位 0.84 | 覆盖 0.42 | 精准 0.52 | 引用一致 0.35 (11/31) | producer产出 8/18
- 多余写入 4 (异表 0) | 耗时 159887ms
- 错误: 失败：match_field - 未找到列[模型]

| # | table.sheet | op | produces | 状态 | 字段分 | matched_op |
|---|---|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | new_prefab_id_1 | 🟡 partial | 0.33 | 有 |
| 2 | interaction.xlsx.Interaction | add | new_interaction_id_1 | ✅ matched | 1.00 | 有 |
| 3 | interaction.xlsx.InteractionConv | add | new_conv_id_1 | 🟠 located_only | 0.00 | 有 |
| 4 | interaction.xlsx.InteractionConvOption | add | option_accept_id | ✅ matched | 1.00 | 有 |
| 5 | interaction.xlsx.InteractionConvOption | add | option_decline_id | ✅ matched | 1.00 | 有 |
| 6 | interaction.xlsx.InteractionConv | add | new_conv_id_2 | ❌ missing | 0.00 | 无 |
| 7 | interaction.xlsx.InteractionConvOption | add | option_go_id_1 | 🟡 partial | 0.33 | 有 |
| 8 | entity_prefab.xlsx.Base | add | new_prefab_id_2 | ❌ missing | 0.00 | 无 |
| 9 | interaction.xlsx.Interaction | add | new_interaction_id_2 | ❌ missing | 0.00 | 无 |
| 10 | interaction.xlsx.InteractionConv | add | new_conv_id_3 | ❌ missing | 0.00 | 无 |
| 11 | interaction.xlsx.InteractionConvOption | add | option_accept2_id | ❌ missing | 0.00 | 无 |
| 12 | interaction.xlsx.InteractionConvOption | add | option_decline2_id | ❌ missing | 0.00 | 无 |
| 13 | interaction.xlsx.InteractionConv | add | new_conv_id_4 | ❌ missing | 0.00 | 无 |
| 14 | interaction.xlsx.InteractionConvOption | add | option_go_id_2 | ❌ missing | 0.00 | 无 |
| 15 | quest/quest.xlsx.Quest | add | new_quest_id | 🟡 partial | 0.25 | 有 |
| 16 | quest/spawn_quest_entity.xlsx.SpawnQuestEntity | add | new_spawn_quest_id | 🟡 partial | 0.25 | 有 |
| 17 | spawn_world_entity.xlsx.SpawnWorldEntity | add | new_spawn_id_1 | ❌ missing | 0.00 | 无 |
| 18 | spawn_world_entity.xlsx.SpawnWorldEntity | add | new_spawn_id_2 | ❌ missing | 0.00 | 无 |
| 19 | reward.xlsx.Reward | modify | - | ❌ missing | 0.00 | 无 |

占位符引用闭环校验：
| consumer步# | 字段 | 占位符 | producer步# | 期望ID | 实际值 | 闭环 |
|---|---|---|---|---|---|---|
| 1 | prefab_id | <new_prefab_id_1> | 1 | 10013112008 | 10013112008 | ✅ |
| 1 | interaction_id | <new_interaction_id_1> | 2 | 10065 | 10065 | ✅ |
| 2 | interaction_id | <new_interaction_id_1> | 2 | 10065 | 10065 | ✅ |
| 2 | effect.data.3006.conv_id | <new_conv_id_1> | 3 | 20 | 20 | ✅ |
| 3 | conv_id | <new_conv_id_1> | 3 | 20 | 20 | ✅ |
| 3 | options[0] | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 3 | options[1] | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 4 | option_id | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 4 | option_function.data.1.conv_id | <new_conv_id_2> | 6 | None | None | ❌ |
| 5 | option_id | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 6 | conv_id | <new_conv_id_2> | 6 | None | None | ❌ |
| 6 | options[0] | <option_go_id_1> | 7 | 23 | None | ❌ |
| 7 | option_id | <option_go_id_1> | 7 | 23 | 23 | ✅ |
| 8 | prefab_id | <new_prefab_id_2> | 8 | None | None | ❌ |
| 8 | interaction_id | <new_interaction_id_2> | 9 | None | None | ❌ |
| 9 | interaction_id | <new_interaction_id_2> | 9 | None | None | ❌ |
| 9 | effect.data.3006.conv_id | <new_conv_id_3> | 10 | None | None | ❌ |
| 10 | conv_id | <new_conv_id_3> | 10 | None | None | ❌ |
| 10 | options[0] | <option_accept2_id> | 11 | None | None | ❌ |
| 10 | options[1] | <option_decline2_id> | 12 | None | None | ❌ |
| 11 | option_id | <option_accept2_id> | 11 | None | None | ❌ |
| 11 | option_function.data.1.conv_id | <new_conv_id_4> | 13 | None | None | ❌ |
| 12 | option_id | <option_decline2_id> | 12 | None | None | ❌ |
| 13 | conv_id | <new_conv_id_4> | 13 | None | None | ❌ |
| 13 | options[0] | <option_go_id_2> | 14 | None | None | ❌ |
| 14 | option_id | <option_go_id_2> | 14 | None | None | ❌ |
| 16 | spawn_id | <new_spawn_quest_id> | 16 | 25001004 | 25001004 | ✅ |
| 17 | spawn_id | <new_spawn_id_1> | 17 | None | None | ❌ |
| 17 | entity_prefab_id | <new_prefab_id_1> | 1 | 10013112008 | None | ❌ |
| 18 | spawn_id | <new_spawn_id_2> | 18 | None | None | ❌ |
| 18 | entity_prefab_id | <new_prefab_id_2> | 8 | None | None | ❌ |

### 链 3: 新增一个任务NPC叫'公会干事'，model_id 1030，放在space_id 10007坐标(10,0,10)，玩家点击后弹出对话：干事说'本周公会三项任务，逐一完成有额外奖励。'选项1'接下全部'，选项2'太忙了'。点击'接下全部'后干事继续说'好！第一项击杀5只野狼，第二项收集3株铁矿石，第三项击败野外BOSS，完成找我领赏。'，再点'明白'接下任务。配置三个支线任务：任务1'公会任务-剿狼'任务ID 250031任务组group_id 253描述'击杀5只野狼'目标类型Combat目标数据'combat_id:[25003101],npc_id:5040,count:5'完成奖励reward_id 10093；任务2'公会任务-采矿'任务ID 250032任务组group_id 253描述'收集3株铁矿石'目标类型Collect目标数据'item_id:[5002],count:3'完成奖励reward_id 10094；任务3'公会任务-讨伐'任务ID 250033任务组group_id 253描述'击败野外BOSS'目标类型Combat目标数据'combat_id:[25003301],npc_id:5041,count:1'完成奖励reward_id 10095。同时把reward_id 10093的名称改为'剿狼奖励'。

- 响应ok: False | 链完整: False | 严格通过: False | truth_ok: False
- 定位 0.92 | 覆盖 0.67 | 精准 0.67 | 引用一致 0.80 (12/15) | producer产出 8/11
- 多余写入 0 (异表 0) | 耗时 134250ms
- 错误: 失败：transaction_rollback - 跳过：依赖的前序 producer {6} 已失败（独立任务不受影响）

| # | table.sheet | op | produces | 状态 | 字段分 | matched_op |
|---|---|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | new_prefab_id | 🟡 partial | 0.67 | 有 |
| 2 | interaction.xlsx.Interaction | add | new_interaction_id | ✅ matched | 1.00 | 有 |
| 3 | interaction.xlsx.InteractionConv | add | new_conv_id_1 | 🟠 located_only | 0.00 | 有 |
| 4 | interaction.xlsx.InteractionConvOption | add | option_accept_id | ✅ matched | 1.00 | 有 |
| 5 | interaction.xlsx.InteractionConvOption | add | option_decline_id | ✅ matched | 1.00 | 有 |
| 6 | interaction.xlsx.InteractionConv | add | new_conv_id_2 | ❌ missing | 0.00 | 无 |
| 7 | interaction.xlsx.InteractionConvOption | add | option_go_id | 🟡 partial | 0.67 | 有 |
| 8 | quest/quest.xlsx.Quest | add | new_quest_id_1 | 🟡 partial | 0.29 | 有 |
| 9 | quest/quest.xlsx.Quest | add | new_quest_id_2 | ❌ missing | 0.00 | 无 |
| 10 | quest/quest.xlsx.Quest | add | new_quest_id_3 | ❌ missing | 0.00 | 无 |
| 11 | spawn_world_entity.xlsx.SpawnWorldEntity | add | new_spawn_id | 🟡 partial | 0.75 | 有 |
| 12 | reward.xlsx.Reward | modify | - | ❌ missing | 0.00 | 无 |

占位符引用闭环校验：
| consumer步# | 字段 | 占位符 | producer步# | 期望ID | 实际值 | 闭环 |
|---|---|---|---|---|---|---|
| 1 | prefab_id | <new_prefab_id> | 1 | 10013112008 | 10013112008 | ✅ |
| 1 | interaction_id | <new_interaction_id> | 2 | 10065 | 10065 | ✅ |
| 2 | interaction_id | <new_interaction_id> | 2 | 10065 | 10065 | ✅ |
| 2 | effect.data.3006.conv_id | <new_conv_id_1> | 3 | 20 | 20 | ✅ |
| 3 | conv_id | <new_conv_id_1> | 3 | 20 | 20 | ✅ |
| 3 | options[0] | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 3 | options[1] | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 4 | option_id | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 4 | option_function.data.1.conv_id | <new_conv_id_2> | 6 | None | None | ❌ |
| 5 | option_id | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 6 | conv_id | <new_conv_id_2> | 6 | None | None | ❌ |
| 6 | options[0] | <option_go_id> | 7 | 23 | None | ❌ |
| 7 | option_id | <option_go_id> | 7 | 23 | 23 | ✅ |
| 11 | spawn_id | <new_spawn_id> | 11 | 10013111012 | 10013111012 | ✅ |
| 11 | entity_prefab_id | <new_prefab_id> | 1 | 10013112008 | 10013112008 | ✅ |

## 四、表现最差链 Top5（优先优化目标）

| cid | 链完整 | 引用一致 | 覆盖 | 精准 | input |
|---|---|---|---|---|---|
| 2 | False | 0.35 | 0.42 | 0.52 | 新增一个任务NPC叫'村长'，model_id 1005，放在space |
| 1 | False | 0.77 | 0.67 | 0.55 | 新增一个任务NPC叫'药铺掌柜'，model_id 1020，放在spa |
| 3 | False | 0.80 | 0.67 | 0.67 | 新增一个任务NPC叫'公会干事'，model_id 1030，放在spa |

## 五、内环优化建议

- 表/sheet 路由失误 5 处：补 table_context.yaml 关键词与 sheet_aliases.yaml，覆盖 NPC/对话/刷新等跨表场景。
- 行操作未产出 12 处：核查 add 主键自增与 modify 行定位（首列空/前缀剥离）链路。
- 字段错误 14 处：补 column_aliases / enum_mappings / value_constraints，强化枚举值预解析与类型校验。
- 引用断裂 4 处 + producer 未产出 22 处：这是 task_chain 核心瓶颈。核查 OperationOrchestrator._capture_produced 主键列名派生（首列 col==1 优先）与 _resolve_placeholders 占位符替换覆盖；确保 add 结果 result_rows 回传主键新值，produces 标签与占位符名对齐。
- 多余写入 1 处：收紧 cascade_rules 与 anti_patterns，防止过度级联改写 expected 之外的表。

注意事项：
- ⚪ 夹具缺失表示 expected 的 row_key 在 resources/ 真实数据中不存在（非 Agent 缺陷），已排除出统计；若需评估该链请同步夹具或配表。
- 引用一致性是 task_chain 区别于单表用例的核心指标：producer 步产出的新 ID 必须被consumer 步正确引用写入，否则跨表配置在运行期无法关联。
- 每条链在独立临时沙箱执行，互不影响；跑完即删，不污染真实 resources/。
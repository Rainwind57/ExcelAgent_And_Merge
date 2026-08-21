# task_chain 复合任务链评估报告（excel_LLM Agent 内环验证）

- 生成时间: 2026-08-13 14:27:36
- 样例来源: task_chain.json（1/1 条有效，0 条夹具排除）
- 评估对象: skill=on（TableAgent 全套：parse_multi + cross_table_splitter + OperationOrchestrator 占位符编排 + skill 配置）
- 执行方式: 进程内直接调用 AgentService（真实 codemaker serve LLM），每条链在 resources/ 临时沙箱副本真实执行，跑前/跑后 xlsx 行级差异作为 ground truth
- 核心增量: 链完整性 + 占位符引用一致性（consumer 引用字段 == producer 实际产出 ID）

## 一、总体指标

| 指标 | 说明 | 值 |
|---|---|---|
| 链完整率 | 整链所有 expected 步 status==matched | 0.0000 |
| truth_ok率 | 全步 row_located+字段满分+无异表多余写入 | 0.0000 |
| 引用一致率 | 占位符引用闭环成立的比例（task_chain 核心） | 0.9375 |
| producer产出率 | produces 标注的步实际回传新 ID 的比例 | 1.0000 |
| 定位功能 | 命中正确 table+sheet+操作类型 | 1.0000 |
| 覆盖度 | expected 行操作真正产出比例（扣异表多余） | 1.0000 |
| 精准程度 | 被定位行字段值完全正确比例 | 0.7803 |
| 严格通过率 | 整链 100%命中且无多余写入 | 0.0000 |
| 响应ok率 | Agent 自报告执行成功 | 1.0000 |
| 平均多余写入 | 未被 expected 认领的行改动 | 4.0000 |
| 平均异表写入 | 写到 expected 之外的表 | 0.0000 |
| 平均耗时(ms) | 单链端到端 | 70155.3 |
| P50/P95(ms) | | 70155.3 / 70155.3 |
| 总耗时(s) | | 70.2 |

## 二、失败模式归类（内环优化定位）

| 失败模式 | 计数 | 涉及链 | 优化方向 |
|---|---|---|---|
| parse_or_exec_failed | 0 | - | parse_multi 超时/LLM 不可用 → 增大超时/降级 splitter 兜底 |
| table_sheet_miss | 0 | - | 路由或 sheet 别名缺失 → 补 table_context/sheet_aliases skill |
| row_missing | 0 | - | add 未落行/modify 未定位行 → 查列定位与主键自增逻辑 |
| field_error | 5 | 1 | 字段值写错/枚举未解析/类型不符 → 补 column_aliases/enum_mappings |
| ref_broken | 1 | 1 | 占位符替换错误或 consumer 字段名错 → 修 OperationOrchestrator._capture_produced 列名派生 |
| producer_not_resolved | 0 | - | producer 新 ID 未回传 result_rows → 修 _append_row 主键回传/produces 标注 |
| extra_writes | 0 | - | 过度级联/误改它表 → 收紧 cascade_rules/反模式拦截 |
| precondition_missing | 0 | - | 夹具与配表不一致（非 Agent 缺陷）→ 同步测试夹具或配表 |

## 三、每条链详情

### 链 1: 新增一个任务NPC叫'寻宝老人'，model_id 1019，放在space_id 10001坐标(120,0,80)，玩家点击后弹出对话：老人说'年轻人，老朽有一事相求——我祖传的玉佩被山贼头目夺走，能否帮我寻回？'选项1'我帮你寻回'，选项2'我现在没空，稍后再来'。点击'我帮你寻回'后老人继续说'多谢！那山贼头目盘踞在space_id 10008，请击杀他取回玉佩，必有重谢。'，再点'我这就去'接下任务。配置对应支线任务'寻回玉佩'，任务ID 250020，任务组group_id 250，描述'帮寻宝老人从山贼头目处夺回祖传玉佩'，目标类型Combat，目标数据'combat_id:[25002001],npc_id:5025,count:1'，完成奖励reward_id 10090。在space_id 10008坐标(50,0,60)刷新山贼头目(npc_id 5025)供玩家击杀。同时把reward_id 10090的名称改为'寻回玉佩奖励'。

- 响应ok: True | 链完整: False | 严格通过: False | truth_ok: False
- 定位 1.00 | 覆盖 1.00 | 精准 0.78 | 引用一致 0.94 (15/16) | producer产出 10/10
- 多余写入 4 (异表 0) | 耗时 70155ms

| # | table.sheet | op | produces | 状态 | 字段分 | matched_op |
|---|---|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | new_prefab_id | 🟡 partial | 0.67 | 有 |
| 2 | interaction.xlsx.Interaction | add | new_interaction_id | ✅ matched | 1.00 | 有 |
| 3 | interaction.xlsx.InteractionConv | add | new_conv_id_1 | ✅ matched | 1.00 | 有 |
| 4 | interaction.xlsx.InteractionConvOption | add | option_accept_id | ✅ matched | 1.00 | 有 |
| 5 | interaction.xlsx.InteractionConvOption | add | option_decline_id | ✅ matched | 1.00 | 有 |
| 6 | interaction.xlsx.InteractionConv | add | new_conv_id_2 | ✅ matched | 1.00 | 有 |
| 7 | interaction.xlsx.InteractionConvOption | add | option_go_id | 🟡 partial | 0.67 | 有 |
| 8 | quest/quest.xlsx.Quest | add | new_quest_id | 🟡 partial | 0.25 | 有 |
| 9 | quest/spawn_quest_entity.xlsx.SpawnQuestEntity | add | new_spawn_quest_id | 🟡 partial | 0.25 | 有 |
| 10 | spawn_world_entity.xlsx.SpawnWorldEntity | add | new_spawn_id | 🟡 partial | 0.75 | 有 |
| 11 | reward.xlsx.Reward | modify | - | ✅ matched | 1.00 | 有 |

占位符引用闭环校验：
| consumer步# | 字段 | 占位符 | producer步# | 期望ID | 实际值 | 闭环 |
|---|---|---|---|---|---|---|
| 1 | prefab_id | <new_prefab_id> | 1 | 10013112008 | 10013112008 | ✅ |
| 1 | interaction_id | <new_interaction_id> | 2 | 10065 | 10065 | ✅ |
| 2 | interaction_id | <new_interaction_id> | 2 | 10065 | 10065 | ✅ |
| 2 | effect.data.3006.conv_id | <new_conv_id_1> | 3 | 21 | 20 | ❌ |
| 3 | conv_id | <new_conv_id_1> | 3 | 21 | 21 | ✅ |
| 3 | options[0] | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 3 | options[1] | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 4 | option_id | <option_accept_id> | 4 | 24 | 24 | ✅ |
| 4 | option_function.data.1.conv_id | <new_conv_id_2> | 6 | 20 | 20 | ✅ |
| 5 | option_id | <option_decline_id> | 5 | 22 | 22 | ✅ |
| 6 | conv_id | <new_conv_id_2> | 6 | 20 | 20 | ✅ |
| 6 | options[0] | <option_go_id> | 7 | 23 | 23 | ✅ |
| 7 | option_id | <option_go_id> | 7 | 23 | 23 | ✅ |
| 9 | spawn_id | <new_spawn_quest_id> | 9 | 25001004 | 25001004 | ✅ |
| 10 | spawn_id | <new_spawn_id> | 10 | 10013111012 | 10013111012 | ✅ |
| 10 | entity_prefab_id | <new_prefab_id> | 1 | 10013112008 | 10013112008 | ✅ |

## 四、表现最差链 Top5（优先优化目标）

| cid | 链完整 | 引用一致 | 覆盖 | 精准 | input |
|---|---|---|---|---|---|
| 1 | False | 0.94 | 1.00 | 0.78 | 新增一个任务NPC叫'寻宝老人'，model_id 1019，放在spa |

## 五、内环优化建议

- 字段错误 5 处：补 column_aliases / enum_mappings / value_constraints，强化枚举值预解析与类型校验。
- 引用断裂 1 处 + producer 未产出 0 处：这是 task_chain 核心瓶颈。核查 OperationOrchestrator._capture_produced 主键列名派生（首列 col==1 优先）与 _resolve_placeholders 占位符替换覆盖；确保 add 结果 result_rows 回传主键新值，produces 标签与占位符名对齐。

注意事项：
- ⚪ 夹具缺失表示 expected 的 row_key 在 resources/ 真实数据中不存在（非 Agent 缺陷），已排除出统计；若需评估该链请同步夹具或配表。
- 引用一致性是 task_chain 区别于单表用例的核心指标：producer 步产出的新 ID 必须被consumer 步正确引用写入，否则跨表配置在运行期无法关联。
- 每条链在独立临时沙箱执行，互不影响；跑完即删，不污染真实 resources/。
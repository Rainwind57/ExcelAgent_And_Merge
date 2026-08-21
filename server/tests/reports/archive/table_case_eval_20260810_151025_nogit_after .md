# 后端 Agent skill on/off A/B 测试报告

- 生成时间: 2026-08-10 15:10:25
- 样例来源: table_operation_test_cases.json（6 条）
- 执行方式: 进程内直接调用 AgentService（真实 codemaker serve LLM），每条样例在 resources/ 临时沙箱副本内真实执行增删改，跑前/跑后 xlsx 差异作为 ground truth
- skill=off: TableAgent(enable_skill=False)，列/行定位仅靠原始表头，不挂列别名/行规则/反模式/短形式
- skill=on : TableAgent(enable_skill=True)，正常加载 server/agent/excel/skills/ 全部 skill

## 一、总体指标对比

| 指标 | 说明 | skill=off | skill=on | 变化 |
|---|---|---|---|---|
| 定位功能 | 命中正确 table+sheet+操作类型的比例 | 0.5556 | 0.6389 | +0.0833 |
| 覆盖度 | expected 行操作中真正被产出的比例（含级联多表） | 0.4444 | 0.5000 | +0.0556 |
| 精准程度 | 被定位到的行里，字段值完全正确的比例 | 0.4213 | 0.4023 | -0.0190 |
| 严格通过率 | 整条样例 expected_answer 100%命中且无多余写入 | 0.1667 | 0.1667 | +0.0000 |
| 响应ok率 | Agent 自报告执行成功比例 | 0.3333 | 0.3333 | +0.0000 |
| 平均多余写入 | 未被 expected 认领的行改动数（越低越好） | 1.1667 | 1.3333 | -0.1666 |
| 平均耗时(ms) | 单条指令端到端耗时（含二次确认续传） | 116686.2 | 236947.9 | -120261.7 |
| P50耗时(ms) | | 133434.4 | 139794.0 | |
| P95耗时(ms) | | 223258.5 | 801532.9 | |
| 需二次确认比例 | 触发级联删除等确认流程的比例 | 0.0000 | 0.0000 | |
| 总耗时(s) | 本轮全部样例累计耗时 | 700.1 | 1421.7 | |

## 二、按操作类型细分（skill=on）

| 操作类型 | n | 定位率 | 覆盖率 | 字段精准度 | (对比 off) |
|---|---|---|---|---|---|
| add | 14 | 0.7857 | 0.7857 | 0.6667 | off: locate=0.7143 cov=0.7143 acc=0.7333 |
| modify | 3 | 0.3333 | 0.3333 | 1.0000 | off: locate=0.3333 cov=0.3333 acc=1.0000 |
| delete | 1 | 1.0000 | 1.0000 | 1.0000 | off: locate=1.0000 cov=1.0000 acc=1.0000 |

## 三、每个样例详细运行情况

### 样例 1: 新增一个NPC叫铁匠老张，model_id为1015，放在space_id 10008的场景坐标(60,0,30)，玩家点击后弹出对话，对话内容为'欢迎来到铁匠铺，我可以帮你锻造装备。'，选项为'好的，我要锻造'和'离开'

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 1.00 | 0.83 |
| 覆盖度(行操作产出率) | 1.00 | 0.33 |
| 精准程度(字段值正确率) | 0.74 | 0.23 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 3 | 6 |
| 耗时(ms) | 133434 | 139794 |
| 错误 | 失败：pk_conflict - ID [100081000] 已被占用 | 失败 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | 🟡 部分匹配 (0.67) | 🟡 部分匹配 (0.67) |
| 2 | interaction.xlsx.Interaction | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 3 | interaction.xlsx.InteractionConv | add | 🟠 定位到但字段不符 (0.00) | 🟠 定位到但字段不符 (0.00) |
| 4 | interaction.xlsx.InteractionConvOption | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 5 | interaction.xlsx.InteractionConvOption | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 6 | spawn_world_entity.xlsx.SpawnWorldEntity | add | 🟡 部分匹配 (0.75) | ❌ 未产生 (0.00) |

### 样例 2: 把entity_prefab中prefab_id为8004的NPC名字从'青龙'改成'青龙堂主'

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | True | True |
| 定位功能(table/sheet命中率) | 1.00 | 1.00 |
| 覆盖度(行操作产出率) | 1.00 | 1.00 |
| 精准程度(字段值正确率) | 1.00 | 1.00 |
| 严格通过 | True | True |
| 多余写入(误写行数) | 0 | 0 |
| 耗时(ms) | 21141 | 22302 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | modify | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |

### 样例 3: 删除prefab_id为8005的NPC白虎的所有相关配置，包括entity_prefab表和spawn_world_entity表中的记录

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | True | False |
| 定位功能(table/sheet命中率) | 1.00 | 1.00 |
| 覆盖度(行操作产出率) | 1.00 | 1.00 |
| 精准程度(字段值正确率) | 1.00 | 1.00 |
| 严格通过 | False | True |
| 多余写入(误写行数) | 1 | 0 |
| 耗时(ms) | 30899 | 42053 |
| 错误 | - | 失败：locate_row - 未找到 8005(mode=exact) |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | delete | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 2 | spawn_world_entity.xlsx.SpawnWorldEntity | delete | ⚪ 夹具缺失(跳过) (0.00) | ⚪ 夹具缺失(跳过) (0.00) |

### 样例 4: 新增一个传送NPC叫'传送使者'，model_id为1016，放在space_id 10008坐标(10,0,10)，玩家点击后传送到space_id 10001

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.67 | 1.00 |
| 覆盖度(行操作产出率) | 0.33 | 1.00 |
| 精准程度(字段值正确率) | 0.25 | 0.64 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 2 | 1 |
| 耗时(ms) | 65437 | 801533 |
| 错误 | 失败：pk_conflict - ID [1065601] 已被占用 | 失败：coerce_value - 列[3001: 战斗ID]类型为 int，值''无法转为整数且无枚举映射，已阻止写入 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | 🟡 部分匹配 (0.67) | 🟡 部分匹配 (0.67) |
| 2 | interaction.xlsx.Interaction | add | 🟡 部分匹配 (0.50) | 🟡 部分匹配 (0.50) |
| 3 | spawn_world_entity.xlsx.SpawnWorldEntity | add | ❌ 未产生 (0.00) | 🟡 部分匹配 (0.75) |

### 样例 5: 修改interaction_id为10001的触发半径从5改为8

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | True | True |
| 定位功能(table/sheet命中率) | 0.00 | 0.00 |
| 覆盖度(行操作产出率) | 0.00 | 0.00 |
| 精准程度(字段值正确率) | 0.00 | 0.00 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 0 | 0 |
| 耗时(ms) | 13514 | 23260 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | interaction.xlsx.Interaction | modify | ⚪ 夹具缺失(跳过) (0.00) | ⚪ 夹具缺失(跳过) (0.00) |

### 样例 6: 新增一个擂台NPC叫'擂台挑战者'，model_id为1012，放在space_id 10008坐标(30,0,40)，玩家点击后进入战斗，战斗ID为102

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.67 | 0.67 |
| 覆盖度(行操作产出率) | 0.33 | 0.33 |
| 精准程度(字段值正确率) | 0.54 | 0.54 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 2 | 1 |
| 耗时(ms) | 220580 | 340777 |
| 错误 | 失败：pk_conflict - ID [1065601] 已被占用 | 失败：pk_conflict - ID [1065601] 已被占用 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 2 | interaction.xlsx.Interaction | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 3 | spawn_world_entity.xlsx.SpawnWorldEntity | add | 🟡 部分匹配 (0.75) | 🟡 部分匹配 (0.75) |

### 样例 7: 修改conv_id为1的对话内容为'你好，旅行者，欢迎来到我们的村庄。'

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | True | True |
| 定位功能(table/sheet命中率) | 0.00 | 0.00 |
| 覆盖度(行操作产出率) | 0.00 | 0.00 |
| 精准程度(字段值正确率) | 0.00 | 0.00 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 0 | 0 |
| 耗时(ms) | 36266 | 25522 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | interaction.xlsx.InteractionConv | modify | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |

### 样例 8: 给conv_id为4的帮派青龙堂总管对话新增一个选项，选项内容为'我想了解帮派贡献'，点击后跳转到新对话，新对话内容为'帮派贡献可以通过完成帮派任务和捐献资源来获得。'

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.00 | 0.33 |
| 覆盖度(行操作产出率) | 0.00 | 0.33 |
| 精准程度(字段值正确率) | 0.00 | 0.00 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 0 | 0 |
| 耗时(ms) | 223258 | 91760 |
| 错误 | 失败：match_field - 未找到列[所属对话] | 失败：pk_conflict - ID [4] 已被占用 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | interaction.xlsx.InteractionConv | modify | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 2 | interaction.xlsx.InteractionConvOption | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 3 | interaction.xlsx.InteractionConv | add | ❌ 未产生 (0.00) | 🟠 定位到但字段不符 (0.00) |

## 四、skill=on 表现最差的样例（覆盖度+精准度最低 Top10）

| cid | input | 覆盖度 | 精准度 | 定位率 | 错误 |
|---|---|---|---|---|---|
| 5 | 修改interaction_id为10001的触发半径从5改为8 | 0.00 | 0.00 | 0.00 | - |
| 7 | 修改conv_id为1的对话内容为'你好，旅行者，欢迎来到我们的村庄。' | 0.00 | 0.00 | 0.00 | - |
| 8 | 给conv_id为4的帮派青龙堂总管对话新增一个选项，选项内容为'我想了解帮派贡 | 0.33 | 0.00 | 0.33 | 失败：pk_conflict - ID [4] 已被占用 |
| 1 | 新增一个NPC叫铁匠老张，model_id为1015，放在space_id 10 | 0.33 | 0.23 | 0.83 | 失败 |
| 6 | 新增一个擂台NPC叫'擂台挑战者'，model_id为1012，放在space_ | 0.33 | 0.54 | 0.67 | 失败：pk_conflict - ID [1065601] 已被占用 |
| 4 | 新增一个传送NPC叫'传送使者'，model_id为1016，放在space_i | 1.00 | 0.64 | 1.00 | 失败：coerce_value - 列[3001: 战斗ID]类型为 int，值''无法转为整数且无枚举映射，已阻止写入 |
| 2 | 把entity_prefab中prefab_id为8004的NPC名字从'青龙' | 1.00 | 1.00 | 1.00 | - |
| 3 | 删除prefab_id为8005的NPC白虎的所有相关配置，包括entity_p | 1.00 | 1.00 | 1.00 | 失败：locate_row - 未找到 8005(mode=exact) |

## 五、夹具错误清单（D7，共 2 条，已排除出统计）

| cid | input | 错误类型 | 详情 |
|---|---|---|---|
| 3 | 删除prefab_id为8005的NPC白虎的所有相关配置，包括entity_p | modify_delete_row_missing | delete 用例 row_key[entity_prefab_id]=8005 在 pristine 未找到目标行 |
| 5 | 修改interaction_id为10001的触发半径从5改为8 | modify_delete_row_missing | modify 用例 row_key[interaction_id]=10001 在 pristine 未找到目标行 |

## 六、结论总结

- skill 挂载后定位功能变化: +0.0833（提升）
- skill 挂载后覆盖度变化: +0.0556（提升）
- skill 挂载后精准程度变化: -0.0190（下降）
- skill 挂载后严格通过率变化: +0.0000
- skill 挂载后耗时变化: -120261.7ms（变慢，正数=on比off快，因为定义为 off-on）
- 综合判定: skill 增强 有效

注意事项：
- ⚪ 夹具缺失(跳过) 表示 expected_answer 引用的 row_key 在当前 resources/ 真实数据中不存在（测试夹具与配表现状不一致），该条不计入定位/覆盖/精准分母，不代表 Agent 缺陷。
- 每条样例都在独立临时沙箱执行，互不影响；测试完成后沙箱已删除，不会污染真实 resources/。
- 「多余写入」統计的是全量 resources/ 目录里未被 expected_answer 认领的行改动（含 expected 未提及的表），用于发现 Agent 误改/过度级联的副作用。
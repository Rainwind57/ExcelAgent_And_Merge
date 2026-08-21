# 后端 Agent skill on/off A/B 测试报告

- 生成时间: 2026-08-11 12:39:18
- 样例来源: table_operation_test_cases.json（6 条）
- 执行方式: 进程内直接调用 AgentService（真实 codemaker serve LLM），每条样例在 resources/ 临时沙箱副本内真实执行增删改，跑前/跑后 xlsx 差异作为 ground truth
- skill=off: TableAgent(enable_skill=False)，列/行定位仅靠原始表头，不挂列别名/行规则/反模式/短形式
- skill=on : TableAgent(enable_skill=True)，正常加载 server/agent/excel/skills/ 全部 skill

## 一、总体指标对比

| 指标 | 说明 | skill=off | skill=on | 变化 |
|---|---|---|---|---|
| 定位功能 | 命中正确 table+sheet+操作类型的比例 | 0.2178 | 0.3206 | +0.1028 |
| 覆盖度 | expected 行操作中真正被产出的比例（含级联多表） | 0.1607 | 0.1984 | +0.0377 |
| 精准程度 | 被定位到的行里，字段值完全正确的比例 | 0.1611 | 0.1919 | +0.0308 |
| 严格通过率 | 整条样例 expected_answer 100%命中且无多余写入 | 0.0000 | 0.0000 | +0.0000 |
| 响应ok率 | Agent 自报告执行成功比例 | 0.1667 | 0.0000 | -0.1667 |
| 平均多余写入 | 未被 expected 认领的行改动数（越低越好） | 1.6667 | 6.1667 | -4.5000 |
| 平均耗时(ms) | 单条指令端到端耗时（含二次确认续传） | 137769.7 | 150440.0 | -12670.3 |
| P50耗时(ms) | | 187488.5 | 163841.7 | |
| P95耗时(ms) | | 227258.1 | 200621.6 | |
| 需二次确认比例 | 触发级联删除等确认流程的比例 | 0.0000 | 0.0000 | |
| 总耗时(s) | 本轮全部样例累计耗时 | 826.6 | 902.6 | |

## 二、按操作类型细分（skill=on）

| 操作类型 | n | 定位率 | 覆盖率 | 字段精准度 | (对比 off) |
|---|---|---|---|---|---|
| add | 23 | 0.4348 | 0.4348 | 0.6748 | off: locate=0.3478 cov=0.3043 acc=0.5000 |
| modify | 2 | 0.0000 | 0.0000 | 0.0000 | off: locate=0.0000 cov=0.0000 acc=0.0000 |
| delete | 0 | 0.0000 | 0.0000 | 0.0000 | off: locate=0.0000 cov=0.0000 acc=0.0000 |

## 三、每个样例详细运行情况

### 样例 106: 新增一个新 NPC 叫"灵兽饲养员老李"，模型用 1015，摆在野外战场 10001 坐标 (88, 0, 12)。玩家走过去点他弹出对话，对话内容写"要不要看看我养的灵兽？"，给两个选项：选"看看"就跳到一段新对话，新对话说"这只赤炎虎饿了，给它喂点东西吧。"；选"下次吧"就什么都不做结束对话。

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.86 | 0.86 |
| 覆盖度(行操作产出率) | 0.71 | 0.86 |
| 精准程度(字段值正确率) | 0.63 | 0.78 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 4 | 4 |
| 耗时(ms) | 202980 | 200622 |
| 错误 | 失败：locate_row - 未找到 <option_2_id>(mode=contains) | 失败 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | 🟡 部分匹配 (0.67) | 🟡 部分匹配 (0.67) |
| 2 | interaction.xlsx.Interaction | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 3 | interaction.xlsx.InteractionConv | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 4 | interaction.xlsx.InteractionConvOption | add | 🟡 部分匹配 (0.50) | 🟡 部分匹配 (0.50) |
| 5 | interaction.xlsx.InteractionConvOption | add | ❌ 未产生 (0.00) | 🟡 部分匹配 (0.50) |
| 6 | interaction.xlsx.InteractionConv | add | 🟠 定位到但字段不符 (0.00) | ✅ 完全匹配 (1.00) |
| 7 | spawn_world_entity.xlsx.SpawnWorldEntity | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |

### 样例 107: 加一件新法宝"玄火鉴"，法宝编号 28599，品质 3，图标 Icon_fabao_xuanhuo，法宝类型 5，它的法宝技能编号 700010 也一起建一下——是个攻击型火系法术叫"玄火灼烧"，对单体造成 180% 伤害。法宝的阴阳权重设 0.35，阳属性描述"激活后对敌方人物附加灼烧"，阴属性描述"激活后对敌方非人物附加灼烧"。

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | True | False |
| 定位功能(table/sheet命中率) | 0.00 | 0.00 |
| 覆盖度(行操作产出率) | 0.00 | 0.00 |
| 精准程度(字段值正确率) | 0.00 | 0.00 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 0 | 1 |
| 耗时(ms) | 3234 | 176915 |
| 错误 | - | 失败：locate_row - 缺少行定位值 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | item/item.xlsx.ItemBase | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 2 | item/item.xlsx.Fabao | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 3 | combat/spell.xlsx.common_spell | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 4 | combat/spell.xlsx.spell_data | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |

### 样例 108: 给蜀山（门派 1）加一个新神通"，神通编号 8099，描述"驭三柄飞剑斩敌"，图标 spell_default.png，解锁等级 1；神通 0 级配置：升级不花钱、升级要求人物 1 级、关联法术 700020。这个法术归到一个新技能组 500 里，组名叫"蜀山御剑组"，组里就含 700020 这一个法术。

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.20 | 0.40 |
| 覆盖度(行操作产出率) | 0.00 | 0.00 |
| 精准程度(字段值正确率) | 0.00 | 0.00 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 1 | 4 |
| 耗时(ms) | 227258 | 163842 |
| 错误 | 失败：coerce_value - 列[技能id]类型为 int，值'703.0'无法转为整数且无枚举映射，已阻止写入 | 失败：coerce_value - 列[技能id]类型为 int，值'703.5'无法转为整数且无枚举映射，已阻止写入 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | school/school_ability.xlsx.SchoolAbility | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 2 | school/school_ability.xlsx.SchoolAbilityLevel | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 3 | combat/spell_group.xlsx.SpellGroup | add | ❌ 未产生 (0.00) | 🟡 部分匹配 (0.67) |
| 4 | combat/spell.xlsx.common_spell | add | ❌ 未产生 (0.00) | 🟠 定位到但字段不符 (0.00) |
| 5 | combat/spell.xlsx.spell_data | add | 🟠 定位到但字段不符 (0.00) | ❌ 未产生 (0.00) |

### 样例 109: 加一条日常打怪任务"清剿赤炎虎"，任务号 777001，归到任务组 510（组名"测试日常"，主线类型），描述"去野外把赤炎虎清掉"，目标是打赢战斗 77777001，打赢给奖励包 10999。战斗 77777001 配成在战场 10001 里打 npc 3000 和 3001 两只怪，赢了给奖励包 10999、输了和平局都不给。奖励包 10999 也建一下，叫"清剿奖励"，每日领 50 次，必给道具 10001 共 5 个，另外 100% 给经验、100% 给金币、金币公式填 100。

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.25 | 0.00 |
| 覆盖度(行操作产出率) | 0.25 | 0.00 |
| 精准程度(字段值正确率) | 0.33 | 0.00 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 1 | 27 |
| 耗时(ms) | 187488 | 138000 |
| 错误 | 失败：match_field - 未找到列[组名] | 失败：locate_row - 未找到 77777001(mode=exact) |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | quest/quest.xlsx.QuestGroup | add | 🟡 部分匹配 (0.33) | ❌ 未产生 (0.00) |
| 2 | quest/quest.xlsx.Quest | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 3 | combat/combat.xlsx.combat_data | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 4 | reward.xlsx.Reward | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |

### 样例 110: 洞府新增一种炼丹房。先把建筑类型 11 建好，叫"炼丹房"，实体类型 ResidenceBuildingForge，一级分类"炼造"、二级分类"丹药"，洞府 1 级最多放 2 个、2 级最多放 3 个。再加一个这种建筑实例"初级炼丹炉"，建筑道具编号 22199，建筑等级 1，模型 2205002，地图图标 4004，需要洞府等级 1，占地 1×1，图纸道具用 10001。最后给炼丹房类型配一个 idle 交互动画，角色蒙太奇 DongfuSit.graph，建筑状态 0，软停止开。

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.00 | 0.67 |
| 覆盖度(行操作产出率) | 0.00 | 0.33 |
| 精准程度(字段值正确率) | 0.00 | 0.37 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 4 | 1 |
| 耗时(ms) | 124899 | 87208 |
| 错误 | 失败：locate_row - 未找到 炼丹房(mode=contains) | 失败：coerce_value - 列[建筑占地面积]类型为 int，值'1×1'无法转为整数且无枚举映射，已阻止写入 |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | residence/residence_building.xlsx.BuildingType | add | ❌ 未产生 (0.00) | 🟡 部分匹配 (0.71) |
| 2 | residence/residence_building.xlsx.ResidenceBuilding | add | ❌ 未产生 (0.00) | 🟡 部分匹配 (0.70) |
| 3 | residence/residence_building.xlsx.BuildingInteract | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |

### 样例 111: 把 28501 号法宝替身草人的阳属性描述改成"激活后 10 秒内替主人承受一次致命伤害"，它的阴阳权重同步从 0.3 调到 0.4；再把 15001 号精钢剑的可用门派列表覆盖成 1, 2, 3，基础物攻词条 WorldAttackCon 的范围改成 70 到 90。

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.00 | 0.00 |
| 覆盖度(行操作产出率) | 0.00 | 0.00 |
| 精准程度(字段值正确率) | 0.00 | 0.00 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 0 | 0 |
| 耗时(ms) | 80758 | 136053 |
| 错误 | 失败：locate_row - 未找到 28501(mode=exact) | 失败：locate_row - 未找到 28501(mode=exact) |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | item/item.xlsx.Fabao | modify | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |
| 2 | item/item.xlsx.Equipment | modify | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |

## 四、skill=on 表现最差的样例（覆盖度+精准度最低 Top10）

| cid | input | 覆盖度 | 精准度 | 定位率 | 错误 |
|---|---|---|---|---|---|
| 107 | 加一件新法宝"玄火鉴"，法宝编号 28599，品质 3，图标 Icon_faba | 0.00 | 0.00 | 0.00 | 失败：locate_row - 缺少行定位值 |
| 108 | 给蜀山（门派 1）加一个新神通"，神通编号 8099，描述"驭三柄飞剑斩敌"，图 | 0.00 | 0.00 | 0.40 | 失败：coerce_value - 列[技能id]类型为 int，值'703.5'无法转为整数且无枚举映射，已阻止写入 |
| 109 | 加一条日常打怪任务"清剿赤炎虎"，任务号 777001，归到任务组 510（组名 | 0.00 | 0.00 | 0.00 | 失败：locate_row - 未找到 77777001(mode=exact) |
| 111 | 把 28501 号法宝替身草人的阳属性描述改成"激活后 10 秒内替主人承受一次 | 0.00 | 0.00 | 0.00 | 失败：locate_row - 未找到 28501(mode=exact) |
| 110 | 洞府新增一种炼丹房。先把建筑类型 11 建好，叫"炼丹房"，实体类型 Resid | 0.33 | 0.37 | 0.67 | 失败：coerce_value - 列[建筑占地面积]类型为 int，值'1×1'无法转为整数且无枚举映射，已阻止写入 |
| 106 | 新增一个新 NPC 叫"灵兽饲养员老李"，模型用 1015，摆在野外战场 100 | 0.86 | 0.78 | 0.86 | 失败 |

## 五、夹具错误清单（D7，共 0 条，已排除出统计）

| cid | input | 错误类型 | 详情 |
|---|---|---|---|

## 六、结论总结

- skill 挂载后定位功能变化: +0.1028（提升）
- skill 挂载后覆盖度变化: +0.0377（提升）
- skill 挂载后精准程度变化: +0.0308（提升）
- skill 挂载后严格通过率变化: +0.0000
- skill 挂载后耗时变化: -12670.3ms（变慢，正数=on比off快，因为定义为 off-on）
- 综合判定: skill 增强 有效

注意事项：
- ⚪ 夹具缺失(跳过) 表示 expected_answer 引用的 row_key 在当前 resources/ 真实数据中不存在（测试夹具与配表现状不一致），该条不计入定位/覆盖/精准分母，不代表 Agent 缺陷。
- 每条样例都在独立临时沙箱执行，互不影响；测试完成后沙箱已删除，不会污染真实 resources/。
- 「多余写入」統计的是全量 resources/ 目录里未被 expected_answer 认领的行改动（含 expected 未提及的表），用于发现 Agent 误改/过度级联的副作用。
# 后端 Agent skill on/off A/B 测试报告

- 生成时间: 2026-08-12 18:21:08
- 样例来源: table_operation_test_cases.json（1 条）
- 执行方式: 进程内直接调用 AgentService（真实 codemaker serve LLM），每条样例在 resources/ 临时沙箱副本内真实执行增删改，跑前/跑后 xlsx 差异作为 ground truth
- skill=off: TableAgent(enable_skill=False)，列/行定位仅靠原始表头，不挂列别名/行规则/反模式/短形式
- skill=on : TableAgent(enable_skill=True)，正常加载 server/agent/excel/skills/ 全部 skill

## 一、总体指标对比

| 指标 | 说明 | skill=off | skill=on | 变化 |
|---|---|---|---|---|
| 定位功能 | 命中正确 table+sheet+操作类型的比例 | 0.8333 | 0.8333 | +0.0000 |
| 覆盖度 | expected 行操作中真正被产出的比例（含级联多表） | 0.8333 | 0.8333 | +0.0000 |
| 精准程度 | 被定位到的行里，字段值完全正确的比例 | 0.7333 | 0.7333 | +0.0000 |
| 严格通过率 | 整条样例 expected_answer 100%命中且无多余写入 | 0.0000 | 0.0000 | +0.0000 |
| 响应ok率 | Agent 自报告执行成功比例 | 0.0000 | 0.0000 | +0.0000 |
| 平均多余写入 | 未被 expected 认领的行改动数（越低越好） | 2.0000 | 2.0000 | +0.0000 |
| 平均耗时(ms) | 单条指令端到端耗时（含二次确认续传） | 307511.0 | 248914.2 | +58596.8 |
| P50耗时(ms) | | 307511.0 | 248914.2 | |
| P95耗时(ms) | | 307511.0 | 248914.2 | |
| 需二次确认比例 | 触发级联删除等确认流程的比例 | 0.0000 | 0.0000 | |
| 总耗时(s) | 本轮全部样例累计耗时 | 307.5 | 248.9 | |

## 二、按操作类型细分（skill=on）

| 操作类型 | n | 定位率 | 覆盖率 | 字段精准度 | (对比 off) |
|---|---|---|---|---|---|
| add | 6 | 0.8333 | 0.8333 | 0.7333 | off: locate=0.8333 cov=0.8333 acc=0.7333 |
| modify | 0 | 0.0000 | 0.0000 | 0.0000 | off: locate=0.0000 cov=0.0000 acc=0.0000 |
| delete | 0 | 0.0000 | 0.0000 | 0.0000 | off: locate=0.0000 cov=0.0000 acc=0.0000 |

## 三、每个样例详细运行情况

### 样例 1: 新增一个NPC叫铁匠老张，model_id为1015，放在space_id 10008的场景坐标(60,0,30)，玩家点击后弹出对话，对话内容为'欢迎来到铁匠铺，我可以帮你锻造装备。'，选项为'好的，我要锻造'和'离开'

| 指标 | skill=off | skill=on |
|---|---|---|
| 响应ok | False | False |
| 定位功能(table/sheet命中率) | 0.83 | 0.83 |
| 覆盖度(行操作产出率) | 0.83 | 0.83 |
| 精准程度(字段值正确率) | 0.73 | 0.73 |
| 严格通过 | False | False |
| 多余写入(误写行数) | 2 | 2 |
| 耗时(ms) | 307511 | 248914 |
| 错误 | 失败：match_field - 未找到列[最大生成数量] | 失败：match_field - 未找到列[最大生成数量] |

expected_answer 逐条判定（skill=off → skill=on）：

| # | table.sheet | op | off | on |
|---|---|---|---|---|
| 1 | entity_prefab.xlsx.Base | add | 🟡 部分匹配 (0.67) | 🟡 部分匹配 (0.67) |
| 2 | interaction.xlsx.Interaction | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 3 | interaction.xlsx.InteractionConv | add | 🟠 定位到但字段不符 (0.00) | 🟠 定位到但字段不符 (0.00) |
| 4 | interaction.xlsx.InteractionConvOption | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 5 | interaction.xlsx.InteractionConvOption | add | ✅ 完全匹配 (1.00) | ✅ 完全匹配 (1.00) |
| 6 | spawn_world_entity.xlsx.SpawnWorldEntity | add | ❌ 未产生 (0.00) | ❌ 未产生 (0.00) |

## 四、skill=on 表现最差的样例（覆盖度+精准度最低 Top10）

| cid | input | 覆盖度 | 精准度 | 定位率 | 错误 |
|---|---|---|---|---|---|
| 1 | 新增一个NPC叫铁匠老张，model_id为1015，放在space_id 10 | 0.83 | 0.73 | 0.83 | 失败：match_field - 未找到列[最大生成数量] |

## 五、夹具错误清单（D7，共 0 条，已排除出统计）

| cid | input | 错误类型 | 详情 |
|---|---|---|---|

## 六、结论总结

- skill 挂载后定位功能变化: +0.0000（持平）
- skill 挂载后覆盖度变化: +0.0000（持平）
- skill 挂载后精准程度变化: +0.0000（持平）
- skill 挂载后严格通过率变化: +0.0000
- skill 挂载后耗时变化: +58596.8ms（变快，正数=on比off快，因为定义为 off-on）
- 综合判定: skill 增强 效果不明显

注意事项：
- ⚪ 夹具缺失(跳过) 表示 expected_answer 引用的 row_key 在当前 resources/ 真实数据中不存在（测试夹具与配表现状不一致），该条不计入定位/覆盖/精准分母，不代表 Agent 缺陷。
- 每条样例都在独立临时沙箱执行，互不影响；测试完成后沙箱已删除，不会污染真实 resources/。
- 「多余写入」統计的是全量 resources/ 目录里未被 expected_answer 认领的行改动（含 expected 未提及的表），用于发现 Agent 误改/过度级联的副作用。
# 任务链测试指南

针对 `task_chain.json` 中 **10 条复合任务链**（跨表多步 + produces 占位符引用链）的测试运行说明。每条链是一条自然语言指令，理应在多张表/sheet 上产生一系列 add/modify 操作，且步骤间存在**新 ID 引用闭环**（前一步产出的 `new_xxx_id` 被后续步骤消费）。

---

## 一、测试目标

验证 AI 配表助手的**复合指令处理能力**：

- **链完整性**：expected 每步是否真正产出对应行操作
- **占位符引用一致性**：consumer 步引用字段值 == producer 步实际产出的新 ID（任务链核心质量指标——引用闭环是否成立）
- **定位 / 覆盖 / 精准 / 多余写入**（复用 `table_case_eval.py` 判定口径）
- **失败模式归类** → 驱动内环优化（解析 / 路由 / 定位 / 字段 / 引用 / 副作用）

---

## 二、前置条件

1. **后端服务运行中**：`tools\start.bat` 已启动 CodeMaker Serve（8666）+ FastAPI（8000）
2. **已登录 netease-codemaker**：`codemaker providers login -p netease-codemaker`
3. **`.env` 已配置**：含 `CODEMAKER_SERVER_URL` 等
4. **索引已构建**：`resources/` 下 74 张表索引就绪（deploy 第6步）
5. **Python 环境**：`uv sync` 已完成（Python ≥3.10）

---

## 三、自动化测试（`task_chain_eval.py`）

在 `server/` 目录下执行：

```bash
# 冒烟：前 3 条链（快速验证链路通）
uv run python -m tests.task_chain_eval --quick 3

# 全量：跑全部 10 条链（skill=on）
uv run python -m tests.task_chain_eval

# A/B 对照：skill on/off 对比（评估 skill 增益）
uv run python -m tests.task_chain_eval --skill both

# 指定用例文件
uv run python -m tests.task_chain_eval --cases-file ../task_chain.json
```

### 评估机制

- 在 `resources/` 沙箱副本上**真实执行**（不污染原表）
- 以「跑前 / 跑后 xlsx 行级差异」为 ground truth
- 与 `expected_answer` 逐条比对：step↔op 绑定 + 引用闭环校验
- 实时进度打印：终端按 6 步流程输出 `✅/❌` + thinking phase

### 输出报告

`server/tests/reports/task_chain_eval_latest.{md,json}`

---

## 四、任务链用例清单（10 条）

| ID | 指令摘要 | 涉及表 / Sheet | produces 引用链 | 预期步数 | 难度 |
|----|---------|---------------|----------------|---------|------|
| C1 | 新增道具商人「云游商人」，点击对话→选项→奖励 | `entity_prefab` / `interaction`(4 sheet) / `spawn_world_entity` | prefab_id→interaction_id→conv_id→option_ids→reward_conv_id | 8 | 高 |
| C2 | 新增擂台 NPC「剑修挑战者」，点击进入战斗 | `entity_prefab` / `interaction` / `spawn_world_entity` | prefab_id→interaction_id→spawn_id | 3 | 中 |
| C3 | 新增传送 NPC「回城传送阵」，点击传送到指定坐标 | `entity_prefab` / `interaction` / `spawn_world_entity` | prefab_id→interaction_id→spawn_id | 3 | 中 |
| C4 | 新增奖励 NPC「签到使者」+ 改 reward 名称 | `entity_prefab` / `interaction` / `spawn_world_entity` / `reward`(modify) | prefab_id→interaction_id→spawn_id | 4 | 中 |
| C5 | 新增武器「七星剑」（装备基础+扩展属性） | `item\item`(ItemBase+Equipment) | new_item_id | 2 | 中 |
| C6 | 新增药品「聚气丹」（道具基础+药品效果） | `item\item`(ItemBase+Potion) | new_item_id | 2 | 中 |
| C7 | 新增法宝「玄阳镜」（道具基础+法宝属性） | `item\item`(ItemBase+Fabao) | new_item_id | 2 | 中 |
| C8 | 新增灵兽「寒冰凤」+ 进化路径 | `pet\pet` / `pet\pet_evolve` | new_pet_id | 2 | 中 |
| C9 | 新增邮件模板 + 全服邮件配置 | `mail`(MailTemplate+GlobalMail) | 无（template_id 显式） | 2 | 低 |
| C10 | 新增支线任务「护送商队」+ 刷怪点 | `quest\quest` / `quest\spawn_quest_entity` | new_spawn_id | 2 | 中 |

### 用例详情

#### C1 云游商人（最复杂——嵌套对话引用链）

**输入**：
> 新增一个道具商人叫'云游商人'，model_id 1021，放在 space_id 10001 坐标(30,0,40)，玩家点击后弹出对话'欢迎光临，要不要看看我的货物？'，选项'好的，看看'和'下次再来'，点击'好的看看'后获得 reward_id 10066 的奖励包

**预期 8 步**：
1. `entity_prefab.xlsx/Base` add → 产出 `new_prefab_id`（含 interaction_id 占位）
2. `interaction.xlsx/Interaction` add → 产出 `new_interaction_id`（effect.key=3006）
3. `interaction.xlsx/InteractionConv` add → 产出 `new_conv_id`（主对话）
4. `interaction.xlsx/InteractionConvOption` add → 产出 `option_1_id`（"好的，看看"）
5. `interaction.xlsx/InteractionConv` add → 产出 `new_reward_conv_id`（奖励对话）
6. `interaction.xlsx/InteractionConvOption` add → 产出 `option_3_id`（"多谢"，带 reward_id=10066）
7. `interaction.xlsx/InteractionConvOption` add → 产出 `option_2_id`（"下次再来"）
8. `spawn_world_entity.xlsx/SpawnWorldEntity` add → 产出 `new_spawn_id`

**引用闭环**：prefab_id→interaction_id→conv_id→option_1→reward_conv_id→option_3，共 6 个 produces 占位符。

#### C2 剑修挑战者

**输入**：
> 新增一个擂台NPC叫'剑修挑战者'，model_id 1012，放在 space_id 10008 坐标(45,0,55)，玩家点击后进入战斗 combat_id 102

**预期 3 步**：entity_prefab add → interaction add（effect.key=3001, combat_id=102）→ spawn_world_entity add

#### C3 回城传送阵

**输入**：
> 新增一个传送NPC叫'回城传送阵'，model_id 1016，放在 space_id 10008 坐标(10,0,10)，玩家点击后传送到 space_id 10001 的坐标(100,0,200)，朝向0度

**预期 3 步**：entity_prefab add → interaction add（effect.key=3005, to_space/to_pos/to_dir）→ spawn_world_entity add

#### C4 签到使者（add + modify 混合）

**输入**：
> 新增一个奖励NPC叫'签到使者'，model_id 1018，放在 space_id 10008 坐标(80,0,80)，玩家点击后获得 reward_id 10030 的奖励，同时修改 reward_id 10030 名称为'每日签到奖励'

**预期 4 步**：entity_prefab add → interaction add（effect.key=3002）→ spawn_world_entity add → `reward.xlsx/Reward` **modify**（id=10030, name=每日签到奖励）

#### C5 七星剑（武器）

**输入**：
> 新增一把武器叫'七星剑'，品质2，图标'Icon_weapon_qixingjian'，装备部位1，装备类型1，可用门派1、2、3，基础物攻 WorldAttackCon 范围80-100，随机词条池1003，分解获得 item_id 10020 数量1，基础评分150

**预期 2 步**：`item\item.xlsx/ItemBase` add（item_type=5）→ `item\item.xlsx/Equipment` add（引用 new_item_id）

#### C6 聚气丹（药品）

**输入**：
> 新增一个药品叫'聚气丹'，品质1，图标'Icon_potion_juqid'，最大堆叠99，可丢弃，使用效果id为10002，回复真元MPMaxCon 500点，效果描述'回复500真元'

**预期 2 步**：`item\item.xlsx/ItemBase` add（item_type=3, max_stack=99）→ `item\item.xlsx/Potion` add（引用 new_item_id, usage_effect.args.MPMaxCon=500）

#### C7 玄阳镜（法宝）

**输入**：
> 新增一个法宝叫'玄阳镜'，品质3，图标'Icon_fabao_xuanyang'，法宝类型3，法宝技能编号4002，附加属性池200002，阴阳权重0.6，阳属性描述'法宝状态下，对敌方火系目标造成20%额外伤害'，阴属性描述'法宝状态下，受到火系攻击减免20%伤害'

**预期 2 步**：`item\item.xlsx/ItemBase` add（item_type=6）→ `item\item.xlsx/Fabao` add（引用 new_item_id, yinyang_rate=0.6）

#### C8 寒冰凤（灵兽+进化）

**输入**：
> 新增一只灵兽叫'寒冰凤'，model_id 1065，品质3，元素类型ice，灵兽蛋道具 item_id 28005，出战所需人物等级30，默认加点2，体力资质5000，物攻资质1800，法攻资质1600，物防资质1100，法防资质1300，并配置进化路径进化为 pet_id 2065 的'究极寒冰凤'，消耗道具 item_id 10004 数量2

**预期 2 步**：`pet\pet.xlsx/Pet` add → `pet\pet_evolve.xlsx/PetEvolveData` add（引用 new_pet_id）

#### C9 周年庆邮件

**输入**：
> 新增一个邮件模板叫'周年庆奖励邮件'，template_id 30099，标题'周年庆奖励发放'，内容'亲爱的玩家，周年庆福利已到，请查收附赠的丰厚奖励'，同时配全服邮件 global_id 6 使用该模板，发送人'系统'，附带奖励 reward_id 10088

**预期 2 步**：`mail.xlsx/MailTemplate` add（template_id=30099 显式）→ `mail.xlsx/GlobalMail` add（引用 template_id）

#### C10 护送商队（任务+刷怪）

**输入**：
> 新增一个支线任务叫'护送商队'，任务ID 250012，任务组 group_id 250，描述'护送商队安全抵达目的地'，目标类型 Escort，目标数据'escort_id:[30001],need_arrive:1,count:1'，奖励 reward_id 10090，同时要求击杀 npc_id 为5023的怪物，在 space_id 10008 坐标(50,0,60)刷新该怪物

**预期 2 步**：`quest\quest.xlsx/Quest` add（quest_id=250012 显式）→ `quest\spawn_quest_entity.xlsx/SpawnQuestEntity` add

---

## 五、手动测试（配表模式 / 前端）

### 方式 A：CodeMaker 配表模式

1. 确保 `tools\start.bat` 已启动后端
2. 在 CodeMaker 对话框输入：`进入配表模式`
3. 确认后进入模式，直接粘贴上述任一**输入**指令
4. 系统走 6 步流程：解析→分区→计划→校验→应用→汇总
5. 观察是否正确识别跨表引用、生成完整操作链

### 方式 B：前端 Web UI

1. 浏览器打开 `http://127.0.0.1:8000`
2. 在指令输入框粘贴**输入**文本
3. 预览 diff → 确认后应用
4. 对照本文档「预期步数」核对实际操作数

### 方式 C：API 直接调用

```bash
# 预览（dry-run，不写盘）
curl -X POST http://127.0.0.1:8000/api/agent/preview \
  -H "Content-Type: application/json" \
  -d '{"text":"新增一个擂台NPC叫剑修挑战者，model_id 1012，放在space_id 10008坐标(45,0,55)，玩家点击后进入战斗combat_id 102"}'

# 真实执行（写盘）
curl -X POST http://127.0.0.1:8000/api/agent/chat \
  -H "Content-Type: application/json" \
  -d '{"text":"<同上指令>"}'
```

---

## 六、评估指标说明

| 指标 | 含义 | 合格线 |
|------|------|--------|
| `chain_complete` | 所有 effective 步 status==matched | True |
| `ref_consistency_rate` | 占位符引用闭环成立比例 | ≥0.9 |
| `producers_resolved / total` | 产出新 ID 成功提取的 producer 数 | =total |
| `locate_rate` | 表/sheet 定位命中率 | ≥0.9 |
| `coverage` | 行定位覆盖率（扣多余写入惩罚） | ≥0.85 |
| `field_accuracy` | 字段精准率（扣多余写入惩罚） | ≥0.9 |
| `strict_pass` | 全 matched 且无 extra ops | True |
| `truth_ok` | 行对+无离表多余写入 | True |

### 失败模式归类

- **解析失败**：指令未识别为复合 CRUD
- **路由错误**：表/sheet 定位错（`table_sheet_hit=False`）
- **定位失败**：表对但行未定位（`row_located=False`）
- **字段偏差**：行对但字段值不匹配（`field_score<1`）
- **引用断裂**：consumer 字段值 ≠ producer 产出 ID（`ref_consistency_rate<1`）
- **副作用**：产生 expected 之外的 extra ops（`extra_ops_off_table>0`）

---

## 七、报告查看

报告路径：`server/tests/reports/task_chain_eval_latest.md`

包含：
- 每条链的输入、耗时、chain_complete、ref 一致性、producers 解析数
- 逐步 step↔op 绑定结果（matched / partial / located_only / missing）
- 引用闭环校验明细（每条占位符的 expected vs actual）
- 失败模式归类统计
- skill on/off A/B 对照（如启用）

---

## 八、常见问题

**Q: 跑测试报 `codemaker serve not alive`？**
A: 检查 8666 端口：`netstat -ano | findstr :8666`。未启动则 `tools\start.bat`。

**Q: 某条链 `precondition_missing`？**
A: expected 里的 modify/delete 步依赖原始数据存在。检查 `resources/` 是否完整（deploy 第6步索引构建成功）。

**Q: `ref_consistency_rate` 低？**
A: 引用断裂——producer 产出了新 ID 但 consumer 字段没正确引用。通常是 LLM 未把前一步产出的 ID 回填到后续步骤。看报告里 RefCheck 的 `expected_value` vs `actual_value`。

**Q: 想新增任务链？**
A: 编辑 `task_chain.json`，按现有格式追加 `{input, expected_answer}`。`produces` 字段标注该步产出的占位符标签，后续步骤用 `<标签名>` 引用。

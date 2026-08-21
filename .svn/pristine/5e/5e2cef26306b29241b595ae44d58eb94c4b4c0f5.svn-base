# AI 配表助手 — 表格操作综合测试说明

测试脚本：`server/tests/run_table_tests.py`

## 一、测试目标

对 AI 配表助手系统的**表格操作能力**做系统化评测。通过一批覆盖面广的自然语言指令，每条指令配明确的「理应结果」与可程序化判定的断言规则，跑完后用「实际结果 vs 理应结果」的对比得出系统准确率。

## 二、被测系统

- **系统**：AI 配表助手（FastAPI 后端 + codemaker serve LLM 底座）
- **入口**：
  - `POST /api/agent/preview` — dry-run 预览（不写盘）
  - `POST /api/agent/chat` — 真实执行（写盘）
  - `POST /api/agent/batch` — 批量执行
  - `POST /api/workflow/snapshot` + `POST /api/workflow/snapshot/{id}/restore` — 快照与恢复
- **能力链路**：自然语言 → LLM 意图分类(qa/crud) → NLIntent 解析 → 表定位 → sheet 定位 → 列匹配 → 行定位 → openpyxl 读写 → DiffPreview

## 三、测试方式（混合模式）

| 用例类型 | 执行方式 | 原因 |
|---------|---------|------|
| 查询 / 修改 / 新增 / 删除 / 复合 / 边界 | `preview`（dry-run） | 不写盘，可无限重复，覆盖面最大 |
| 写盘闭环（H/J 类） | `chat`（真实写盘） + 共享快照恢复 | 验证「写→读回」持久化闭环 |
| 批量 | `batch` | 验证批量执行与错误聚合 |
| 快照闭环 | `chat` + `restore` + 读回 | 验证快照恢复后数据回滚 |

**可重复性保证**：所有写盘用例在共享快照基线上运行，每条用例前先 `restore` 回基线，跑完统一恢复，`resources/` 回到测试前状态。

## 四、真实数据样本（断言依据）

### `pet.xlsx` / Pet sheet（43 行，data_start_row=5）

| 灵兽id | 名称 | 物攻资质 | 体力资质 | 速度资质 | 成长率 |
|--------|------|---------|---------|---------|-------|
| 1052 | 火焰犬 | 1500 | 4500 | 1000 | 1.15 |
| 2052 | 烈焰犬 | 1550 | 4700 | 1200 | 1.2 |
| 3052 | 炽焰獒 | 1600 | 4900 | 1400 | 1.25 |

### `city/building.xlsx` / BuildingType sheet（20 行）

建筑名称：帮派基地 / 坊市 / 传送阵 / 城主府 / 青龙图腾 / 白虎图腾 / 朱雀图腾 / 玄武图腾 / 东木门 / 南火门 / 西金门 / 北水门 / 守方大营门 / 仙圃 / 镇妖阵 ...

## 五、用例清单（指令 + 理应结果 + 判定规则）

### A. 单表查询（preview / get）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| A01 | 查询灵兽火焰犬的物攻资质 | 返回 1500 | ok=T ∧ intent=get ∧ message 含 "1500" |
| A02 | 查询灵兽火焰犬的成长率 | 返回 1.15 | ok=T ∧ message 含 "1.15" |
| A03 | 查询灵兽烈焰犬的物攻资质 | 返回 1550 | ok=T ∧ message 含 "1550" |
| A04 | 查询灵兽炽焰獒的成长率 | 返回 1.25 | ok=T ∧ message 含 "1.25" |
| A05 | 查询灵兽火焰犬的体力资质 | 返回 4500 | ok=T ∧ message 含 "4500" |
| A06 | 查询灵兽火焰犬的所有属性 | 返回该行 | ok=T ∧ intent=get ∧ message 含 "火焰犬" |
| A07 | 查询建筑帮派基地的建筑名称 | 查到 | ok=T ∧ message 含 "帮派基地" |
| A08 | 查询建筑传送阵的描述 | 查到 | ok=T ∧ message 含 "传送阵" |

### B. 单表修改（preview / set，验证解析+定位+diff 生成）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| B01 | 将灵兽火焰犬的物攻资质改为 1500 | diff new_value=1500 | ok=T ∧ intent=set ∧ diff.changes[0].new_value="1500" |
| B02 | 将灵兽火焰犬的物攻资质改为 2000 | diff new_value=2000 | ok=T ∧ intent=set ∧ diff.changes[0].new_value="2000" |
| B03 | 把灵兽炽焰獒的成长率改成 1.5 | diff new_value=1.5 | ok=T ∧ intent=set ∧ diff.changes[0].new_value="1.5" |
| B04 | 将灵兽烈焰犬的速度资质改为 1800 | diff new_value=1800 | ok=T ∧ intent=set ∧ diff.changes[0].new_value="1800" |

### C. 单表新增（preview / insert|add）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| C01 | 新增一个灵兽，名称测试兽A，物攻资质1600 | 新增成功 | ok=T ∧ intent∈{insert,add} |
| C02 | 新增一个灵兽，名称朱雀，品质3，成长率1.5 | 复合新增 | ok=T ∧ intent∈{insert,add} |
| C03 | 增加建筑名称为测试塔，赋值它的建筑类型是99 | 复合+代词 | ok=T ∧ intent∈{insert,add} |

### D. 单表删除（preview / delete）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| D01 | 删除灵兽名称为火焰犬的行 | 识别删除 | ok=T ∧ intent=delete |
| D02 | 删除灵兽烈焰犬 | 识别删除 | ok=T ∧ intent=delete |

### E. 复合语句 / 代词消解（preview）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| E01 | 增加建筑名称为瞭望塔，赋值它的建筑类型是99999 | INSERT+代词消解 | ok=T ∧ intent∈{insert,add} |
| E02 | 新增灵兽名称朱雀，然后设置它的成长率是2.0 | 复合+代词 | ok=T ∧ intent∈{insert,add} |
| E03 | 把灵兽火焰犬的物攻资质改为2000，法攻资质改为1500 | 多字段修改 | ok=T ∧ intent=set |

### F. 多表 / 跨表查询（preview / qa）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| F01 | 灵兽相关的表有哪些 | 列出 pet 等 | ok=T ∧ reply_type=qa ∧ message 含 "pet"/"灵兽"/"宠物" |
| F02 | 邮件相关的表格有哪些 | 列出 mail | ok=T ∧ reply_type=qa ∧ message 含 "mail"/"邮件" |
| F03 | hero表有哪些列 | 列出 hero 列 | ok=T ∧ reply_type=qa ∧ message 含 "名称"/"hero"/"人物id" |
| F04 | 有哪些配置表 | 列举表名 | ok=T ∧ reply_type=qa |
| F05 | 建筑相关的表有哪些 | 列出 building | ok=T ∧ reply_type=qa ∧ message 含 "building"/"建筑" |

### G. 边界 / 异常处理（preview）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| G01 | 查询灵兽不存在的兽的物攻资质 | 未找到行 | ok=F ∨ message 含 未找到/不存在/找不到/无 |
| G02 | 查询灵兽火焰犬的不存在的列 | 列未找到 | ok=F ∨ message 含 未找到/不存在/找不到/无法/匹配 |
| G03 | 将灵兽火焰犬的物攻资质改为 | 缺值 | ok=F |
| G04 | 你好 | 闲聊走 qa | ok=T ∧ reply_type=qa |
| G05 | 删除灵兽完全不存在兽XYZ的行 | 未找到 | ok=F ∨ message 含 未找到/不存在/找不到 |

### H. 真实写盘闭环（chat + 快照恢复）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| H01 | 将灵兽火焰犬的物攻资质改为 2000 | 写盘+读回=2000 | ok=T ∧ intent=set ∧ diff new_value=2000 ∧ 写后 preview 读回=2000 |
| H02 | 将灵兽火焰犬的成长率改为 2.0 | 写盘 | ok=T ∧ intent=set ∧ diff new_value=2.0 |
| H03 | 新增一个灵兽，名称测试兽闭环，物攻资质1800 | 新增 | ok=T ∧ intent∈{insert,add} |

### I. 批量操作（batch）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| I01 | [3 条查询] | 全成功 | ok=T ∧ success_count=3 ∧ fail_count=0 |
| I02 | [1 成功+1 失败] | 部分失败 | ok=T ∧ success≥1 ∧ fail≥1 |

### J. 快照恢复闭环（chat + restore + 读回）

| ID | 指令 | 理应结果 | 判定规则 |
|----|------|---------|---------|
| J01 | 将灵兽火焰犬的物攻资质改为 2000 → 恢复快照 → 查询回=1500 | ok=T ∧ intent=set ∧ diff=2000 ∧ restore 后 preview 读回=1500 |

## 六、判定规则汇总

断言函数（见脚本 `assert_*` 系列）：

- `assert_ok` — 响应 ok=True
- `assert_not_ok` — 响应 ok=False（边界用例）
- `assert_intent` / `assert_intent_any` — intent 字段匹配
- `assert_reply_type` — reply_type=qa/crud
- `assert_message_contains` / `assert_message_any` — message 文本含子串
- `assert_diff_change_value` — diff_preview.changes[0].new_value 等于预期（写操作校验）

## 七、运行

```bash
# 前置：后端 + codemaker serve 已启动

# 全量跑（约 5-10 分钟）
uv run python -m server.tests.run_table_tests --json-out server/tests/report.json

# 只跑某类别
uv run python -m server.tests.run_table_tests --category 单表查询

# 只跑某 id（调试用）
uv run python -m server.tests.run_table_tests --id A01
```

## 八、报告输出

- 控制台：逐条 PASS/FAIL + 按类别准确率 + 系统总准确率
- JSON（`--json-out`）：每条用例的 id/类别/指令/预期/实际响应摘要/是否通过/耗时，便于程序化分析

## 九、准确率口径

- **系统准确率** = 通过用例数 / 总用例数 × 100%
- **类别准确率** = 该类通过数 / 该类总数 × 100%
- 判定标准：实际响应满足该用例「判定规则」全部条件即记为通过；任一不满足记为失败。失败用例会附上实际响应摘要供人工核查（区分是「系统能力不足」还是「断言过严」）。

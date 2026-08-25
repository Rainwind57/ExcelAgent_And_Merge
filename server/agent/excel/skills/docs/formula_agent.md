# 公式处理 Agent Skill（P0-1）

> 让 AI 能可靠改含公式配表：从"机械坐标位移"到"语义理解 + 工具执行"。
> 对应 `P0-roadmap.md` §1.2（insert_row）+ §1.3（Agent 化公式处理）。

## 0. 背景与问题

AI 用 openpyxl 改含公式表，有两个独立痛点：

1. **缓存值丢失**（已解决，L0）：openpyxl save 清空公式缓存值 → 编表工具链读出 0/空。
   解决：`formula_cache_validator.py`（libreoffice 重算）+ CLI 所有写表方法接入 `_save_with_cache_check`。
   **配表操作模式经原子 API 间接接入**：配表模式调的 `cell/update`、`cells/batch-update`、`row/delete`、`row/insert`、`column/add`、`column/delete`、`add-form/commit` 经 `agent_service.py` 转发到上述 CLI 写表方法，L0 缓存保护同样生效——配表模式改公式输入源后缓存值自动重算，无需用户手动处理。
2. **引用位移 / 语义缺口**（本 skill）：
   - **机械位移**：删/插行列后，openpyxl 只物理移动单元格，不重写公式文本内引用 → 汇总范围错位。
   - **语义缺口**：`append_row` 末尾加行，机械位移不介入 → "统计全部数据行"的汇总 `=SUM(F3:F14)` 不会自动含新行。这需要**理解公式意图**，机械规则做不到。

## 1. 三层架构

| 层 | 职责 | 组件 | 状态 |
|---|---|---|---|
| **L1 机械层** | Excel 标准删/插行列位移，无需理解 | `formula_ref_shifter` + CLI `delete_row`/`insert_row`/`insert_column`/`delete_column` 内置 | ✅ |
| **L2 语义原语层** | 公式语义解读 / 影响预演 / 按决策重写 | `formula_semantics.py`：`interpret_formula`/`scan_sheet_formulas`/`preview_formula_impact`/`rewrite_formula` | ✅ 本 skill |
| **L3 agent 层** | AI 居中：读 L2 语义 → 推理目标公式 → 调 L2 执行 | AI（prompt + 工具调用） | ✅ 流程见 §4 |

**核心原则**：L2 是**纯原语，不做业务判断，判断权在 AI**。工具只提供"理解"和"执行"原语，
"改什么"由 AI 决定。不硬编码任何业务规则（整列引用规约 / 启发式汇总识别均否决）。

**与 L1 的关系**：L1 机械位移**保留**。标准删/插行列（Excel 兼容）走 L1，快且确定，无需 AI 介入。
`append_row` 这种"机械位移无意义、需语义理解"的场景走 L2→L3。

## 2. L2 四个原语

均挂在 `CodeMakerCLI` / `StubCodeMakerCLI` 上，AI 可直接调用。前三个纯读，`rewrite_formula` 写+缓存校验。

### 2.1 `interpret_formula(path, sheet, cell) → FormulaSemantics`
解析单格公式语义：函数类型、引用范围、是否跨表、是否覆盖数据区、是否末行（汇总特征）。
复用 `formula_ref_shifter._parse_ref` 的 token 解析。

关键字段：
- `funcs` 顶层函数名；`is_aggregate` 聚合(SUM/AVERAGE/MAX...)；`is_lookup` 查表(VLOOKUP/INDEX...)
- `is_cross_sheet` 跨表引用；`has_dynamic_func` 含 OFFSET/INDIRECT（机械位移会跳过）
- `covers_data_area` 引用覆盖数据区；`is_last_row` 公式在末数据行（典型汇总位置）
- `notes` 人类可读语义备注

### 2.2 `scan_sheet_formulas(path, sheet) → list[FormulaSemantics]`
扫描全 sheet 所有公式单元格语义。AI 拿全貌：哪些是汇总、哪些是查表、哪些是行内计算。

### 2.3 `preview_formula_impact(path, sheet, op) → ImpactReport`
**dry-run 模拟增删改，不落盘**。返回每格机械位移结果 + 语义缺口标注。

`op` 取值：
```python
{"kind": "append_rows", "count": 3}   # 末尾追加，机械不位移，标语义缺口
{"kind": "insert_row", "row": 5}       # 中间插行，机械位移（范围扩展）
{"kind": "delete_row", "row": 5}       # 删行，机械位移（范围收缩/#REF!）
{"kind": "delete_col", "col": 3}       # 删列，col 可为列号或字母
```

返回 `ImpactReport`：
- `impacts` 每格 `CellImpact`：`formula_before`/`formula_after`（机械位移后）/`has_ref_error`/`semantic_gap`
- `semantic_gaps` 有缺口的单元格摘要；`needs_agent_decision` 是否需 AI 决策重写

**语义缺口是事实标注，非业务判断**：只说"聚合范围 F3:F14 末行 14，新增数据行 16-17 未纳入范围"，
不说"应改为 SUM(F3:F17)"。仅对**跨行聚合范围**（`rhi > rlo`）标注，行内单行公式（=SUM(B3:E3)）不标。

### 2.4 `rewrite_formula(path, sheet, cell, new_formula) → CLICallResult`
按 AI 决策的目标公式写入 + 缓存校验。复用 `_save_with_cache_check`（libreoffice 重算）。
**不做语义校验**——AI 决定写什么就写什么。

## 3. §1.2 `insert_row`（中间插入行）

CLI 新增 `insert_row(path, sheet, row, values=None)`：在指定行上方插入新行。
与 `delete_row` 对称：`ws.insert_rows(row)` → `shift_workbook_formulas(row, +1)` → 缓存校验。
表头/类型行不动；`values` 按列索引写新行数据区。用于有序编号段中间插入配置行。

接入清单（6→7 个写表方法）：

| 方法 | 结构变更 | 位移器 | 说明 |
|---|---|---|---|
| `delete_row` | 删行 | ✅ row,-1 | |
| `insert_row` | 插行 | ✅ row,+1 | **本 skill 新增** |
| `insert_column`(中间) | 插列 | ✅ col,+1 | after=None 末尾追加不接入 |
| `delete_column` | 删列 | ✅ col,-1 | |
| `write_cell`/`append_row`/`rename_column` | 否 | ❌ | 改值/末尾追加/改表头，不动引用 |

## 4. L3 agent 流程

### 场景 A：append 加数据行，末行汇总需含新行（需 rewrite）
```
1. scan_sheet_formulas → AI 找到 F15 是"统计全部数据行"汇总
   （=SUM(F3:F14)，is_aggregate + covers_data_area + is_last_row）
2. preview_formula_impact(append_rows,3) → 机械结果：汇总不变；
   语义标注：新行 16-18 未纳入 F3:F14
3. AI 推理：汇总语义=全部数据行，新 3 行是数据行 → 目标 =SUM(F3:F18)
4. append_row 写 3 行数据
5. rewrite_formula(F15,"=SUM(F3:F18)") 执行 AI 决策
6. 缓存校验重算
```
> 注意循环引用：若汇总与被汇总同列（F15 求 F 列），rewrite 的范围不能含汇总格自身。
> 汇总放独立列（如 G 列求 F 列）可避免。AI 据 `interpret_formula` 的 refs 判断。

### 场景 B：中间插行（机械位移已正确，无需 rewrite）
```
1. preview_formula_impact(insert_row,5) → 机械结果：F15→F16，范围 F3:F14→F3:F15；
   needs_agent_decision=False（无语义缺口）
2. AI 判定：机械位移正确 → 直接 insert_row，无需 rewrite
3. insert_row(5, values) → 位移器自动重写 + 缓存校验
```

**决策准则**：`preview` 返回 `needs_agent_decision=False` → 走 L1 机械（insert/delete）；
`needs_agent_decision=True` → AI 推理目标公式后 `rewrite`。

## 5. 成功样例（真实运行结果）

样本：`resources/qa_test/formula_samples/formula_sum.xlsx`（SeasonStat sheet，39 公式）
结构：数据行 3-14（每行 F=SUM/G=AVERAGE/H=MAX of B:E），汇总行 15（F15/G15/H15 覆盖 F3:F14）。

### 5.1 interpret_formula（汇总格 F15）
```
formula: =SUM(F3:F14)
funcs: ['SUM'] aggregate: True lookup: False
covers_data_area: True is_last_row: True cross_sheet: False
notes: 聚合汇总；覆盖数据区；位于数据末行(典型汇总位置)；引用=F3:F14
```

### 5.2 scan_sheet_formulas（39 公式，首3+末3）
```
total: 39
  F3 : =SUM(B3:E3)      | 聚合汇总；覆盖数据区；引用=B3:E3
  G3 : =AVERAGE(B3:E3)  | 聚合汇总；覆盖数据区；引用=B3:E3
  H3 : =MAX(B3:E3)      | 聚合汇总；覆盖数据区；引用=B3:E3
  F15: =SUM(F3:F14)     | 聚合汇总；覆盖数据区；位于数据末行(典型汇总位置)；引用=F3:F14
  G15: =AVERAGE(F3:F14) | 聚合汇总；覆盖数据区；位于数据末行(典型汇总位置)；引用=F3:F14
  H15: =MAX(F3:F14)     | 聚合汇总；覆盖数据区；位于数据末行(典型汇总位置)；引用=F3:F14
```
AI 据此识别：F3-H14 是行内计算，F15/G15/H15 是末行汇总（覆盖数据区 + is_last_row）。

### 5.3 preview_formula_impact(append_rows=2) → 标语义缺口
```
needs_agent_decision: True
  gap: SeasonStat!F15(=SUM(F3:F14)): 聚合范围F3:F14末行14，新增数据行16-17未纳入范围
  gap: SeasonStat!G15(=AVERAGE(F3:F14)): 聚合范围F3:F14末行14，新增数据行16-17未纳入范围
  gap: SeasonStat!H15(=MAX(F3:F14)): 聚合范围F3:F14末行14，新增数据行16-17未纳入范围
```
行内公式（F3-H14）未被误标（单行范围过滤）。AI 见缺口 → 推理扩展范围。

### 5.4 preview_formula_impact(insert_row=5) → 机械位移正确无缺口
```
changed: 33 gaps: 0 needs_decision: False
  F15: =SUM(F3:F14) -> =SUM(F3:F15)
```
insert 机械扩展范围含新行，`needs_decision=False` → AI 直接 insert_row，无需 rewrite。

### 5.5 完整 agent 流程（append + rewrite 闭环）
自构样本（汇总放 G 列避免循环引用）：
```
1.scan: 汇总在 G13 = =SUM(F3:F12)
2.preview: S!F13(=SUM(F3:F12)): 聚合范围F3:F12末行12，新增数据行14-16未纳入范围
4.append 3 行后汇总仍 =SUM(F3:F12)（未含新行）
5.rewrite: ok=True cache=公式缓存完好，校验通过
6.汇总已扩展为 =SUM(F3:F16)，含全部数据行
PASS：agent 流程闭环（scan→preview→append→rewrite）
```

### 5.6 insert_row 机械位移闭环
```
=== E2E insert_row 位移 ===
  ok=True needs_manual_fix=False
  PASS：插行后汇总范围扩展 + 下方公式行号位移
  （F13 汇总→F14，范围 F3:F12→F3:F13；下方公式行号位移）
```

## 6. 测试

| 测试文件 | 覆盖 | 状态 |
|---|---|---|
| `test_formula_ref_shifter.py` | L1 位移规则（7 单测） | ✅ |
| `test_formula_shift_e2e.py` | L1 端到端（6 e2e） | ✅ |
| `test_formula_cache.py` | L0 缓存保护（6 场景） | ✅ |
| `test_formula_semantics_e2e.py` | **L2 + insert_row + agent 流程（16 e2e，含边界）** | ✅ 本 skill 新增 |
| `verify_formula_agent.py` | **功能完整性验证脚本（18 项，独立运行报告）** | ✅ 本 skill 新增 |

运行：
```bash
# 单元/e2e 测试
python -m server.tests.test_formula_semantics_e2e
python -m server.tests.test_formula_shift_e2e
python -m server.tests.test_formula_ref_shifter
python -m server.tests.test_formula_cache

# 功能完整性验证（结构化报告，exit code 0=全过）
python -X utf8 -m server.tests.verify_formula_agent
```

`test_formula_semantics_e2e.py` 16 e2e 覆盖：
- insert_row：位移 / 边界（范围首行下移 vs 内部扩展）
- interpret：汇总 / 跨表 VLOOKUP / 查表 / OFFSET 动态函数
- scan：全表识别 / 多 sheet 隔离
- preview：append 缺口 / insert 机械无缺口 / delete_row #REF! / delete_col #REF!
- rewrite：成功路径 / 无效 sheet 失败不崩溃
- agent 流程：append+rewrite 闭环 / insert 无需 rewrite

`verify_formula_agent.py` 10 验证项（18 子检查），含真实 `formula_sum.xlsx` 样本输出，产出 PASS/FAIL 报告，任一失败 exit=1。

## 7. 文件清单

| 文件 | 作用 |
|---|---|
| `server/agent/formula_semantics.py` | **L2 语义原语层（本 skill 新增）** |
| `server/agent/formula_ref_shifter.py` | L1 机械位移器 |
| `server/agent/formula_cache_validator.py` | L0 缓存保护（libreoffice 重算） |
| `server/agent/cli_interface.py` | CLI：`insert_row` + 4 个 L2 原语方法（本 skill 新增） |
| `server/agent/skills/formula_agent.md` | 本 skill 文档 |
| `server/tests/test_formula_semantics_e2e.py` | L2 + agent 流程 e2e（16 用例，本 skill 新增） |
| `server/tests/verify_formula_agent.py` | 功能完整性验证脚本（18 项，本 skill 新增） |

## 8. 否决方案（决策依据）

- ❌ 整列引用规约（`=SUM(F:F)`）：硬编码规范，非所有表适用，不解决"AI 理解"诉求
- ❌ 启发式识别汇总行（末行 + 含 SUM）：规则脆弱，误判风险，仍是硬编码
- ✅ agent 化语义理解：通用，判断权归 AI，工具只提供原语

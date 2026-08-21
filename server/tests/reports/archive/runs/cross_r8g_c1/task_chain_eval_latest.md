# task_chain 复合任务链评估报告（excel_LLM Agent 内环验证）

- 生成时间: 2026-08-13 14:24:17
- 样例来源: task_chain.json（1/1 条有效，0 条夹具排除）
- 评估对象: skill=on（TableAgent 全套：parse_multi + cross_table_splitter + OperationOrchestrator 占位符编排 + skill 配置）
- 执行方式: 进程内直接调用 AgentService（真实 codemaker serve LLM），每条链在 resources/ 临时沙箱副本真实执行，跑前/跑后 xlsx 行级差异作为 ground truth
- 核心增量: 链完整性 + 占位符引用一致性（consumer 引用字段 == producer 实际产出 ID）

## 一、总体指标

| 指标 | 说明 | 值 |
|---|---|---|
| 链完整率 | 整链所有 expected 步 status==matched | 0.0000 |
| truth_ok率 | 全步 row_located+字段满分+无异表多余写入 | 0.0000 |
| 引用一致率 | 占位符引用闭环成立的比例（task_chain 核心） | 0.0000 |
| producer产出率 | produces 标注的步实际回传新 ID 的比例 | 1.0000 |
| 定位功能 | 命中正确 table+sheet+操作类型 | 1.0000 |
| 覆盖度 | expected 行操作真正产出比例（扣异表多余） | 1.0000 |
| 精准程度 | 被定位行字段值完全正确比例 | 0.0000 |
| 严格通过率 | 整链 100%命中且无多余写入 | 0.0000 |
| 响应ok率 | Agent 自报告执行成功 | 0.0000 |
| 平均多余写入 | 未被 expected 认领的行改动 | 0.0000 |
| 平均异表写入 | 写到 expected 之外的表 | 0.0000 |
| 平均耗时(ms) | 单链端到端 | 105125.9 |
| P50/P95(ms) | | 105125.9 / 105125.9 |
| 总耗时(s) | | 105.1 |

## 二、失败模式归类（内环优化定位）

| 失败模式 | 计数 | 涉及链 | 优化方向 |
|---|---|---|---|
| parse_or_exec_failed | 0 | - | parse_multi 超时/LLM 不可用 → 增大超时/降级 splitter 兜底 |
| table_sheet_miss | 0 | - | 路由或 sheet 别名缺失 → 补 table_context/sheet_aliases skill |
| row_missing | 0 | - | add 未落行/modify 未定位行 → 查列定位与主键自增逻辑 |
| field_error | 2 | 1 | 字段值写错/枚举未解析/类型不符 → 补 column_aliases/enum_mappings |
| ref_broken | 1 | 1 | 占位符替换错误或 consumer 字段名错 → 修 OperationOrchestrator._capture_produced 列名派生 |
| producer_not_resolved | 0 | - | producer 新 ID 未回传 result_rows → 修 _append_row 主键回传/produces 标注 |
| extra_writes | 0 | - | 过度级联/误改它表 → 收紧 cascade_rules/反模式拦截 |
| precondition_missing | 0 | - | 夹具与配表不一致（非 Agent 缺陷）→ 同步测试夹具或配表 |

## 三、每条链详情

### 链 1: 新增灵兽'烈焰虎王'，pet_id 30010，model_id 2005，灵兽品质5，灵兽元素类型Fire。配置进化：'烈焰虎'(pet_id 30009)在进化等级30时进化为'烈焰虎王'，进化需要消耗3个'进化丹'(item_id 6001)和2个'灵兽精魄'(item_id 6002)。

- 响应ok: False | 链完整: False | 严格通过: False | truth_ok: False
- 定位 1.00 | 覆盖 1.00 | 精准 0.00 | 引用一致 0.00 (0/1) | producer产出 2/2
- 多余写入 0 (异表 0) | 耗时 105126ms
- 错误: 失败：pk_conflict - ID [2005] 已被占用

| # | table.sheet | op | produces | 状态 | 字段分 | matched_op |
|---|---|---|---|---|---|---|
| 1 | pet/pet.xlsx.Pet | add | new_pet_id | 🟠 located_only | 0.00 | 有 |
| 2 | pet/pet_evolve.xlsx.PetEvolveData | add | new_evolve_id | 🟠 located_only | 0.00 | 有 |

占位符引用闭环校验：
| consumer步# | 字段 | 占位符 | producer步# | 期望ID | 实际值 | 闭环 |
|---|---|---|---|---|---|---|
| 2 | 进化后的灵兽ID | <new_pet_id> | 1 | 2005 | None | ❌ |

## 四、表现最差链 Top5（优先优化目标）

| cid | 链完整 | 引用一致 | 覆盖 | 精准 | input |
|---|---|---|---|---|---|
| 1 | False | 0.00 | 1.00 | 0.00 | 新增灵兽'烈焰虎王'，pet_id 30010，model_id 200 |

## 五、内环优化建议

- 字段错误 2 处：补 column_aliases / enum_mappings / value_constraints，强化枚举值预解析与类型校验。
- 引用断裂 1 处 + producer 未产出 0 处：这是 task_chain 核心瓶颈。核查 OperationOrchestrator._capture_produced 主键列名派生（首列 col==1 优先）与 _resolve_placeholders 占位符替换覆盖；确保 add 结果 result_rows 回传主键新值，produces 标签与占位符名对齐。

注意事项：
- ⚪ 夹具缺失表示 expected 的 row_key 在 resources/ 真实数据中不存在（非 Agent 缺陷），已排除出统计；若需评估该链请同步夹具或配表。
- 引用一致性是 task_chain 区别于单表用例的核心指标：producer 步产出的新 ID 必须被consumer 步正确引用写入，否则跨表配置在运行期无法关联。
- 每条链在独立临时沙箱执行，互不影响；跑完即删，不污染真实 resources/。
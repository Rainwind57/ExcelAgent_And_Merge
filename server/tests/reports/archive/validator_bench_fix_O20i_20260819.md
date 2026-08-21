# O20i #30 required_fields.yaml 自动生成（§6 跨模块）

> 来源：`docs/OPTIMIZATION_LEDGER.md` §6 跨模块 TODO #30。
> 日期：2026-08-19。
> 状态：代码完成（9 单测绿，全量 1037 passed / 1 预存红零回归），e2e 阻 R7。

---

## 0. 背景

TODO #30：required_fields.yaml "README 宣称但缺" → 前期部分落地（独立文件空跑，`_load_required_fields` 读 `raw.get("required_fields", {})`），自动生成（由 index 非空列派生）留 TODO。

---

## 1. 修复

### 1.1 table_index.py 扩 col_non_empty 字段
- `SheetMeta` 加 `col_non_empty: list[int] = field(default_factory=list)`（per-col 非空计数，向后兼容默认空）。
- `_scan_sheet` 行遍历内（`actual_rows` 块）统计 per-col 非空（非 None 且 `str(v).strip()` 非空）。
- `load_index` 反序列化 `s.get("col_non_empty", [])` 兼容旧索引（无此字段 → 空 list）。
- SheetMeta 构造返填 `col_non_empty=col_non_empty`。

### 1.2 derive_required_fields.py 派生脚本（新建）
- 从 `_table_index.json` 统计每表每 sheet 每列非空率 `col_non_empty[c] / row_count`。
- 非空率 ≥ 阈值（默认 0.9，`--threshold` 可调）→ 必填列。
- row_count < 2 跳过（统计无意义）。
- col_non_empty 空/长度不匹配跳过（旧索引兼容）。
- 手工条目优先：同 stem+sheet 已有手工条目 → 不被派生覆盖。
- 输出 yaml 顶层 `required_fields` key 包裹（与 `_load_required_fields` 读取 `raw.get("required_fields", {})` 对齐）。
- CLI：`python -m agent.excel.skills.derive_required_fields --workspace ../resources --threshold 0.9`。

### 1.3 重建 index + 派生
- `build_index(Path('../resources'))` 重建 `_table_index.json`（84 tables 含 col_non_empty）。
- 跑派生 → 78 表 / 171 sheet 必填配置写入 `required_fields.yaml`。
- 示例：activity/Activity 必填 `[活动id, 活动名称]`（备注列非空率 0.2 < 0.9 非必填）。

---

## 2. 测试（`tests/test_derive_required_fields_o20i.py` 9）

### TestSheetMetaColNonEmpty（2）
| 测试 | 场景 | 期望 |
|---|---|---|
| test_default_empty_list | SheetMeta 默认 col_non_empty=[] | 向后兼容 |
| test_load_index_old_format_compat | 真实 index 含 col_non_empty + 长度匹配 headers | 字段存在 |

### TestDeriveRequiredFields（6）
| 测试 | 场景 | 期望 |
|---|---|---|
| test_derive_high_rate_columns_required | 非空率 ≥ 0.9 | id/名称 必填，备注非必填 |
| test_derive_threshold_adjustable | 阈值 0.1 | 备注也变必填 |
| test_derive_skips_low_row_tables | row_count=1 | 跳过 |
| test_derive_skips_missing_col_non_empty | col_non_empty 空 | 跳过（旧索引兼容） |
| test_derive_preserves_manual_entries | 预置手工条目 | 手工优先不覆盖 |
| test_derive_writes_required_fields_key | 输出 yaml | 顶层 required_fields key 包裹 |

### TestLoadRequiredFieldsIntegration（1）
| 测试 | 场景 | 期望 |
|---|---|---|
| test_load_reads_derived_config | 真实 index 派生后 _load_required_fields | 读到 78 stems 配置 |

---

## 3. 确定性验证

| 测试文件 | 测数 | 覆盖 | 结果 |
|---|---|---|---|
| `tests/test_derive_required_fields_o20i.py` | 9 | SheetMeta×2 + derive×6 + 集成×1 | 9/9 passed |
| 相关回归（cross_table_connectivity/decompose_agent） | 32 | table_index/locator 链路 | 32/32 passed（1 skipped） |
| 全量回归 | 1037 | 全仓库 | 1037/1037 passed（1 预存红 `test_column_matcher_semantic`，1 skipped） |

**零回归**：1 预存红 `test_column_matcher_semantic`（O5-O20h 持续存在，与所有 O 改动无关）。

---

## 4. 残留 follow-up

1. **#22 produces 阈放宽**：暂缓，需 eval 验证（行为变更，避免重蹈 R8 回归）。
2. **#37 run_one_case --no-raise**：已实现（run_one_case.py:422 `add_argument("--no-raise")` + 437 `raise_on_err=not args.no_raise`），TODO 标记滞后，需更新文档。
3. **变体样例基线**：阻 R7，需 serve 起后跑 quest_npc_variants + cross_chain 评估。
4. **required_fields 实际生效验证**：派生后 `_load_required_fields` 读到配置，但 validator `validate_field_layer` ③ 必填性检查是否真用派生配置 → 待 e2e 验证（add 操作漏填必填列应 warning）。

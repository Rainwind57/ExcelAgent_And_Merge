# R9-C1 PATCH_CONFIG 声明式 schema 守门（5 坑硬规则）

> 第二波 P0 优化 · 方法 C
> 日期：2026-08-17
> 报告数据：`patch_validator_latest.json`
> ca-overview §3.3 轻享服 5 个坑 + §3.2 PATCH_CONFIG 格式 + §3.1 _capped 是补丁文件。

## 痛点

ca-overview §3.3 明文 5 坑全是**静默失败**（改了没生效 / 融合退化 / 格式破坏 / 后者静默忽略），无任何 pre-apply 校验。§3.3 要求"merge 系统必须处理"。

## 5 条硬规则（全 hold 级）

| 规则 | 坑 | 校验 | 后果 |
|---|---|---|---|
| ① | 坑1 | PATCH_CONFIG A 列 sheet 名 ∈ trunk 主表 sheet 集合 | 融合结果不含该 sheet |
| ② | 坑5 | PATCH_CONFIG A 列不重复登记 | 先写者优先，后者静默忽略 |
| ③ | 坑3 | _capped 必须含 PATCH_CONFIG sheet | 融合退化或失败 |
| ④ | 坑4 | _capped 不能有 CONFIG sheet（补丁文件无 CONFIG） | 格式破坏 |
| ⑤ | §3.2 | PATCH_CONFIG B 列 ∈ {PATCH_GEN, SHEET_GEN} | 融合方式非法 |

## 改动清单

### `server/engine/patch_validator.py`（新建）

| 组件 | 说明 |
|---|---|
| `Violation` | dataclass：`{rule, kind=patch_config, severity=hold, sheet, message, detail}`，`to_dict` 可序列化 |
| `validate_capped_workbook(path, trunk_sheet_names)` | 5 条硬规则校验，全 hold 级；规则③命中提前返回（无 PATCH_CONFIG 则①②⑤无法校验） |
| `trunk_sheet_names` 空/None | 跳过规则①（无 trunk 参照） |

### `server/routers/validate.py`

| 改动 | 说明 |
|---|---|
| import `validate_capped_workbook` + `Path`/`List`/`BaseModel` | |
| `CappedValidateRequest` | `{path, trunk_sheets}` |
| `POST /api/validate/capped` | 返回 `{ok, violations}`，path 相对 RESOURCES_DIR 或绝对 |

### `server/tests/test_patch_validator.py`（新建）

8 用例（tmp_path 构造样例，不污染 resources）：
1. 合规 _capped — 无违规
2. 坑4 有 CONFIG — rule=4 命中
3. 坑3 无 PATCH_CONFIG — rule=3 命中
4. 坑1 sheet 不在 trunk — rule=1 命中
5. 坑5 重复登记 — rule=2 命中
6. §3.2 非法融合方式 — rule=5 命中
7. 无 trunk 跳过规则① — 不报 sheet 不在 trunk
8. `Violation.to_dict` 可 JSON 序列化

## 指标

| 指标 | 值 |
|---|---|
| 5 坑覆盖率 | 5/5 全覆盖 |
| 新单测 | 8 全过 |
| 回归测试 | 53 全过 零回归（merge_preflight + formula_gate + comment_guard + formula_cache + save_cache_scope + write_verification + fast_apply + merge_eval + merge_formula_cache + merge_progress_snapshot） |
| 零回归 | ✅ |

## 首版范围

| 范围 | 状态 |
|---|---|
| 5 坑硬规则校验函数 | ✅ 落地 |
| 独立 `POST /api/validate/capped` 路由 | ✅ 落地 |
| 8 单测（tmp_path 构造样例） | ✅ 落地 |
| C2 `_validate_apply_refs` 内补调用 | ⏳ 留后续（需确认 apply 何时写 _capped） |
| 脱敏样例 `resources/sample_capped.xlsx` | ⏳ 未放（测试自构造，避免污染脱敏目录） |
| 违规接 `pre_commit_hold`（kind=patch_config） | ⏳ 留后续（复用方法 B 通道） |

## 验证命令

```
python -m pytest tests/test_patch_validator.py → 8 passed
全套回归 → 61 passed
```

## 后续升级

- [ ] C2 apply 路径接入：`_validate_apply_refs` 或 `merge_branch` apply 写盘前调 `validate_capped_workbook`（需确认 apply 何时写 _capped vs 全量表）
- [ ] 脱敏样例放 `resources/sample_capped.xlsx` 供前端联调
- [ ] 违规接 `pre_commit_hold`（kind=`patch_config`），复用方法 B 通道

# O17 方法 E Schema 血缘联动

> 轮次：O17（2026-08-18）
> 范围：E1-E3 闭环 — trunk 加列后预检 _capped.xlsx/ca/dev/* 同名表缺此列。E4 agent 注入留 follow-up（agent 不写 _capped）。见 `docs/OPTIMIZATION_LEDGER.md` §1 + §3 + `docs/archive/事前预防优化TODO.md` 第三波 E。
> 前置：方法 B（precommit_hold 通道）就位（kind=structure_sync_missing 文档预留，代码自由 str 无门禁）。

## 设计

新建 `column_lineage.py` 扫多分支根目录所有 .xlsx 数据 sheet 列定义（row1=header/row2=type/row3=constraints），建 `{table -> {sheet -> {column -> present_in_branches}}}` 图。`_capped` 后缀统一 table_key（pet_capped 与 pet 同 table）。`sync_preview` 返回 trunk 有列其他分支缺的清单。

## 改动清单

| 项 | 文件 | 改动 |
|---|---|---|
| E1 新模块 | `server/engine/column_lineage.py`（新文件） | `ColumnLineageEntry`/`ColumnLineageGraph` dataclass（present_in_branches 记录列出现在哪些分支）；`compute_column_lineage(branch_roots) → ColumnLineageGraph`（扫多分支根 .xlsx，`_capped` 后缀统一 table_key，跳过 CONFIG/说明 sheet）；`sync_preview(table, sheet)` 返回 trunk 有列其他分支缺的清单；`_scan_sheet_columns(ws)` helper（读 row1-3 列定义，不跳过 CONFIG 但 caller 过滤）。 |
| E2 column_added | `server/routers/structural.py` `compute_column_changes`（新函数） | `src_columns/tgt_columns: {table -> {sheet -> set(column_names)}}` → `kind=column_added`（src 有 tgt 无）。changed 需 type 数据（现仅 set 比对）留 follow-up。 |
| E3 sync-preview 路由 | `server/routers/validate.py` `GET /structural/sync-preview`（新） | `?table=xxx&sheet=yyy` → compute_column_lineage(RESOURCES_DIR + 子目录) + sync_preview。 |

## 确定性验证

```
python -m pytest server/tests/test_column_lineage_o17.py -q
=> 12 passed in 2.22s

python -m pytest server/tests/ -q
=> 973 passed, 1 failed, 1 skipped in 214.28s
   # 1 预存红：test_column_matcher_semantic::test_fuzzy_simplified_colname_hits
   #   与 O17 无关（列匹配器优先级，未触及 column_matcher.py）
```

## 残留 follow-up

- **E4 agent 注入**：agent.py 不写 _capped（全 server 无写 _capped 代码），注入语义需 testtest 有 capped 目录才能 e2e。可改为 trunk 主表加列前调 sync_preview 预检，但 trunk 写入走 cli `_save_with_cache_check`，注入点需评估是否复用 hold_events 通道。
- **column_changed**：需 type 数据比对（现 compute_column_changes 仅 set 比对），留后续扩 compute_column_changes 签名传 type dict。
- **前端 sync-preview 拦截卡**：MergeGuideView.vue 接 structure_sync_missing 事件渲染（minified 需重建）。
- **方法 G/H**：八方法残留（见 ledger §3）。

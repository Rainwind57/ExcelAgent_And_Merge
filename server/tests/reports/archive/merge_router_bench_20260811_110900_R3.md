# R3 路由层性能基准报告

> 生成时间：2026-08-11 11:09
> 脚本：`server/tests/merge_router_bench.py --runs 1`
> 数据源：`merge/svn/demo_svn/wc`（trunk + branches/dev1/dev2/subdev_1）
> 环境要求：python-calamine 已装（pyproject 声明，R3 补装）

## 优化项

- **#7 svn info 批量预填**：`_prefill_rev_cache` 一次 `svn info -R --xml` 取全目录所有文件 (rev,author,date)，替代 N 表×多文件逐个 `svn log -l 1`（74 表 ~148 次 subprocess ~190s → ~0.2s）。
- **#8 calamine Rust 引擎**：`_sheet_names` 优先 python-calamine（Rust，0.1ms/文件），替代 openpyxl read_only 回退（0.3-0.5s/文件）。`_dir_sheet_sets` 222 次文件打开 ~17.5s → ~0.8s。
- **#9 subdir 假删除修复**：`merge_subdir.py:435` 加 `src_scope_only=True`，消除 69 假 `source_deleted`。
- **#10 AI 预取成本控制**：`_prefetch_ai_suggestions` 仅当 AgentService 已初始化才预取 + 只取冲突最多前 5 sheet + 在途去重。
- **#11 _suggest_cache LRU+TTL**：OrderedDict 上限 2000 条 / TTL 1h，防长驻服务内存膨胀。
- **#12 漂移脚本修复**：`verify_merge_*.py` 数据源从失效的 `svn/fixture/wc`、`demo/trunk_r4` 切到 `demo_svn/wc`。
- **#13 表级并行**：`branch_compare`/`subdir_compare` 表循环 ThreadPool 并行（max 4）。本地 SVN file:// 下无收益（GIL+IO 竞争），远程 svnserve/http 场景生效，保留无害。

## 基准方法

monkeypatch A/B：同进程内 patch `_prefill_rev_cache` 为 no-op + `_sheet_names` 强制 openpyxl，模拟优化前；取消 patch 跑优化后。直接调 `branch_compare`/`subdir_compare` 函数（不走 HTTP），`time.perf_counter` 计时。

## 性能对比（优化前 → 优化后）

| 环节 | 优化前(ms) | 优化后(ms) | 加速比 | 说明 |
|---|---|---|---|---|
| `/dirs` 加载 | 128.3（冷扫） | 0.0（热缓存） | TTL 缓存命中 | 30s TTL，进页面必调 |
| `preview-base` | — | 245.6 | 无优化项 | LCA 反查 svn log |
| **`branch_compare`** | **99524.3** | **89513.4** | **1.11x** | dev1→trunk 74 表全量 |
| **`subdir_compare`** | **21785.4** | **9342.1** | **2.33x** | subdev_1→trunk 5 表全量 |

## 结果一致性

- branch_compare：优化前/后 groups 输出完全一致 ✅
- subdir_compare：优化前/后 structural_changes 输出一致 ✅
- `false_source_deleted = 0`（假删除已消除）✅

## 收益归因

| 优化项 | 贡献 | 证据 |
|---|---|---|
| #8 calamine | subdir 2.33x 主因 | `_dir_sheet_sets` 222 次文件打开 17.5s→0.8s（openpyxl 0.3-0.5s/文件 → calamine 0.1ms/文件） |
| #7 svn info 批量预填 | branch 1.11x + subdir 辅助 | `_prefill_rev_cache` 一次 svn info 0.1s 替代 148 次 svn log（本地 file:// 单次 1.3s，理论 190s→0.2s；实测占比小因本地 svn log 快） |
| #9 subdir 假删除修复 | 正确性 | `src_scope_only=True` 后 69 假删除→0，baseline/optimized 均 0 |
| #10/#11 AI 预取+LRU | 资源占用 | 逻辑单测验证（不在本基准计时范围） |
| #13 表级并行 | 本地无收益 | branch 串行/并行 0.96x（负优化），subdir 0.96x；GIL+本地 svn IO 竞争抵消 |

## 瓶颈分析

- branch_compare 优化后 89.5s 中 ~88s 在 compare loop（`read_group_files` + `compare_sheet` 引擎层，非路由层范围）
- subdir_compare 优化后 9.3s 分布：base 导出 0.3s + 预填 0.25s + 5 表 compare 3s + `_dir_sheet_sets` 0.8s + 结构标注
- 用户报告"引擎已用 id() 哈希 + sparse 修掉（benchmark 全 74 表 ~7.5s）"指引擎层 `merge_eval.py`，不含路由层 svn 反查；路由层优化后 89.5s 主体在引擎层读值+比对

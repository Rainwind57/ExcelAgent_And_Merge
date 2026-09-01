# Excel-Agent × Merge 性能与评测数据汇总（单一事实源）

> **本文件用途**：汇总 Excel-Agent（4-Step 多 Agent 配表）与 Merge（SVN 三方合并）两线全部**真实运行/评测/压测数据**，供答辩 PPT、后续优化与协同者参考。
> **数据来源**：全部来自仓库内**归档报告 + 评测脚本输出 JSON + 实测复跑**，非臆造。每条标注证据文件路径。
> **铁律**：不同样例集不可横比；所有"提升%"均为**同集 before→after**。含"设计值/目标"的条目明确标注，不与实测混。

---

## 〇、快速结论（一页版）

| 维度 | 关键数字 |
|---|---|
| Agent LLM 调用 | 6 步线性 23+ 次 → 4-Step 3-5 次（**-80%**） |
| Agent 定位准确率 | 66.7% → 80.6%（S-A 8 条） |
| Agent 引用一致（quest_npc） | 6% → **100%** |
| Agent 高复杂跨表样例 | 早期全失败 → **6/6 通过**（每例 LLM 19 次） |
| Merge compare（大表） | ~48s → ~6.4s（**7.5×**） |
| Merge apply | ~19.6s → ~4.3s（**3.8×**），提交量 46.4MB→0.1MB（**-99.8%**） |
| Merge 假冲突率 | 0.5 → 0 |
| 可靠性 | 回归 180 项全过、单测 323+ 零回归 |
| 本轮优化（对话树占位符闭环等） | 嵌套占位符漏检 7/8→0、命名漂移假阳性 5/6→2/6、聊天噪音 66.7%→0% |

---

## 一、Excel-Agent：4-Step 多 Agent 配表流水线

### 1.1 架构级指标（6 步线性 → 4-Step 重构）

| 指标 | 6 步线性（08-10） | 4-Step 终态（08-17） | 提升 |
|---|---|---|---|
| 一次配表 LLM 调用 | 23+ 次 | 3-5 次 | **-80%** |
| 跨 6 表全链耗时 | 10+ 分钟 | 目标 <90s（**设计值**，受外部 serve 阻塞未实测） | 目标 >85% |
| 意图覆盖度（命中表数） | 2/8 | 8/8 | +12pts |
| 漏字段率 | 70%+ | <10% | -60pts |

证据：`docs/archive/优化全过程.md` §2.6 + `docs/答辩PPT内容文档-45min.md`

### 1.2 端到端评测（R1，S-A 集 8 条，skill=on before/after）

| 指标 | before | after | 提升 |
|---|---|---|---|
| 定位功能 | 0.6667 | 0.8056 | +20.8% |
| 覆盖度 | 0.6111 | 0.7500 | +22.7% |
| 精准程度 | 0.5185 | 0.6917 | +33.4% |
| 严格通过率 | 0.1667 | 0.3333 | +100% |
| 平均耗时(ms) | 82236 | 98637 | trade-off |
| 响应 ok 率 | 0.8333 | 0.3333 | 预期下降（拒错数据） |

证据：`docs/archive/优化全过程.md` R1 节

### 1.3 4-Step Loop 6 复杂样例实测（bench_iter_results，parallel=1，当前基线）

| # | 样例 | 复杂度 | 墙钟(s) | LLM调用 | ok | 失败数 |
|---|---|---|---|---|---|---|
| 1 | 封印魔龙（跨6表+占位符闭环） | 高 | 464.3 | 19 | ✓ | 0 |
| 2 | 幽冥宗门派（跨3目录6+表） | 高 | 681.6 | 19 | ✓ | 0 |
| 3 | 九尾天狐全生命周期（跨2目录4表） | 高 | 947.3 | 19 | ✓ | 0 |
| 4 | 万圣狂欢（跨6表 add+modify） | 高 | 225.1 | 19 | ✓ | 0 |
| 5 | 聚灵塔（同 xlsx 5 sheet） | 高 | 323.7 | 19 | ✓ | 0 |
| 6 | 复合修改+删除级联（跨4表） | 高 | 372.4 | 19 | ✓ | 0 |

> **★ 核心结论**：6/6 全过、每例稳定 LLM 19 次。对比早期 `bench_v2`(parallel=3) 全 Timeout、`bench_myrun`(parallel=2) 几乎全败 → **串行 parallel=1 是稳定性基线**（LLM 服务端共享阻塞下，稳 > 快）。

证据：`bench/bench_iter_results.md` / `bench/bench_v2.md` / `bench/bench_myrun.md`

### 1.4 单表/低复杂实测（bench_v1.json，parallel=1）

| # | 样例 | 耗时(s) | ok |
|---|---|---|---|
| 1 | 新增灵兽朱雀（单表 add） | 29.9 | ✓ |
| 2 | 查询灵兽饕餮（单表 get） | 20.6 | ✗（未找到"饕餮"，给相近行提示） |
| 3 | 改灵兽资质（单表 modify） | 25.4 | ✗（未知错误兜底） |
| 4 | 新增建筑瞭望塔（复合语句） | 33.5 | ✓ |
| 5 | 删除测试兽（单表 delete） | 14.3 | ✗（未知错误兜底） |
| 6 | 灵兽相关表（QA 问答） | 9.2 | ✓ |

证据：`bench/bench_v1.json` / `docs/archive/bench_v1.md`

### 1.5 真实 LLM e2e 诊断（O19，R7 解封后，bench_4step.py）

| # | 样例 | ok | 问题 | 代码可修 | LLM 缺口 |
|---|---|---|---|---|---|
| S1 | 封印魔龙 | True | 重复写入+覆盖不足 | ✅ 去重钩子缺 | ⚠ 候选不全 |
| S3 | 九尾天狐 | True | 覆盖不足(pet 1/4) | ✅ 候选策略 | ⚠ 单表漏拆 |
| S4 | 万圣狂欢 | False | 错表+未知错误+parser崩 | ✅ alias 错+classifier | ⚠ 错路由 |
| S6 | 复合修改 | False | modify 失败 | ✅ classifier regex | - |
| S2/S5 | 幽冥宗/聚灵塔 | False | str+list 崩（O19 已修） | ✅ | - |

> 结论：失败主因多为**代码层缺信号**（去重/错误分类/alias），非 LLM 本身。

证据：`docs/bench_failure_fix_requirements.md`

### 1.6 skill 简版（30 样例确定性，零 LLM 波动）

| 指标 | off | on |
|---|---|---|
| 定位成功率 | 0.933 | **1.000** |
| 精确命中 | 0 | **1.00** |
| 严格综合 | 0.704 | **0.959** |
| 平均耗时(ms) | 533 | **321（减半）** |

### 1.7 skill 真写盘（8 样例，S-A 集）

| 指标 | off | on |
|---|---|---|
| 定位 | 0.472 | 0.639 |
| 覆盖 | 0.417 | 0.583 |
| 精准 | 0.497 | 0.590 |

证据：`docs/archive/优化全过程.md` skill 节

### 1.8 quest_npc 单链深化（R7→R8c）

| 指标 | 原始 | 阶段8 | R7 | R8 | R8c |
|---|---|---|---|---|---|
| 引用一致 | 0.06 | 0.875 | 0.62 | **1.00**(达标) | 0.94 |
| 覆盖 | - | - | 0.82→0.91 | 0.91 | **1.00** |
| 精准 | - | - | 0.56→0.76 | 0.76 | 0.78 |
| 耗时(s) | - | - | 134→115 | 115 | **107** |

证据：`docs/archive/优化全过程.md` R7-R8c 节

### 1.9 复杂级联样例（R4，case 106-111，S-B 集）

| 指标 | before | after |
|---|---|---|
| 用例 106 覆盖度 | 0.16 | 峰值 0.39（+143%） |
| 用例 106 耗时 | 204s | 112s（**-45%**） |
| 用例 107 精准 | 0 | 0.79 |
| 平均耗时(off) | - | 114s |

### 1.10 大数据量压测（bench_perf_tables_latest，10k 行表）

| 指标 | get（查物攻资质） | set（改物攻资质→9999） |
|---|---|---|
| ok | ✓ | ✓ |
| 耗时(ms) | 31204 | 332337 |
| 准确率 | ✓ | ✗（语义层偏差） |
| LLM 调用 | 0 | 0 |

- 成功率 100%、准确率 50%、LLM 0 次（全规则）
- ⚠ p95 >30s，大表存在行索引/上下文构建瓶颈

证据：`server/tests/reports/bench_perf_tables_latest.{md,json}`

### 1.11 可靠性

| 指标 | 数值 | 证据 |
|---|---|---|
| 回归通过 | Agent 109 项 + Merge 核心 71 项 = **180 项** | `docs/archive/优化全过程.md` |
| 单测 | 145 → 204 → **323+ 项零回归** | 同上 + `server/tests/` |
| 确定性定位成功率 | 6 轮迭代恒 100% | S-G 集 |

---

## 二、Merge：SVN 目录分支三方合并

### 2.1 端到端（HTTP 实测）

| 操作 | before | after | 提升 |
|---|---|---|---|
| branch_compare（全表） | ~48s | ~6.4s | **7.5×** |
| apply 处理（big_data 单表） | ~19.6s | ~4.3s | **3.8×** |
| apply 上传 payload | 46.4MB | 0.1MB | **-99.8%** |
| compare 序列化 | 4.9s | 0.3s | **16×** |

证据：`merge/scripts/benchmark_report.md`

### 2.2 分阶段复算（S0→S3）

| 阶段 | branch compare(处理) | 序列化 | big_data apply |
|---|---|---|---|
| S0 未优化 | 31.42s | 5.39s | 19.23s |
| S1 读取加速 | 6.78s | 5.87s | 19.76s |
| S2 序列化加速 | 7.71s | 0.33s | 19.30s |
| S3 当前 | 7.31s | 0.29s | 4.19s |

### 2.3 路由层 A/B（R3，monkeypatch 确定性）

| 环节 | before | after | 加速 |
|---|---|---|---|
| subdir_compare（5 表） | 21.8s | 9.3s | **2.33x** |
| branch_compare（74 表） | 99.5s | 89.5s | 1.11x（瓶颈在引擎层） |
| calamine 222 次文件打开 | 17.5s | 0.8s | openpyxl 0.3-0.5s/文件 → 0.1ms |
| svn info 批量（rev_cache） | N 次 svn log ~190s | 一次 svn info ~0.2s | **~950×** |
| 假 source_deleted | 69 | 0 | 正确性 |

### 2.4 引擎层（R6 #24/#32/#33）

| 指标 | before | after |
|---|---|---|
| 假冲突率（seed id=2） | 0.5 | 0（语义归一 100/100.0/1e2） |
| 未变更表跳过 | 全 74 表 compare | **64/74 跳过（86%）**，branch 54.2→49.3s（1.10x） |
| ProcessPool 并行（8 表 3000×150） | ThreadPool 3.69s | **1.84s（2.0x）** |
| 10 万行大表全链 | 卡死 | ~10.4s（compare 3.9s + apply 6.3s） |

### 2.5 公式/读取快路径

| 指标 | before | after |
|---|---|---|
| 公式检测 zip 快扫（10w 行） | openpyxl 全量 ~6s | zip 扫 `<f>` 标签 ~0.05s（**~120×**） |
| 10w 行 schema 读取 | openpyxl ~5.5s | calamine ~0.05s（100×，Rust） |

证据：`docs/Merge部分详细萃取.md` + `server/agent/excel/formula/formula_cache_validator.py:229`

### 2.6 AI 建议（与 4-Step 联通）

| 指标 | before | after |
|---|---|---|
| 冲突建议调用 | N 次网络往返（每格 15-45s） | 1 次批量 LLM（约 30-60s） |
| 失败回退 | - | 逐格并行（≤8 worker） |
| 核心策略 | - | 纯代码不 AI 化（AI 只进建议路径） |

### 2.7 新鲜复跑（2026-08-31，bench_merge_before_after.py）

```
before(全表compare): 62147.4ms
after (skip+归一):   58617.3ms
speedup_33: 1.06x
no_change_tables: 0    ← 当前 demo_svn 快照 trunk/dev1 rev 全不同 → #33 未命中
conflicts: 18 = 18     ← 当前快照无 "100"vs"100.0" 型假冲突
```

> ⚠️ 与 08-11 历史"64/74 跳过 1.10x"不矛盾——**是快照差异**（08-11 rev 全等才触发 #33；当前快照全部 rev 不同）。引用时按来源标注日期。

证据：`server/tests/reports/bench_merge_before_after_latest.json`（2026-08-31 实跑）

### 2.8 merge_router_bench（reports 归档）

| 指标 | 值 |
|---|---|
| branch_speedup | 1.6 |
| subdir_speedup | 1.96 |
| dirs 冷加载 | 161ms → 热缓存 0ms |
| 结果一致性 | true |
| false_source_deleted | 0 |

证据：`server/tests/reports/merge_router_bench_latest.json`

---

## 三、合并：Main Table（答辩主表）

| # | 引擎 | 改动 | before | after | 提升 |
|---|---|---|---|---|---|
| 1 | Agent | 4-Step 重构 | 23+ 次 LLM / 10+ min | 3-5 次 / <90s 目标 | **-80%** |
| 2 | Agent | 索引定位 | 全表扫描 6.8s | 0.58s | **11.7x** |
| 3 | Agent | R1 语义门+级联 | 定位 0.667 / 精准 0.519 | 0.806 / 0.692 | +20.8% / +33.4% |
| 4 | Agent | R1 严格通过 | 0.167 | 0.333 | **+100%** |
| 5 | Agent | R4 跨表模板 | 覆盖 0.16 / 204s | 0.39 / 112s | +143% / -45% |
| 6 | Agent | R8 column_matcher | 引用一致 0.06 | 1.00 | 达标 |
| 7 | Agent | skill 简版 30 样例 | 定位 0.933 / 533ms | 1.000 / 321ms | +7pts / 减半 |
| 8 | Agent | 单测体系 | 145 → 204 | 323+ | 零回归 |
| 9 | Agent | 大表全链（10w） | 卡死 | ~10.4s | compare 3.9s+apply 6.3s |
| 10 | Merge | compare 稀疏化+calamine | ~48s | ~6.4s | **7.5×** |
| 11 | Merge | apply fast_apply XML | ~19.6s / 46.4MB | ~4.3s / 0.1MB | 3.8× / -99.8% |
| 12 | Merge | compare 序列化 | 4.9s | 0.3s | **16×** |
| 13 | Merge | 路由层 calamine+svn批量 | subdir 21.8s / branch 99.5s | 9.3s / 89.5s | 2.33x / 1.11x |
| 14 | Merge | 假删除修复 | 假 source_deleted 69 | 0 | 69→0 |
| 15 | Merge | 语义归一 | 假冲突 0.5 | 0 | 归零 |
| 16 | Merge | 未变更表跳过 | 全 74 表 | 64/74 跳过 | 86%（1.10x） |
| 17 | Merge | ProcessPool 并行 | ThreadPool 3.69s | 1.84s | **2.0x** |
| 18 | Merge | 公式快扫（10w） | ~6s | ~0.05s | **~120×** |
| 19 | Merge | AI 批量建议 | N 次往返/每格 15-45s | 1 次 LLM | N→1 |

---

## 四、已知边界与诚实项

1. **Agent `<90s` 为设计目标**：复杂跨表受外部 `codemaker serve` 阻塞（143.8k token/156s，非 agent 代码可修）未实测达成。单表/复合实测见 §1.4。
2. **bench_v1 的 modify/delete 样例 ok=false**（§1.4 #3/#5）——如实标注，属"未命中"非崩溃。
3. **大表压测准确率 50%**（§1.10 set 语义偏差）——已暴露待优化点。
4. **#33 未变更跳过**：当前 demo_svn 快照 rev 全不同，未触发；08-11 历史 rev 全等时 64/74 跳过。按日期引用。
5. **ProcessPool 真并行**：需 worker 模块完全隔离才可启，当前 demo_svn 回退 ThreadPool。
6. **skill 早期负收益**（jj 08-03 locate -0.024）——R1-R9 系统性优化后转正，勿混淆版本。

---

## 五、本轮优化模拟压测（2026-09-01，未提交代码改动）

> 背景：本轮有一批未提交的真实代码优化（decompose_agent/validator_agent/step3_execute_subagent/operation_orchestrator/step4_conclude_subagent/agent_service 等），围绕"对话树循环依赖占位符闭环""命名风格漂移误报""聊天区降噪""LLM调用成本控制""Step4口径修复"。以下 5 项均为**真实调用仓库函数 + 构造固定 fixture** 的模拟压测（个别子项因依赖过重上下文退化为简化状态机，已在对应 md 里逐条标注），不与真实端到端准确率混用。

| # | 模块 | 指标 | before | after | 脚本/证据 |
|---|---|---|---:|---:|---|
| 1 | `validator_agent._norm_name` | FORWARD_REF_BROKEN 假阳性（12组样例） | 5/6 | **2/6** | `tools/simulate_validator_forwardref_norm.py` |
| 2 | `step3_execute_subagent` 占位符扫描 | 8条混合样例漏检嵌套占位符 | 7 | **0** | `tools/simulate_placeholder_nested_backfill.py` |
| 2b | 循环依赖回填（简化状态机模拟） | 5节点链式依赖可执行节点数 | 1/5 | **5/5** | 同上 |
| 3 | `agent_service` 聊天降噪 | 25条混合会话展示行数/噪音占比 | 24行/66.7% | **8行/0%** | `tools/simulate_agent_service_noise_filter.py` |
| 4a | `decompose_agent` 分组硬上限 | 12候选实际跑的分组数/LLM子调用 | 3 | **2** | `tools/simulate_decompose_chain_cost.py` |
| 4b | `decompose_agent` dict列lint | 3个合法dict列场景误清空率 | 100% | **0%** | 同上 |
| 5 | `step4_conclude_subagent.all_ok` | 8种(s1,s2,s3)组合中误报场景数 | 3/8 | **0/8** | `tools/simulate_step4_allok_drift.py` |

证据（对应报告）：`bench/ppt_validator_forwardref_norm.md`、`bench/ppt_placeholder_nested_backfill.md`、`bench/ppt_agent_service_noise_filter.md`、`bench/ppt_decompose_chain_and_dict_lint.md`、`bench/ppt_step4_allok_drift.md`

### Skill 系统机制补充（非新增效果数据，是对 §1.6/1.7 的机制说明）

- 四层结构：`L0`人工根目录 / `L1_derived`自动派生 / `L2_runtime`运行时学习 / `L3_anti_patterns`反模式，物理路径 `server/agent/excel/skills/`。
- 转正安全阀（`skill_updater.py` `promote_with_guard`）：候选 → 快照 → 写盘 → mini 回归(30样本, lift≥0.05) → 通过转 active / 不通过回滚+隔离区(30天)。
- AI 归纳的反模式需额外命中 3 次才可能转正（比确定性规则更严，因无 ground-truth）。
- 诚实边界：`L2_runtime/column_aliases.runtime.yaml` 当前样例库为空骨架，机制已建成但未跑出规模化数据。

---

## 六、数据源路径速查

| 数据 | 路径 |
|---|---|
| 4-Step Loop 6 样例 | `bench/bench_iter_results.md` / `bench_v2.md` / `bench_myrun.md` |
| 单表低复杂 | `bench/bench_v1.json` + `docs/archive/bench_v1.md` |
| 真实 LLM e2e 诊断 | `docs/bench_failure_fix_requirements.md` |
| 端到端优化全程 | `docs/archive/优化全过程.md` |
| 指标对照表 | `docs/archive/优化指标对照表.md` |
| Merge 性能报告 | `merge/scripts/benchmark_report.md` |
| merge 路由 A/B | `server/tests/reports/merge_router_bench_latest.json` |
| 大表操作压测 | `server/tests/reports/bench_perf_tables_latest.{md,json}` |
| merge #33/#24 A/B 新鲜跑 | `server/tests/reports/bench_merge_before_after_latest.json` |
| 本轮优化模拟压测（5项） | `bench/ppt_validator_forwardref_norm.md` / `ppt_placeholder_nested_backfill.md` / `ppt_agent_service_noise_filter.md` / `ppt_decompose_chain_and_dict_lint.md` / `ppt_step4_allok_drift.md` |
| 公式快扫 | `server/agent/excel/formula/formula_cache_validator.py:229` |
| 答辩 PPT 内容 | `docs/答辩PPT内容文档-45min.md` |
| 单一事实源（桌面版） | 桌面 `性能优化指标整合文档.md`（本文件为其仓库内镜像+扩展） |

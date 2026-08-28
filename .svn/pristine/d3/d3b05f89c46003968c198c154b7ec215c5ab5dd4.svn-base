# Excel-Agent 与 Merge 双线优化总结

> 本文整合 Excel-Agent（CRUD 自然语言配表）与 Merge（多版本差异比对合并）两部分的开发过程总结、已落地项核证、深度问题剖析、以及基于真实代码核证的可靠提升方案与路线图。
> 承接既有四份核心文档（`优化全过程.md` / `excel-agent问题与优化方向.md` / `合并引导性能优化诊断.md` / `事前预防优化TODO.md`），不重复其内容，仅做结构化提炼与跨线整合。
> 数据来源：SVN r31519→r32867 历史 + 真实代码核证（截至 2026-08-17）。

---

## 〇、项目与双线定位

- **项目**：`ai-excel-tool v2.0.0`——AI 辅助 Excel 游戏配表工具，四能力：自然语言 CRUD / 多版本 diff-merge / 表格浏览 / 数据验证。技术栈 FastAPI + openpyxl + langchain/langgraph + codemaker serve LLM + Vue3。
- **Excel-Agent 线**：`server/agent/excel/` 子包，从用户自然语言到 Excel 写盘的多步管道（拆解→定位→分解→校验→执行→回填→汇总）。
- **Merge 线**：`server/routers/merge_branch.py` / `merge_subdir.py` / `engine/`，三方（base LCA + source + target）svn-based 比对 + 冲突解决 + apply 回写。
- **源码根**：`server/`，`excel/` 子包已分层为 `cli/core/parser/pipeline/repair/locator/subagent/skills`。

---

## 一、Excel-Agent 线优化总结

### 1.1 已落地里程碑（R1→R8g，累计成果）

| 轮次 | 主题 | 核心提升 |
|---|---|---|
| R1 | Prompt 升级 + 值语义门 + SET PK 级联 + bug 修 | 定位 +20.8% / 覆盖 +22.7% / 精准 +33.4% / 严格通过 +100% |
| R2 | skill AI 反模式归纳（失败 trace → pending_review 反模式） | LLM 从 8 失败 trace 归纳 4 反模式（PK 冲突 / seed 不存在 / conv_id 定位 / 类型转换） |
| R3 | merge 路由层性能 | subdir_compare 2.33x（21.8→9.3s）/ 假 source_deleted 69→0 |
| R4 | agent 配表层（Step5 追踪 + 6 用例） | 用例 106 耗时 204→112s（-45%）/ 用例 107 精准 0→0.79 |
| R5 | on 轮卡点修复 | semantic_outlier 对 add 放宽 / splitter 跳 Step3 AI / parse_multi 空返回回退规则 |
| R6 | merge 引擎层 + 前端 | #24 语义归一 / #33 未变更跳过 / #32 ProcessPool / #38 批量采纳 / #39 冲突排序 |
| R7 | quest_npc 单样例深化 | option_go 接任务选项 + branch_conv 原文尾保护（trace 双证确定性收益） |
| R8 | type_aliases 嵌套字段整名匹配 | quest_npc 引用一致 0.62→**1.00**（达标） |
| R8b | 关系图驱动的 produces 推断层 | 结构性消除 per-template produces 硬编码 |
| R8c | verify-repair fix_fields 校验 + 显式 PK 字面代换 | quest_npc ok=True / cov 1.00 / 107s |
| R8g | LLM 链分解器（schema 注入 + 产每表一 op） | mail ok=True/cov 1.00 / pet LLM 产 Pet+PetEvolveData |

**累计维度**：单测 145→204、通过率 100%（零回归）；端到端定位 0.67→0.81、覆盖 0.61→0.75、精准 0.52→0.69、严格通过 0.17→0.33；quest_npc 单样例引用一致 0.06→1.00。

### 1.2 核心难点与根因（R1-R9，按严重度）

| 根因 | 性质 | 关键解决 |
|---|---|---|
| **R1 别名污染**：「奖励」→formula_nested 误路由 0.90 置信关掉歧义 | 致命 | `alias_mapping.json` 改→reward + 补支线/采集/NPC |
| **R2 quest 规则不可达**：措辞变体穷举成 regex 失败 | 致命 | 复杂输入走 LocatorAgent→DecomposeAgent 链 |
| **R3 列名单向子串**：表头是子串才匹配 | 严重 | `_column_in_text` 双向 + 公共子串窗口 |
| **R4 歧义消解短路关 LLM**：单候选→is_cross_table=False | 严重 | `_is_complex_input` 保全部候选 + FK 关系图扩表 |
| **R5 DecomposeAgent 不能发现表**：只能分解 locator 已选 | 严重 | FK 图朝向式扩表补对端 stem |
| **R6 splitter 模板盲区**：不覆盖 quest/reward 新增 | 严重 | 复杂输入直走 LLM 链跳模板 |
| **R7 serve 端 agentic LLM 慢/贵**：143.8k token/156s | 性能/成本 | 非代码可修，缩小单 prompt 范围并行 + 隔离 session |
| **R8 ValidatorAgent LLM 假实现**：无 _call_llm | 质量 | 真接 LLM 前向引用裁决 opt-in |
| **R9a/R9b 路径错位 + 双命名空间** | 环境 | 逐级向上找路径 + conftest 收口 sys.path |

### 1.3 深度问题剖析（最有提炼价值）

#### 1.3.1 架构悖论：「规则主、LLM 兜底」是反向的

> `excel-agent问题与优化方向.md:106-117`

- **规则过强**：一个 0.90 错别名一票否决式选错表 + 关歧义 → 关掉 LLM
- **规则过浅**：游戏域语义（支线/采集/完成奖励）无法穷举成 regex
- **LLM 兜底位置错**：被放在规则后且只在 `is_cross_table` 真时介入，恰恰是规则"自信选错"的单候选场景最需 LLM 却被关
- **LLM 触发后上下文病态**：一次喂全表 schema（76MB `_table_index.json`）→ 慢/贵/易超时

**正确方向**：复杂意图 LLM 主、简单意图规则主。规则只适合单动词单表 + 已固化硬模板链；凡含对话/选项/支线/奖励/采集/多 id 引用等聚合信号走 LLM。

#### 1.3.2 R7：非代码可修的服务侧 blocker（最重要洞察）

对照实验锁定（`excel-agent-diagnosis.md:193-201`）：

| 输入 | 结果 |
|---|---|
| trivial prompt「回复 OK」 | 1.7s ✓ |
| DecomposeAgent prompt（含 quest/reward/interaction stem） | 90/180s 返回空 ✗ |
| prompt 显式加「不要调用任何工具，不要读取/搜索文件」 | 仍 90.6s 空回复——serve 忽略约束 |

**结论**：serve 端对「prompt 文本出现的表 stem」做自动文件读取上下文，90s 超时返回空。这是 codemaker serve 端**固定行为**，excel-agent 代码侧唯一能做的临时绕过 = **缩小每次 LLM 调用范围（scoped decompose）**；根治需 serve 侧关 auto-context / 提供纯文本补全端点。

#### 1.3.3 过拟合温床：per-template 硬编码 produces

> `优化全过程.md:776-778`

R8 跨链变体（进化/道具/邮件）引用一致 0.00 证明：splitter per-template 硬编码 produces 是过拟合单一 quest_npc 案例的温床——无模板链 produces 缺失 → 通用 topo 引擎无边可建。R8b 落地 `produces_inference.py` 关系图驱动是结构性解法；R8g LLM 链分解器进一步让 splitter <2 intent 时 LLM 接管——**LLM 主、规则为安全网**真正泛化。

#### 1.3.4 确定性证据链方法论（项目最成熟的方法论资产）

8 条样本 LLM 方差远大于确定性增量（如 R2 case5/6/7 全 0 拉低指标）。项目建立的方法论：
- **确定性验证（零 LLM 波动）**：单测 + monkeypatch A/B + samples 脚本作硬证据
- **trace step 级验证**：`CODEMAKER_STEP5_TRACE=1` 实时打印每子任务序号/表/sheet/动作/耗时/ok/产出 ID/失败
- **unit + trace 双证**：确定性收益不应因 LLM 方差回退而回滚
- **端到端指标作方向参考**：含 LLM 波动非硬证据

### 1.4 结构性债务：双骨架分裂（关键洞察）

项目存在**两套并行执行骨架**，但现代化基础设施只在一套用：

| 设施 | 文件 | CRUD 链 | Pipeline |
|---|---|:---:|:---:|
| 7 步编排 + 断点续跑 | `core/checkpoint.py` + `pipeline/pipeline.py` | ✗ | ✓ |
| 多 Agent 并发派发 + 超时 + 失败隔离 | `subagent/dispatcher.py` | ✗ | ✓ |
| 角色化 SubAgent（Dialog/ItemNpc/Task 3 角色） | `subagent/roles.py` | ✗ | ✓ |
| 失败回退 Step3 重试 1 次 | `pipeline.py:303-314` | ✗（走 verify-repair 轮次循环模式不一致） | ✓ |
| Pre-commit hold 通道 + 漏行预检 + audit | `routers/precommit_hold.py` | ✗ | ✓（merge apply） |
| 拓扑排序 + produces 推断 + 占位符替换 | `core/operation_orchestrator.py`/`produces_inference.py` | ✓（Step5） | ✓（Step4/6） |
| 证据层（按 stem 分文件 jsonl + 文件锁 + 轮转） | `core/evidence_logger.py` | 写但消费者不明 | 未接 |
| 失败案例库 + 反模式自学习三层 skill | `skills/L1_derived`/`L2_runtime`/`L3_anti_patterns` | R2 #6 落地归纳 | RAG 检索未落地 |

**双骨架分裂本质**：`core/agent.py` 6 步 CRUD 链 vs `pipeline.py` 7 步 Pipeline 都执行"拆解→分区→填表→校验→写库"，但 dispatcher 多 Agent 并行 / checkpoint 断点续跑 / roles 角色化 SubAgent / 失败回退 Step3 都只在 Pipeline 用。后果：

- **能力浪费**：CRUD 链走单 DecomposeAgent 串行，复杂跨表意图无法多 Agent 并行（正是 R7 慢/贵根因之一）
- **改进不互惠**：优化 dispatcher / checkpoint 只惠及 Pipeline；CRUD 链卡死即重跑
- **维护重复**：两套代码重复耦合 LLM 调用方式

### 1.5 结构性提升方案（基于实际代码）

#### 方案 A：双骨架统一为「CoreEngine + 规模分层」（主轴，一改三赢）

**目标**：抽出共享 `excel/engine_core/` 层，CRUD 链与 Pipeline 变为同引擎两规模。CRUD 链复杂输入自动走多 Agent 并行，Pipeline 大文件复用同一引擎。

**接线**：
1. 新建 `excel/engine_core/`，把 `subagent/dispatcher.py` + `roles.py` + `core/checkpoint.py` + `core/operation_orchestrator.py` + `pipeline/verifier.py` 收口为共享层（仅挪位置 + 接通，不重写）
2. 改 `core/agent.py:3661-3683` 链触发：复杂输入（`_is_complex_input` 已判）→ `dispatcher.dispatch` 派发 `roles.py` 的 Dialog/ItemNpc/Task 多角色 SubAgent **并行**，替代单 DecomposeAgent 逐候选串行
3. `pipeline.py:_build_subagents` 复用同一 `engine_core.dispatch`
4. 简单输入走原 fast-path 不动

**收益**：agent 能力提升（多 Agent 并行）+ 性能（并行加速 + R7 prompt 缩小到 10-20k）+ 用户体感（多 Agent 协作）。**直接解 R7 慢/贵**。

**红线**：角色化 SubAgent schema 注入需复用 `_type_aliases`（R8）避免字段失配；简单输入不可误入多 Agent 路径（complexity gate 严）；保"规则做得了的不抢"原则。

#### 方案 B：CRUD 链引入断点续跑 + 增量 patch（用户最大受益）

**目标**：CRUD 链失败不再重头跑全盘重改，可断点续 + 用户中途纠正只补缺失项。

**接线**：
- `core/agent.py:run` 每步完成调 `CheckpointManager.update(step_id, "done", ...)`，复用现成的 `checkpoint.py:_maybe_skip`/`_restore_step_state` 模式
- 已落地 `POST /reply` + `_ask_callback` + `ask` SSE（TODO#39）接 checkpoint：用户 reply 时写 `steps[id].status=pending`，从断点续跑而非进程重启
- verify-repair 失败达上限不再 ABORT，落 `pre_commit_hold`(kind=semantic_outlier/id_conflict) → 反问用户 → 增量 patch 续跑

**收益**：156s 卡死后用户改个参数能续跑而非从头重跑；E2E 体感时延大幅降。

**红线**：需复杂意图才启用（`CODEMAKER_CRUD_CHECKPOINT=1` opt-in 避回归）。

#### 方案 C：Pre-commit hold 通道下沉到 CRUD 链（post-repair → pre-prevent 范式跃迁）

**目标**：`routers/precommit_hold.py` 已有 `PreCommitHoldEvent` + `preflight_row_manifest` + `record_hold_audit` + `emit_hold_sse`，目前仅接 merge。下沉到 CRUD 链 `_phase_execute` 前，对 add/set/delete 做 pre-flight（id 冲突 method F / 占位符未替换 TODO#21 / 悬空 FK TODO#34 / patch_config 守门 method C）。

**接线**：`core/agent.py:_phase_execute`（行 ~5318）前调 `preflight_op`；命中 emit SSE hold；`CODEMAKER_CRUD_PREFLIGHT=hold|warn|off` 默认 warn。

**收益**：写盘前拦数据丢失（id 冲突静默覆盖、悬空 FK 写废数据），从"事后 verify-repair 抢救"变"事前 hold 防未然"。

#### 方案 D：Evidence×RAG×AntiPattern 三层自学习闭环（越用越准）

**目标**：`core/evidence_logger.py` 已按 stem 写证据 jsonl，但无消费者。建 RAG 检索让证据变成"能力"：

- 新建 `excel/rag_searcher.py`：jieba + `_table_index.json:search_blob` 做 BM25 召回（零新依赖）
- `DecomposeAgent._build_prompt` / `validator_agent` LLM 路径前，BM25 召回 K 条同表族同语义的 `dialog_failures/*.jsonl` 案例做 few-shot 注入
- 与 R2 #6 反模式归纳形成双层闭环：
  - **pre-failure**：`L3_anti_patterns.yaml` 在 `_check_anti_pattern` 做 lookup 拦截
  - **post-failure 检索**：RAG 召回真实失败案例做 few-shot 引导 LLM 不重蹈

**收益**：跨链变体（pet/mail/item 目前引用一致 0.00）无模板链走 LLM 兜底时，RAG few-shot 把同族过往失败经验喂给 LLM，无模板链成功率提升。

---

## 二、Merge 线优化总结

### 2.1 已落地里程碑（R3 + R6 + 后续 ProcessPool 升级）

| 轮次 | 优化项 | 收益 |
|---|---|---|
| R3 | #7 svn info 批量预填 rev_cache / #8 calamine Rust 引擎 / #9 src_scope_only 假删除修 / #10 AI 预取成本控制 / #11 _suggest_cache LRU+TTL / #13 表级并行 | subdir_compare 2.33x（21.8→9.3s）/ 假 source_deleted 69→0 |
| R6 | #24 语义相等归一 / #25 splitter intent 跳 Step4 AI / #33 未变更表跳过 compare / #32 ProcessPool 引擎层真并行 / #38 一键批量采纳 / #39 冲突密度排序 | 假冲突率 0.5→0 / demo_svn 64/74 表跳过(86%) / ProcessPool samples 2.0x |
| 后续 | #32 升级（阈值 9999→4 + 防 hang + worker 隔离） | R6.5 待修的 demo_svn 卡死已解，74 表 ProcessPool 4 workers 应见 ~3-4× |

### 2.2 已自动生效但用户可能不知（先告知）

| 项 | 现状（代码核证） | 说明 |
|---|---|---|
| #32 ProcessPool 真并行 | `engine/parallel_compare.py:33` 阈值 4 / `:76` ProcessPoolExecutor / `:79` as_completed(timeout=60) / `:81` future.result(timeout=30) / `:91-97` except 自动回退 ThreadPool | R6.5 注意事项"待修的卡死"已修；worker `_compare_one_table_proc` module-level + partial 可 pickle，符合隔离约定 |
| progress_cb 进度回调 | `merge_branch.py:1109`/`merge_subdir.py:457` 已传 `progress_cb=_tbl_progress` | 每完成一表回调 `{done,total}`，但**前端尚未消费**（见方案 ⑤） |
| svn info 批量预填 | `merge_branch.py:1091-1092` `_prefill_rev_cache(src_dir, ...)` + `(tgt_dir, ...)` 两次 | 一次 svn info -R 替代 N 表 × 多文件逐个 svn log（~190s→0.2s） |
| base 批量导出优化 | `merge_branch.py:1080` `_prepare_base_export` 一次性 svn export 整目录 | N 张表从本地拷贝，避免 N 次 per-table svn cat |

### 2.3 待落地可靠方案（按 ROI，基于真实代码核证）

#### 方案 ② P0-3：base export 按 `base_rev` 磁盘复用（直击 10s 主因）

**核证现状**（`merge_branch.py:799-880`）：三种 base 来源都 `dest = tmp / f"base_export_r{rev}"`，而 tmp 是每次 compare 新建临时目录 → 连续两次同 base_rev 合并每次都重 `svn export -r`（120s 超时单次最大成本）。`merge_subdir.py:173` 的 `_prepare_base_export_subdir` 同源同问题。

**改造**（最小变更）：
```python
_BASE_EXPORT_CACHE_ROOT = Path(os.environ.get("MERGE_BASE_EXPORT_CACHE",
                                              MERGE_DIR / ".base_export_cache"))
# 改 dest:
dest = _BASE_EXPORT_CACHE_ROOT / f"base_export_r{rev}"
if dest.exists() and dest.is_dir() and any(dest.rglob("*.xlsx")):
    return dest  # 命中，零 subprocess
# 后续 _svn_export_dir 不变；加 SNAPSHOT_KEEP=5 retention（参 TODO#23 _prune_snapshots 模式）
```

- **安全**：SVN rev 不可变 → 同 rev 必同内容
- **收益**：连续比对同分支同 base_rev 从 10s 段首跳到 0s
- **风险**：低。需配 mtime/容量清理（参 `.snapshots` retention 已有范本）
- **工作量**：0.5-1 天
- **测试**：同 base_rev 连续两次 compare，第二次应跳 export（perf 计时单测，参 R3 bench 模式）

#### 方案 ③ P0-1：无变更跳过提前到 `read_group_files` 之前

**核证现状**（`merge_stages.py:220-235`）：`_build_group` 先 `read_group_files(paths)`（line 220 全量解析 xlsx），**才**判 `version_meta` rev 全等跳过（line 235 #33）。#33 跳过了 `compare_sheet` 但**没跳 IO+解析**——浪费一次全量读取。

**改造**（最小变更）：把 line 235-269 的 rev 全等跳过块上移到 line 220 之前。rev 已由 `_prefill_rev_cache`（一次 `svn info -R`，零文件读）取，无需 read_group_files 即可判。

- **安全**：rev 全等即内容相同（SVN 不可变）；缺 rev/不一致仍走原路径
- **收益**：demo_svn 74 表中 64 表 svn 未改动（86%），跳过 read_group_files——每表省 ~0.12s，大表（多 sheet 10w 行）单表省秒级，**整批 88s 引擎瓶颈砍 IO 段大头**
- **风险**：低，但 `no_change` 分支产出的 SheetDiff 不能漏 `headers`——复用现 line 243-269 已写好的"从 base_rows_raw 取 headers"块即可
- **工作量**：0.5 天
- **复用 #33**：与 #33 不重复——#33 跳 compare_sheet，P0-1 跳 read_group_files 更上游

#### 方案 ④ P0-2：`_collect_table_keys` 加 `(dir, mtime)` 缓存

**核证现状**（`merge_branch.py:606-620`）：纯 `rglob("*.xlsx")` 无缓存。被调 8 处（`/dirs` 4 次、`/tables`、`branch_compare` 2 次、`subdir_compare`）。`_DIRS_CACHE` 30s TTL 是端点结果级，不是 `_collect_table_keys` 本身级。冷 `/dirs` 列 380 分支 = 380 次 rglob。

**改造**：
```python
_TABLE_KEYS_CACHE: dict[tuple[str, float], set] = {}  # (dir_path, max_mtime) -> keys
_TABLE_KEYS_LRU = 256
def _collect_table_keys(base_dir: Path) -> set:
    if not base_dir.is_dir(): return set()
    try:
        m = max((f.stat().st_mtime for f in base_dir.rglob("*.xlsx")
                 if not f.name.startswith("~$")), default=0)
    except OSError: m = 0
    cached = _TABLE_KEYS_CACHE.get((str(base_dir), m))
    if cached is not None: return cached
    keys = {...原 rglob...}
    if len(_TABLE_KEYS_CACHE) >= _TABLE_KEYS_LRU: _TABLE_KEYS_CACHE.pop(next(iter(_TABLE_KEYS_CACHE)))
    _TABLE_KEYS_CACHE[(str(base_dir), m)] = keys
    return keys
```
更稳可改用 svn rev 作 key（已 _prefill 取过）。

- **安全**：svn up 改 mtime 变 → 自动失效
- **收益**：/dirs 冷启动（380 目录）+ 同分支多端点访问免重复 rglob
- **风险**：低
- **工作量**：0.5 天

#### 方案 ⑤ progress_cb → SSE 进度接前端（彻底告别"加载中无反馈"）

**核证现状**：后端 `_tbl_progress` 已在每完成一表调 `progress_cb("compare_tables", done, total)`，但 `merge_branch.py:1209` progress/{id} SSE 端点似乎只细化到 `resolve_branches` 阶段，表级进度未推前端。前端 `MergeGuideView.vue` 已有 `tableTabs` computed + `batchAdoptHighConfidence`/`sortByConflict` 等 R6 落地，但无 progress bar。

**改造**：
- 后端：`_compare_task_emit` 复用现成 SSE emit pattern（已用于 resolve_branches），把 `_tbl_progress` 闭包改成直接推 `event=compare_tables data={done,total,table,last_action}`（约 10 行）
- 前端 `MergeGuideView.vue`：现有 `tableTabs computed` 旁加 `progressBar` computed（监听 SSE `compare_tables` 事件），显示 `done/total` + 完成表名

- **收益**：74 表 batch 不再"加载中"几秒无反馈；用户能预判卡哪张表
- **风险**：极低，纯 UI 层零算法风险
- **工作量**：0.5 天

#### 方案 ⑥ apply 接入 `precommit_hold`：防漏行静默丢数据（铜墙铁壁）

**核证现状**：`routers/precommit_hold.py` 已完整（漏行预检 + hold 事件 + audit），但 `merge_stages.py:_validate_apply_refs:351` 只做悬空引用 **warning 不阻断**；`preflight_row_manifest` 函数就位却没接到 apply 链路。

**改造**（接线点 `merge_branch.py` apply 端点 / `merge_stages.py:stage3_apply`）：
```python
# apply 前调 preflight_row_manifest（base_pks 复用 _validate_apply_refs 的 extra_pks）
report = preflight_row_manifest(mr, base_pks)
if report.will_silently_drop and os.environ.get("CODEMAKER_PREFLIGHT_HOLD","hold") == "hold":
    emit_hold_sse(_compare_task_emit, task_id, report.holds[0])  # 推 SSE 让前端拦
    raise HTTPException(409, detail=report.to_dict())  # hold 阻断
```
对应前端 `MergeGuideView.vue` 加红 card（仿 #38 popup 模式）+ override 按钮。

- **收益**：ca-overview §2.3.1 痛点（testbranch 全量覆盖丢 id=10500）从"事后发现"变"事前拦截"
- **风险**：低，默认 hold 实测无误，可先 warn 灰度；`extra_pks` 已有零额外 IO
- **工作量**：1 天

#### 方案 ⑦ P1-1：解析结果磁盘缓存以 `(file_path, svn_rev)` 为 key

**核证现状**（`engine/parser.py:26`/`:123`）：`read_excel` / `read_formulas_and_comments` **零缓存装饰器**（grep 全 engine 仅 `compare.py:15 _col_letter` 有 lru_cache）。每次 compare 全量重解析所有 xlsx。

**改造**：
```python
_CACHE_ROOT = Path(os.environ.get("MERGE_PARSE_CACHE",
                                   Path(tempfile.gettempdir()) / "merge_parse_cache"))
def _cache_get(file_path, rev):
    key = hashlib.md5(f"{Path(file_path).resolve()}@r{rev}".encode()).hexdigest()
    p = _CACHE_ROOT / key[:2] / (key + ".msgpack")
    return p, msgpack.unpackb(p.read_bytes()) if p.exists() else None
def read_excel_cached(file_path, rev): ...
```
`read_group_files` 调用点改为传 rev 走 `_cached` 变体。

- **安全**：rev 不可变 → 必同结果
- **收益**：重复比对同 rev 表免解析，10w 行大表秒级节省；与 P0-1 正交
- **风险**：中。内存涨需 LRU；公式/批注随 rev 失效；需 `msgpack-numpy` 依赖（或 pickle）
- **工作量**：2 天

#### 方案 ⑧ P1-3：公式/批注 sheet 不整体回退，走向量化 + sparse 补差

**核证现状**（`engine/compare.py:614-621`）：`if not formulas_active and not comments_active and not detect_missing` 才走 `_compare_sheet_vectorized`；含公式或批注就整张退回 `for pk, gi in key_order` Python 三重循环（line 622+）。

**改造**：拆分职责——值层 always 跑向量化，公式/批注只对 vec 标出的 inserted/deleted/conflict 行 sparse 补差，n² 退化为 O(变更行数)：
```python
if not detect_missing:
    vec_rows = _compare_sheet_vectorized(file_rows, ...)  # 包含值层
    if formulas_active or comments_active:
        _patch_sparse_formula_comments(vec_rows, file_formulas, file_comments)
    return vec_rows
```

- **收益**：公式表（项目大量含公式列配表）从 10~50× 慢路径到接近向量化速度
- **风险**：中高。需保证 diff 语义一致（公式文本/批注 author+text 三方比对规则不变），必补 `test_merge_eval.py` 契约 + `samples_first_batch.py` 样例
- **复用**：与 R6 #24 `_semantic_key` 协同（值归一后再用 vec 比对）
- **工作量**：2-3 天

---

## 三、双线对比与协同效应

### 3.1 共性方法论

| 维度 | Excel-Agent 表现 | Merge 表现 |
|---|---|---|
| LLM 在判定路径 | 「复杂意图 LLM 主、简单意图规则主」原则 | AI 建议（confidence）但列策略纯代码（已确认不 AI 化） |
| 确定性证据 | monkeypatch A/B + unit + trace step 双证 | svn rev 不可变 → 缓存安全 + 真 svn 比对作 ground truth |
| 失败处理演进 | verify-repair 轮次 → hold/prevent 反问续跑 | warning 不阻断 → preflight hold 事前拦截 |

### 3.2 共享基础设施（双线协同接口）

| 设施 | 现状 | 双线协同方案 |
|---|---|---|
| `routers/precommit_hold.py` | excel-agent 未接（方案 C 待落地）；merge apply 未接 preflight（方案 ⑥ 待落地） | 双线共用 `PreCommitHoldEvent` + `emit_hold_sse`：CRUD 链 kind=semantic_outlier/id_conflict；merge kind=missing_rows/dangling_refs |
| `subagent/dispatcher.py` + `roles.py` | 只 Pipeline 用 | 方案 A 双骨架统一——CRUD 链复杂意图复用 dispatcher 并行 |
| `core/checkpoint.py` | 只 Pipeline 用 | 方案 B CRUD 链断点续跑复用同一 CheckpointManager |
| `core/evidence_logger.py` + `skills/L3_anti_patterns` + `dialog_failures/` | evidence 写但无消费者 | 方案 D 双线 LLM 路径共用 RAG few-shot 召回 |
| ProcessPool + dispatcher 并行骨架 | excel-agent 复杂意图并行（方案 A）；merge 表级并行（已落地） | 同一 dispatcher 设计模式双线复用 |

### 3.3 协同效应

1. **excel-agent 方案 A 双骨架统一** 与 **merge ProcessPool 升级已生效** 共用 dispatcher 并行骨架 → 一处优化双线受益
2. **excel-agent 方案 C pre-commit hold 下沉** 与 **merge 方案 ⑥ apply 接 hold** 共用 `precommit_hold.py` → 一次性把 hold 通道双线接通
3. **excel-agent 方案 D Evidence×RAG** 与 **merge AI 建议路径** 共用失败案例库 + BM25 召回 → 越用越准双线协同
4. **excel-agent R7（缩小单 prompt 范围）** 与 **merge 流式 SSE** 主题一致 → 缩小粒度并行是双线共同性能杠杆

---

## 四、统一落地路线图（按 ROI）

### 4.1 双线 ROI 矩阵

| 序 | 项 | 线 | 工作量 | 风险 | 收益维度 |
|---|---|---|---|---|---|
| 1 | ⑤ progress_cb→SSE 接前端 | Merge | 0.5 天 | 极低 | UX 立竿见影 |
| 2 | ② P0-3 base export 按 rev 复用 | Merge | 0.5-1 天 | 低 | 性能 直击 10s 主因 |
| 3 | ③ P0-1 无变更跳过提前 | Merge | 0.5 天 | 低 | 性能 砍 IO 段 |
| 4 | ④ P0-2 _collect_table_keys mtime 缓存 | Merge | 0.5 天 | 低 | /dirs 冷启动 |
| 5 | A1 抽 dispatcher+roles 共享层（零行为变更） | Agent | 1 天 | 低 | 基础设施解耦 |
| 6 | A2 CRUD 链 complex 路径接多 Agent 并行 | Agent | 2-3 天 | 中 | agent 能力质变 + 解 R7 慢 |
| 7 | B CRUD 断点续跑 + 增量 patch | Agent | 2 天 | 中 | UX 体感大改善 |
| 8 | ⑥ apply 接 precommit_hold | Merge | 1 天 | 低 | 数据安全 事前防丢 |
| 9 | C Pre-commit hold 下沉 CRUD | Agent | 1-2 天 | 低 | 数据安全 范式跃迁 |
| 10 | D Evidence×RAG 闭环（BM25） | Agent | 2-3 天 | 中 | 无模板链成功率↑ |
| 11 | ⑦ P1-1 解析结果磁盘缓存 | Merge | 2 天 | 中 | 大表秒级节省 |
| 12 | ⑧ P1-3 公式 sheet 不整体回退 | Merge | 2-3 天 | 中高 | 公式表 10~50× |
| 进阶 | 方法 H 多 Agent 对抗网 Red/Blue/Auditor | Agent | 5-8 天 | 高 | 主动狩猎失败模式 |
| 进阶 | P2 pysvn PoC / SSE 持久+流式 | Merge | 各 1 周 | 高 | 进阶性能 |
| 进阶 | serve 侧 R7 关 auto-context / 纯文本补全端点 | Agent | — | 外部依赖 | R7 根治 |

### 4.2 推荐波次

| 波次 | 内容 | 周期 | 主要受益 |
|---|---|---|---|
| 1 | Merge ①②③④（已生效告知 + P0 三件套 + ⑤ UX） | 2-3 天 | merge 性能 + 体验质变 |
| 2 | Agent A1+A2（双骨架统一主轴） | 3-4 天 | agent 能力质变 + 解 R7 |
| 3 | Agent B + 双线 hold 接通（B + ⑥ + C） | 3-4 天 | 数据安全 + UX 体感 |
| 4 | Agent D + Merge ⑦⑧（自学习 + 缓存深化） | 5-7 天 | 长期质量 + 大表性能 |
| 5 | 进阶方向（方法 H / pysvn / serve R7 根治） | 视外部资源 | 进阶跃迁 |

### 4.3 验收基线（确定性证据）

| 类 | 验证手段 | 命中标准 |
|---|---|---|
| 零回归 | `conftest.py _reset_skill_caches` autouse + 204 单测回归 | 0 新增失败 |
| 性能 | monkeypatch A/B + `merge_router_bench.py` 计时 + `samples_process_pool.py` | 至少 1.5× 或绝对值下降 |
| 端到端 | `table_case_eval.py` 跑 quest_npc + 跨链变体 + R3 真实 demo_svn bench | 单测零回归 + 指标不退化 |
| 用户体验 | 真实 SVN 工作副本跑 branch_compare + 前端 SSE 进度 + 前端 hold card | 可见 X/Y 表进度 + 拦截漏行 |

---

## 五、锚定原则与边界

### 5.1 已明确不改的边界（来自 `优化全过程.md:1080`）

| 不做项 | 原因 |
|---|---|
| merge 核心 AI 化 | AI 建议足够，列策略纯代码确定性更可靠 |
| 公式缓存重算交给 AI | 工程问题，officecli/libreoffice 重算方案对路 |
| 列名匹配主路径换 AI | column_matcher 别名 yaml 确定性任务，AI 留兜底 |
| 行定位主路径换 AI | 倒排索引+分层匹配已够，AI 只在多行歧义仲裁 |

### 5.2 总原则

- **AI 进「判断+生成」主路径，不进「确定性匹配」主路径**。规则做好的不抢，规则做不了的才接。
- **确定性证据优先**：LLM eval 噪声大，用 monkeypatch A/B + 单测 + trace step 级作硬证据，端到端指标作方向参考。
- **已埋基础设施优先复用**：`precommit_hold.py` / `dispatcher` / `checkpoint` / `evidence_logger` 已就位，先接通再新建。
- **SVN/Excel 不可变语义优先利用**：rev 不可变 → 缓存安全；不可变源作缓存 key 是确定性收益的根基。
- **复杂意图 LLM 主、简单意图规则主**：避免规则过强（误选 + 关 LLM）又过浅（穷举失败）的反向架构。

### 5.3 双线共同性能范式

- **缩小粒度并行**：excel-agent 多 Agent 并行（每角色 schema 小）；merge 表级 ProcessPool + sheet 级 ThreadPool
- **不可变作缓存根**：excel-agent 失败案例不可变 → RAG 检索安全；merge svn rev 不可变 → 解析/diff 结果缓存安全
- **事前预防胜于事后抢救**：excel-agent pre-commit hold 防 id 冲突；merge preflight 漏行预检防静默丢数据

---

## 六、一句话总结

项目当前最大的结构性机会是**把已埋的现代化基础设施从只服务单一骨架下沉共享**——`precommit_hold` 双线接通（CRUD ⑥ + merge ⑤）、`dispatcher/checkpoint/roles` 双骨架统一（方案 A + ProcessPool 已生效）、`evidence_logger/dialog_failures/anti_patterns` 双线 RAG 共用（方案 D）；同时 merge 性能侧三条 P0 低风险快赢（base export 按 rev 复用 + 无变更跳过提前 + collect_table_keys mtime 缓存）在 SVN rev 不可变语义下确定性收益。这一揽子方案同时提高 agent 能力、增强 merge 性能、便于用户后续使用（断点续跑 + pre-flight 防丢 + 实时进度），且消除双骨架分裂的维护债务。

---

> 本文承接 `docs/优化全过程.md` R1-R8g 已落地记录、`docs/excel-agent问题与优化方向.md` 根因清单、`docs/合并引导性能优化诊断.md` 性能方案、`docs/事前预防优化TODO.md` 八方法 TODO；新增内容为双线结构化提炼、双骨架分裂洞察、双线协同效应、基于代码核证的可靠方案编号 ②-⑧ 与 ROI 矩阵。

---
marp: true
theme: default
paginate: true
size: 16:9
style: |
  section { font-size: 21px; }
  h1 { font-size: 34px; }
  h2 { font-size: 27px; }
  h3 { font-size: 23px; }
  table { font-size: 15px; }
  code { font-size: 14px; }
---

# AI 配表助手：Excel-Agent 与三方合并引擎

## 技术架构 · 设计权衡 · 性能优化 · 创新点

<!-- 答辩人：___　 部门：___　 日期：___ -->

---

## 目录

1. 背景与问题定义
2. 总体架构
3. Excel-Agent：智能配表引擎
4. Merge：三方合并引擎
5. 性能优化（Agent 侧 + Merge 侧）
6. 测试与质量保障
7. 核心创新点
8. 总结与展望
9. 问答环节（Q&A）

---

# 1. 背景与问题定义

---

## 1.1 业务背景

游戏策划配置数据存放于 **60+ 张 Excel 表**（灵兽 / 道具 / 建筑 / 任务 / 战斗 / 副本等），
多人并行开发时存在两类高频问题：

| 问题 | 现状 | 影响 |
|---|---|---|
| 配表效率 | 手工改 Excel，跨表外键靠人肉查 | 慢、易错、易漏外键 |
| 合并冲突 | 多分支改同一张表，人工 diff | 冲突遗漏 → 数据丢失 / 上线事故 |

**目标**：用自然语言驱动配表（Agent），用三方合并解决多分支并行冲突（Merge），二者共用同一套表结构知识。

---

## 1.2 技术挑战

| # | 挑战 | 难点 |
|---|---|---|
| 1 | 自然语言 → 精确的"表/Sheet/列/行" | 中文别名、模糊指代、多意图、跨表外键 |
| 2 | 多版本三方合并 | "100" vs 100 假冲突、跨分支 ID 撞车、公式引用 |
| 3 | 大表性能 | 10w 行 openpyxl 读写秒级→需亚秒级 |
| 4 | AI 成本与稳定性 | LLM 调多了贵、调少了错、故障需降级 |
| 5 | 数据安全 | AI 误写不可逆 → 需备份/回滚/校验 |

---

# 2. 总体架构

---

## 2.1 系统架构

```
FastAPI :8000 ──HTTP──▶ CodeMaker Serve :8666 (LLM 底座)
    │
    ├─ Agent  (/api/agent/*)
    │     ├─ qa 分支  → QAHandler（table_index 问答）
    │     └─ crud 分支 → TableAgent（NL→解析→匹配→openpyxl 读写）
    ├─ Merge  (/api/compare, /api/merge/branch|subdir)
    │     └─ engine/（compare + id_resolver + ref_integrity + fast_apply）
    └─ Tables / Workflow / Validate / Skills
```

**三层解耦**：`routers`（API 层）→ `engine`/`agent`（领域层）→ `openpyxl`/`lxml`（IO 层）

---

## 2.2 关键设计原则

1. **单一知识源**：`_table_index.json` + `value_constraints.yaml` 同时服务 Agent 与 Merge，避免两套元数据漂移
2. **降级优先**：LLM 不可用 / 策略缺失 / 快路径不满足 → 回退保守路径，绝不硬失败
3. **正确性 > 自动化**：类型不符、低置信、悬空引用一律"留人工"，不强行自动合并
4. **可观测**：全链路 thinking / tool / step SSE + 证据日志 + LLM 调用计数

---

# 3. Excel-Agent 智能配表引擎

---

## 3.1 主智能体（LangGraph 编排）

```
START → classify(意图分类) ─┬─ qa   → QAHandler.answer → END
                            └─ crud → TableAgent.run    → END
```

- **意图分类**：规则短路优先，未命中才走 LLM（省 token、降延迟）
- **dry_run 模式**：`classify` 复用同一图，预览不写盘（临时副本上执行）
- **流式思考**：`_think`/`_step` 回调推 SSE，前端可折叠展示 thinking + tool 块

---

## 3.2 四层匹配引擎

| 层 | 模块 | 职责 | 关键技术 |
|---|---|---|---|
| L1 注册中心 | `table_index.py` | 60+ 表索引 | MD5 增量刷新 |
| L2 模糊匹配 | `fuzzy_matcher.py` | 值候选搜索 | 子串 0.5 + 编辑 0.35 + 重叠 0.15 |
| L3 表格定位 | `table_locator.py` | 文本→表/Sheet | 5 级递进 + 歧义消解 |
| L4 上下文构建 | `llm_context.py` | LLM prompt | Token 感知 + 7 Skill 注入 |

- 别名系统：`灵兽/宠物 → pet.xlsx`，三层优先级（表精确→表通配→全局通配）
- 文件监听：watchdog 防抖 1s，外部改表自动增量刷新索引

---

## 3.3 双管道设计

### 7 步管道（文件输入：.md/.xlsx 剧本批量配表）

```
Step0 断点检查 → Step1 拆解(parse_file→DocIntent+符号映射)
→ Step2 分区(表/sheet 定位) → Step3 并行填表(asyncio.gather 派发 SubAgent)
→ Step4 汇总(拓扑排序+符号闭环校验) → Step5 验证(skill 规则终检)
→ Step6 写库(cli instrument + 占位符回查) → Step7 清理+报告
```

### 4-Step V2（自然语言多表指令）

```
Step1 parse → Step2 validate → Step3 execute → Step4 conclude
```

- 三态 `ok`：True / False / **None(pending)**，避免"行已写但某步失败"误判
- 零-LLM fallback：Splitter baseline（11 模板）+ ColumnExtractor

---

## 3.4 子 Agent 并行与容错

- `dispatcher.py`：`asyncio.gather` 并行派发 N 个 SubAgent
- **隔离失败**：单 Agent 失败不拖垮整批，返回 `ok=False` fragment
- **超时控制**：单 Agent 120s 超时（`CODEMAKER_SUBAGENT_TIMEOUT`）
- **定向重试**：仅重试失败者，保留成功 fragment
- **嵌套 loop 处理**：thread worker 内 `asyncio.run` 新 loop，兼容同步 `chat()`

角色化 Agent：`DecomposeAgent` / `LocatorAgent` / `ValidatorAgent` / `LLMAgent`

---

## 3.5 可靠性与安全机制

| 机制 | 模块 | 说明 |
|---|---|---|
| 备份/审计/回滚 | `backup_audit.py` | 写前备份 + JSONL 审计 + 按索引回滚 |
| 断点续跑 | `checkpoint.py` | 每步原子写 `_checkpoint.json` |
| 语义闸门 | `semantic_gate.py` | 低置信不自动执行，交人工确认 |
| 错误分类 | `error_classifier.py` | 8 类 ErrorType 定向回退 |
| 反模式归纳 | `skill_updater.py` | AI 从失败归纳 anti_patterns 反馈环 |
| 公式缓存保护 | `formula_cache_validator.py` | save 前后快照对比，丢失则 LibreOffice 重算 |
| 证据日志 | `evidence_logger.py` | 全链路可追溯 |

---

# 4. Merge 三方合并引擎

---

## 4.1 三方合并模型

```
        merge-base（公共祖先，SVN copyfrom 反查）
        /        \
    ours       theirs（两个分支最新版本）
        \        /
       merge result
```

- **merge-base 反查**：`svn log --stop-on-copy` 定位 copyfrom-rev，替代手工 fork 快照
- **两种流程**：跨分支（absorb/merge_back）+ 同分支子目录合回（subdir）

---

## 4.2 行匹配与差异分类

以**第一列为主键**对齐所有版本的行：

| row_type | 语义 |
|---|---|
| `matched` | base 与衍生都有该主键 |
| `inserted` | 仅衍生有（新增行） |
| `deleted` | 仅 base 有 |
| `missing_row` | base 有但所有分支都缺（M3 漏行 P0 告警） |

单元格三级：`changed`（单值变化）/ `conflict`（多衍生值 distinct>1）/ `formula`（公式列）

---

## 4.3 语义相等归一（核心算法）

消除"表示差异导致的假冲突"，三方判定与多数表决共用同一把尺：

```python
def _semantic_key(v):
    if v is None: return ('none', '')
    if isinstance(v, bool): return ('bool', v)   # 避免 True==1 误判
    if isinstance(v, (int, float)): return ('num', float(v))
    s = str(v).strip()
    if s == '': return ('none', '')
    try: return ('num', float(s))
    except: return ('str', s)
```

`"100"` vs `100`、`"a "` vs `"a"`、`0.1` vs `"0.10"` → 判为**相等**，不再误报冲突。

---

## 4.4 列策略自动合并

`merge_strategies.yaml` 驱动，按 `表 → sheet → 列` 三级匹配策略：

| 策略 | 规则 | 适用列 |
|---|---|---|
| `base_priority` | 保留基准值 | ID/外键 |
| `take_newer` | 取首个非空衍生值 | 数值 |
| `take_longest` | 取最长字符串 | 文本 |
| `take_max` | 取最大数值 | 数值 |
| `range_check` | [0,100] 内取衍生否则基准 | 百分比 |
| `manual` | 留人工 | 默认 |

自动合并后**仍做类型校验**（`value_constraints.yaml`），类型不符降级人工，防止"等级列 int 被写成 string"。

---

## 4.5 冲突推荐与多数表决

`recommend_version` 对未配置策略的单元格做**多数表决启发式**：

```
多数版本同值(count>=2)
  ├─ 与基准一致 → 推荐保留基准
  └─ 与基准不同 → 推荐多数值
全不同 → 保守回退基准（提示评估）
```

- 表决用 `_semantic_key`，`100/100/"1e2"` 计入同一票
- 非强制，仅"⭐推荐"提示，用户可自选
- 预留 AI 基座接入点，未来替换为 LLM 推荐

---

## 4.6 ID 冲突重映射（编号冲突处理）

**核心场景：两分支同时新增同编号，但内容不同**

```
分支A 新增 编号=99（内容 X）   ┐
分支B 新增 编号=99（内容 Y）   ┴─ 合并时主键撞车，但内容是两条不同实体
```

**处理流程（`id_resolver.py`）**：

```
步骤1 内容判定：多分支同主键 inserted 行，比对整行签名
   ├─ 内容相同 → 视为重复提交，保留单条（不是冲突）
   └─ 内容不同 → 拆成两条独立新行 ← 正是上述场景

步骤2 主键重映射：先到先得 + 后到者自增
   首个来源保留 99，后到者分配 max+1 的新编号（跳过已占用）

步骤3 外键联动：id_mapping 带分支标记 (file, old_pk) → new_pk
   分支B 内引用旧 99 的外键同步更新为新编号，不误伤分支A 的 99
```

**两种模式（`conflict_mode`）**：

| 模式 | 行为 | 适用 |
|---|---|---|
| `split`（默认） | 视为两条独立新行，先到先得，后到者重映射主键 | 两个不同实体撞号 |
| `conflict` | 视为同一行冲突，保留合并行标 `_pk_conflict`，交人工裁决 | 确是同一行、内容有分歧 |

- 重映射行打 `id_remapped=True` + `original_pk=99`，前端显示重编号徽标 + 导出时主键写批注
- `ref_integrity` 消费 `id_mapping` 同步外键值、检测悬空引用
- **双路径已接**：`/compare`（compare.py 内部 mode=split）+ `/merge`（merge_engine 可切 conflict_mode）

---

## 4.7 公式缓存保护（核心正确性机制）

**痛点根源**：openpyxl save 会清空公式缓存值；游戏编表工具链用 xlrd 读公式格返回缓存值，无缓存则输出 0/空。merge 导出必须保护公式缓存。

**三层机制**：

### ① 比对阶段：公式列智能识别（`compare.py`）

类 Git 三方合并，公式列单独处理：

| 场景 | diff_type | 处理 |
|---|---|---|
| 公式文本一致，引用输入值变了 | `formula` | 不判冲突，标 `formula_changed` + 预览重算值 |
| 公式文本各版本不同 | `formula_conflict` | 作为冲突，前端选公式版本 |
| 重算结果与列类型不符 | — | `formula_notice` 附提示 |

- `_eval_row_formula` 内置轻量求值器：SUM/AVERAGE/MAX/MIN/COUNT + 行内算术（`=B7+C7+D7`），安全求值（禁 builtins）
- 跨行/跨表复杂公式求值不了 → 返回 None 交 LibreOffice 兜底

### ② 导出阶段：快照→校验→重算→二次校验（`diff.py:_save_with_formula_cache`）

```
save 前快照所有公式格缓存值
  → wb.save
  → save 后回读对比，检测缓存丢失/变更
  → 丢失则 libreoffice headless 重算写回
  → 二次校验仍不一致 → 阻断提交（needs_manual_fix=True）
```

非公式表 fast-path 零开销跳过；`precommit_hold` 产出 `formula_loss` hold 事件，`recommendation: recalc | manual_fix`。

### ③ 大表阈值跳过（`parser.py`）

```
_FORMULA_SKIP_THRESHOLD = 500KB
  大表（≥500KB）跳过公式/批注读取（openpyxl 解析 10w 行 XML 需 6s/文件）
  → 走无公式 fallback（公式列当普通值列处理，行为正确）
  小表（<500KB）正常读公式保语义
```

- 读值用 calamine（Rust，100x 提速）；公式/批注用 `read_formulas_and_comments` 单遍合并读
- 公式引用位移：`_rewrite_formula_row`（inserted 行 append 后重写行引用）+ `_shift_existing_formula_refs`（删/插行后位移既有引用）

---

## 4.8 高级合并特性

- **同作者自动合并**（`commit_authors`）：同一作者多提交取最新版本，跨作者才判冲突
- **批注三方 diff**（M7）：各版本批注不同 → `comment_conflict`
- **表头结构差异**（M5）：列增删/列类型变化检测
- **漏行检测**（M3）：全量覆盖分支合入时，base 行被遗漏 → P0 告警
- **列血缘联动**（`column_lineage`）：trunk 加列预检各分支缺列清单

---

# 5. 性能优化

---

## 5.1 Agent 侧性能优化（一）

| 优化 | 模块 | 收益 |
|---|---|---|
| Token 感知上下文 | `llm_context.py` | 12000 上限 + 2000 预留，聚焦表完整结构 + 其余简要摘要，超限自动降级，避免 60+ 表全量注入 token 爆炸 |
| 意图分类规则短路 | `graph.py` | 规则命中不走 LLM，省一次 LLM 往返 |
| LLM 会话复用 | `llm.py` | 同实例跨 invoke 复用 codemaker session，避免每次分类重建 |
| 零-LLM fallback | `splitter_baseline` | 跨表拆分优先 11 模板 + ColumnExtractor，仅复杂场景才调 LLM |

---

## 5.2 Agent 侧性能优化（二）

| 优化 | 模块 | 收益 |
|---|---|---|
| 子 Agent 并行 | `dispatcher.py` | `asyncio.gather` 并行，N 表串行→并行 |
| LLM 熔断器 | `step_ai_enhancer` | 连续失败达阈值熔断，降级跳过，防雪崩重试 |
| 分层校验漏斗 | L0 规则闸 | L0 零 LLM 覆盖 ~80% 关切，仅疑点走 L1 AI 判官 |
| read_sheet 全量缓存 | `cli_interface.py` | (path,sheet) 键缓存，写操作收敛失效 |
| Step1 分段缓存 | `parse_agent.py` | 消除 Step1 重复调 split_multi_intent |

---

## 5.3 Agent 侧性能优化（三）

| 优化 | 模块 | 收益 |
|---|---|---|
| Schema Bundle 共享 | `schema_bundle.py` | 多 SubAgent 共享 schema 上下文，避免重复扫描 |
| BM25 索引单例 | `rag_searcher.py` | 按 path 缓存 BM25 索引 |
| 表索引懒加载 | `cli_interface.py` | `_table_index.json` 懒加载 + mtime 失效 |
| 公式快照缓存 | `formula_cache_validator.py` | (path,mtime,size) 键，同文件免二次 load |
| 枚举单例缓存 | `enum_resolver.py` | 内存映射 + reset 刷新 |

---

## 5.4 Agent 侧：LLM 调用观测

`llm_counter.py` 提供 per-run 成本观测，支撑优化决策：

```
total_calls / total_tokens / by_site 分站点统计
success_path_calls vs failure_path_calls 分流
```

- 线程本地计数（`threading.local`），避免多线程 eval 竞态
- 生产路径不调 snapshot 则零 IO 零副作用
- 支撑"ai_confirm_table 冗余削减"等优化的次数证据

> 实测：样例6 从 153.6s 优化；chilong 基线 failures 9→7→1；墙钟从超时向 180s 目标收敛

---

## 5.5 Merge 侧性能优化（一）

### 快路径：绕过 openpyxl（`fast_apply.py`）

```
10w 行表 openpyxl 全量 load+save ≈ 13s（+公式快照 12s）
          ↓ 纯数据大表直接 zip+XML 直改
仅改十几格 → 只改 sheet XML，秒级
```

- lxml（C 实现）序列化 25MB sheet XML：**3.4s → ~1s**
- 小表（<512KB）仍走 openpyxl，避免回归风险
- 编辑语义与常规路径完全对齐（matched/inserted/deleted 一致）

---

## 5.6 Merge 侧性能优化（二）

| 优化 | 模块 | 收益 |
|---|---|---|
| numpy 广播向量化 | `compare._compare_vectorized` | 替换三层 Python 循环 |
| 列号 lru_cache | `compare._col_letter` | 10w 行主循环免重复计算 |
| matched 行免拷贝 | `id_resolver` (M7-2) | 省 120w 次 dict 分配 |
| ProcessPool 真并行 | `parallel_compare` | 表数≥4 跨进程并行（GIL 下 ThreadPool 仅 0.96x） |
| 目录缓存预热 | `merge_branch` | 冷首次 rglob 11.8s → 秒级 |
| AI 建议预取 | 后台线程池 | 只预取冲突最多的前 5 sheet |

---

## 5.7 Merge 侧：并行与容错

`parallel_compare.py` 的并行策略：

```
表数 < 4  → ThreadPool（IO 段释放 GIL）
表数 ≥ 4  → ProcessPoolExecutor（compare_sheet 真并行）
             ├─ 单表失败：记日志继续，不拖垮整批
             ├─ 超时（60s/表）：自动回退 ThreadPool
             └─ pickling/import 失败：自动回退 ThreadPool
```

- Windows spawn 下重依赖（LLM client / file_watcher）隔离，避免子进程 import 死锁
- 阈值 / worker 数 / 超时均可环境变量覆盖

---

## 5.8 Agent 大表快路径实测（10k / 50k / 100k）

纯数据大表（无公式/批注/合并单元格）全链路快路径，实测耗时：

| 操作 | 快路径实现 | 10k | 50k | 100k |
|---|---|---|---|---|
| read_sheet | calamine 整表读 | 269ms | 1.5s | 2.6s |
| search（3846 命中） | calamine 内存扫 | 1.5s | 1.4s | 2.7s |
| locate_row | calamine 内存扫 | 169ms | 812ms | 1.7s |
| write_cell | zip+XML 直改 | 652ms | 3.0s | 6.0s |
| append_row | zip+XML 尾追加 | 643ms | 3.1s | 6.5s |
| delete_row | zip+XML 删行+重排行号 | 1.5s | 7.8s | 16.1s |
| insert_row | zip+XML 插行+重排行号 | 1.6s | 8.1s | 13.9s |

**对照**：优化前读 10k 即 O(N²) 卡死 >60s、写 100k openpyxl 全量 load+save ~35s。
读路径约 **100 倍提速**，写路径约 **5 倍提速**。

**关键修复点**：
- `ws.max_column` 是 openpyxl 动态 property，循环内反复求值退化为 O(N²) → 提出循环外缓存
- `_is_type_cell` 原仅认 ASCII 标识符，把 `灵兽id:int` 这类中文前缀类型行误判为数据行 → 放宽为中文标识符
- `fast_apply._replace_cell` 缺行号（`r="B"` 应为 `r="B3"`）→ 修复

**安全兜底**：含公式/批注/合并单元格的表一律回退 openpyxl 原路径，快路径失败自动回退，语义不变。

---

## 5.9 索引实时更新机制（一致性保障）

索引（`_table_index.json`：78 表、79MB）不是静态快照，三层保障实时一致：

```
① 写后增量刷新（主路径）
   TableAgent._refresh_index_after_write：每次写操作完成后
   → refresh_if_changed 按 MD5 比对，只重扫变更文件（非全量）
   → 清空 skill_context / matcher / index 缓存，避免旧缓存脏读

② 文件监听（外部修改）
   TableFileWatcher（watchdog）：外部手改 Excel → 防抖 1s 自动增量刷新

③ 启动自愈
   agent_service 启动时：索引缺失/损坏 → 全量重建；主进程再增量刷新
```

- **dry_run（预览）不刷新**：沙箱在临时副本操作，`live_index=False` 跳过刷新，避免污染全局索引（设计使然）
- 索引原子写（先写 .tmp 再 `os.replace`），并发读不会读到半截 JSON
- 写入后下一轮"查询测试兽"即可命中倒排索引（row_index）

---

# 6. 测试与质量保障

---

## 6.1 测试体系

| 类型 | 规模 | 说明 |
|---|---|---|
| 单元测试 | 103 文件 / 1114 函数 | 引擎 + 解析 + 匹配 + 合并纯逻辑 |
| Eval 基线 | LLM-as-judge | 自然语言全链路准确率评测 |
| 压测资产 | `resources/perf/` | 10k/50k/100k 生成器 + bench 脚本 |
| 快照闭环 | workflow snapshot | 写盘→读回→restore 持久化验证 |

- **可重复性**：写盘用例在共享快照基线上，每条前 restore，跑完恢复
- **缓存隔离**：`conftest.py` autouse 清空 skill 缓存，避免跨测试假阳性

---

## 6.2 Spec 驱动开发

`openspec/` 目录承载变更提案与规格：

```
changes/<name>/proposal.md + design.md + tasks.md + specs/*.md
archive/ 归档历史变更（eval-infra / merge-stall-overhaul / anti-pattern-induction）
```

- 每个 P0 问题先写 design（含设计决策记录），再落地 + 补测试
- 变更归档可追溯，避免"改了忘了为什么"

---

# 7. 核心创新点

---

## 7.1 核心创新点汇总

1. **语义相等归一 `_semantic_key`**：一套归一化尺贯穿 diff 判等 + 冲突推荐，消灭类型/表示差异假冲突
2. **列策略驱动自动合并**：YAML 声明式策略 + 类型校验兜底，可热更新（mtime 感知）
3. **ID 冲突重映射 + 分支标记映射**：`(file,old_pk)→new_pk` 精确追踪，避免跨分支主键误命中
4. **SVN copyfrom 反查真实 merge-base**：替代手工 fork 快照，合并基础自动定位
5. **zip+XML 快路径**：大数据表合并绕过 openpyxl，编辑语义与常规路径完全对齐
6. **同作者自动合并**：利用提交作者信息减少非必要冲突

---

## 7.2 数据面与工程创新

- **跨表 ID 段校验 + 查重**（`id_scope`）：消费 `id_mgr.xlsx` 段定义，扫全目录查跨表 ID 冲突（P0）
- **列血缘图**（`column_lineage`）：schema 变更预检各分支，`sync_preview` 输出缺列清单
- **占位符拓扑回填**（`produces_inference`）：`<name>` 占位符按依赖拓扑序回填真实 ID
- **枚举自动发现**（`analyze_enum_columns`）：AI 扫 int 列自动推断中文枚举映射
- **公式引用机械位移**：删/插行后自动重写公式引用
- **反模式自我归纳闭环**：从失败案例 AI 归纳 anti_patterns，持续提升准确率
- **LLM 熔断器 + 三态 ok + 断点续跑**：AI 系统的降级、可恢复、不误判设计

---

# 8. 总结与展望

---

## 8.1 总结

- **Excel-Agent**：LangGraph 主智能体 + 4 层匹配引擎 + 7 步/4 步双管道 + 子 Agent 并行，自然语言 → 精准落库
- **三方合并引擎**：语义归一 + 列策略自动合并 + ID 重映射 + 引用完整性 + 快路径，冲突可视化、自动解决、人工兜底
- **性能**：Agent 侧 Token 控制 + 并行 + 熔断 + 缓存；Merge 侧向量化 + 快路径 + 进程并行，大表 13s → 秒级

**核心价值**：配表效率提升、合并正确性三重保障（语义判等 + 分支标记 + 类型校验）、成本可控。

---

## 8.2 展望

1. **AI 冲突推荐接入**：`recommend_version` 预留 AI 基座接入点，用 LLM 语义推荐替代启发式
2. **L0 规则闸全落地**：正确性校验进 Step4 写后校验 + 回退循环
3. **工程补齐**：CI + 静态检查（ruff/mypy）+ 超大表流式合并
4. **降本**：LLM 调用次数进一步收敛（缓存 + 规则闸 + 更精准上下文）

---

# 9. 问答环节（Q&A）

---

## Q1：为什么不用现成的合并库（如 git merge 三方合并 / pandas diff）？

**答**：三个原因——
1. **业务语义**：Excel 是"主键行 + 列类型 + 公式 + 批注"的富结构，pandas 只对单元格值比对，无法处理公式引用位移、批注三方 diff、列类型校验、ID 段约束
2. **合并策略**：不同列有不同合并规则（ID 保基准、数值取最大、文本取最长），需要 `merge_strategies.yaml` 声明式驱动，通用库无此概念
3. **git 的对象模型**：git 基于行/字节，Excel 是 zip 二进制，直接套 git 会退化成"整文件冲突"，无单元格粒度。我们自己做 `compare_sheet` 才有行/列粒度 diff + 自动合并 + 类型兜底。

---

## Q2：语义相等归一会不会误判？（"100" 和 100 类型不同）

**答**：不会，这是有意为之的设计取舍。
- 归一化**只用于判等（是否冲突）**，不改变原值——判为相等后，合并落盘时保留原始类型值，绝不强制转型
- bool 单独用 `('bool', v)` 归一，避免 `True == 1` 的 Python 陷阱
- 对于**确实需要区分"文本 100"和"数值 100"的场景**，有 `value_constraints.yaml` 列类型标注做二次校验：若列标注 `int` 而候选是字符串，自动合并会因类型不符降级为人工
- 即"语义判等"负责减少假冲突，"类型校验"负责兜底真差异，两层互补

---

## Q3：merge-base 怎么确定的？SVN copyfrom 反查可靠吗？

**答**：
- 通过 `svn log --stop-on-copy` 反查分支创建点的 copyfrom-rev，得到两个分支分叉前的公共祖先版本号，作为 merge-base
- 可靠性：copyfrom 是 SVN 元数据，不受文件名/目录模拟影响，比旧版"手工拷贝 fork 快照"更准确
- 兜底：`_resolve_branch_point` 解析失败时回退到 `_resolve_branch_path` 的路径解析，再失败走显式传入的 merge-base；legacy 三阶段流程仍保留（deprecated 不删除），双轨并行保证兼容

---

## Q4：LLM 上下文 token 为什么重要，怎么保证不超限？

**答**：
- 60+ 表全量结构注入会远超模型上下文窗口（且 token 成本随表数线性增长）
- `llm_context.py` 采用**Token 感知增量加载**：先估算固定部分（header+skills+rules），聚焦表注入完整结构（列名+行数+前 3 行样本），剩余预算给其他表简要摘要（文件名+Sheet 名）
- 三重保险：上限 12000 token + 2000 安全余量 + 超限自动降级为全简要模式；降级仍超则按序省略并标记 `omitted`
- 估算规则：中文 ≈ 2 token/字、ASCII ≈ 0.5 token/字，成本低且满足工程精度

---

## Q5：子 Agent 并发的并发控制、超时、熔断怎么设计的？

**答**：
- **并发**：`asyncio.gather` + `asyncio.to_thread` 并行派发，单 Agent 是同步方法，to_thread 不阻塞 loop
- **超时**：单 Agent 120s（`CODEMAKER_SUBAGENT_TIMEOUT`），超时取消并返回 `ok=False, error="timeout"`
- **隔离**：单 Agent 失败/超时不影响其他 Agent，返回顺序与输入对齐
- **重试**：仅对失败 Agent 定向重试 1 次，保留成功 fragment
- **熔断**（`step_ai_enhancer`）：连续失败达阈值（`CODEMAKER_AI_CIRCUIT_THRESHOLD`）→ 熔断，后续调用直接降级跳过，不再调 LLM，防止后端故障雪崩；`reset_circuit` 恢复

---

## Q6：如何保证 AI 写表不破坏数据？

**答**：五层防线——
1. **写前备份**：`backup_audit.py` 操作前复制原文件到 `server/backups/`，JSONL 审计，支持按索引回滚
2. **dry_run 预览**：先跑 `classify` 预览 diff，用户确认后才写盘
3. **语义闸门 + 置信度**：低置信不自动执行，列匹配/表定位歧义交人工确认
4. **公式缓存保护**：save 前后快照对比公式缓存，丢失则 LibreOffice 重算，仍不一致阻断提交
5. **写后校验**：主键唯一性 + 引用完整性 + 列类型校验，违反即回滚

---

## Q7：zip+XML 快路径的编辑语义怎么和 openpyxl 对齐？风险？

**答**：
- 快路径**只处理纯数据大表**（无公式/批注/合并单元格），满足条件才启用，否则返回 `None` 回退 openpyxl
- 编辑语义严格对齐：matched 行写 `value≠base` 的已解决单元格（col≠0）；inserted/missing_row 按主键升序插整行；deleted 删整行；有插删时整表行号重排保证 `r` 连续
- 文本用 inlineStr 不触碰 sharedStrings，数值/布尔按类型写，样式取自落点参考行
- 风险控制：`_FAST_MIN_SIZE = 512KB` 小表不走快路径；快路径仅在"十几格改动"场景收益，且经过 bench 验证

---

## Q8：ID 冲突重映射的跨分支外键，怎么保证不误命中？

**答**：
- 映射表带**分支标记**：`(file, old_pk) → new_pk`，而非裸 `old_pk → new_pk`
- 分支 B 内引用旧 99 的外键，只按 `(B, 99)` 查表更新，不会命中分支 A 的 99
- `ref_integrity` 消费 `id_mapping` 同步外键值后，再扫全表检测悬空引用（dangling refs），引用不闭环会标记出来
- split/conflict 双模式：多分支同主键但内容不同 → split 拆两条独立行；确为同一行冲突 → conflict 标记人工，不擅自重映射

---

## Q9：公式缓存为什么要保护？openpyxl 直接保存会怎样？

**答**：
- **根因**：openpyxl save 会清空公式缓存值；游戏编表工具链用 xlrd 读公式格返回缓存值，无缓存则输出 0/空——这是"静默数据损坏"，比报错更危险
- **保护链**：save 前快照所有公式格缓存值 → save → save 后回读对比 → 丢失则 libreoffice headless 重算写回 → 二次校验仍不一致才阻断（needs_manual_fix=True）
- **比对侧**：公式列不按值 diff，而是"文本一致 → 引用值变化标 formula_changed 预览重算；文本不一致 → formula_conflict 让用户选版本"，避免把公式列当普通值误判冲突
- **性能兜底**：500KB 阈值——大表（通常纯数据无公式）跳过公式读取走无公式 fallback，省 openpyxl 6s/文件；小表正常读公式保语义

---

## Q10：性能指标怎么测的，可复现吗？

**答**：
- **压测资产**：`gen_perf_tables.py` 幂等生成 10k/50k/100k 行表（perf_ability/perf_item/perf_pet），`bench_perf_tables.py` 逐样例测耗时
- **对比法**：`bench_merge_before_after.py` 用 git stash 对照优化前后，确保"零回归"（改前改后失败数一致）
- **LLM 成本**：`llm_counter.py` per-run 打点调用次数 + token，支撑成本侧对比
- 关键数据：快路径 10w 行 13s→秒级、lxml 3.4s→1s、目录缓存 11.8s→秒级、ProcessPool 表级并行

---

## Q11：1000+ 测试怎么保证稳定、不 flaky？

**答**：
- **缓存隔离**：`conftest.py` autouse fixture 清空 skill 模块级缓存（_route_cache/_columns_cache/_YAML_CACHE），避免跨测试状态泄漏
- **快照基线**：写盘用例在共享快照上跑，每条前 restore，跑完统一恢复，`resources/` 回到测试前状态
- **sys.path 收口**：统一 `server/` 在 path，消除 `agent.X` vs `server.agent.X` 双命名空间导致的 isinstance 断裂
- **零-LLM 优先**：引擎/解析/匹配纯逻辑测试不依赖 LLM，只有 eval 类才需启动 codemaker serve

---

## Q12：反模式归纳闭环会不会把错误规则学进去？

**答**：有护栏——
- 归纳产物进 `L3_anti_patterns/` 与 `_pending/` 隔离，**待定候选不直接生效**
- `skill_updater.promote_with_guard` 带守卫：`CODEMAKER_INDUCE_PROD` 默认关（生产不归纳），`CODEMAKER_SKIP_REGRESSION` 默认开（跳过同步 LLM 回归提速）
- 归纳出的反模式以"软约束"注入验证层，最终仍以 value_constraints / 引用完整性等硬规则为准，软规则不强制拦截
- 演进路径：失败 → 归纳 → pending → 人工 review → promote，不是全自动上线

---

## Q13：为什么不用数据库而用 Excel + 文件？

**答**：
- **业务约束**：策划的交付物就是 Excel（游戏引擎直接读 xlsx 转数据），中间改数据库会增加一道同步成本，且破坏现有工具链（xlrd 读缓存值）
- **工作流**：版本管理走 SVN，合并对象天然是文件，我们的引擎直接对标"文件级三方合并"这一真实痛点
- **折中**：用 `_table_index.json`（表结构）+ `value_constraints.yaml`（列约束）+ `id_mgr.xlsx`（ID 段）补足了数据库才有的"schema + 约束 + 主键段"能力，即"文件承载数据 + 索引承载元数据"

---

## Q14：10w 行合并的复杂度与内存瓶颈在哪？

**答**：
- **复杂度**：`compare_sheet` 主键对齐 O(N)（哈希建 base_pk_map），单元格比对 O(N×C)（N 行 × C 列）
- **向量化**：numpy 广播 `base_keys != key_mats` 一次判全矩阵差异，替换 Python 三层循环，C 层完成
- **内存**：每格预计算 `_semantic_key` 元组矩阵（object dtype），无差异行走 sparse 只存主键单元格，避免物化 10w×N 列全量
- **免拷贝**：matched 行共享 cells 引用不深拷贝（省 120w 次 dict 分配），仅 inserted 行需深拷贝
- **大表**：10w+ 行走 `fast_apply` zip+XML 直改，跳过 openpyxl 全量 load

---

## Q15：如果 LLM 挂了，系统还能用吗？

**答**：能，多级降级——
1. **意图分类**：规则短路优先，简单 CRUD 不需要 LLM
2. **零-LLM fallback**：Splitter baseline（11 模板）+ ColumnExtractor 处理跨表拆分
3. **Merge 全链路零 LLM**：`compare_sheet` / `id_resolver` / `ref_integrity` / `fast_apply` 均不依赖 LLM，AI 建议只是可选增强
4. **LLM 熔断**：连续失败自动熔断，走降级路径不阻塞主流程
5. **兜底提示**：`README` 明确"codemaker serve 不可用报『AI 服务未启动』，模型额度耗尽报『底层模型调用失败』"

---

## Q16：这个系统的边界/局限是什么？

**答**（坦诚边界）：
1. **公式语义**：仅支持行内聚合公式重算，跨行/跨表复杂公式依赖 LibreOffice 兜底，未做语义级公式重写
2. **快路径适用性**：仅纯数据大表，含合并单元格/复杂样式/批注的表回退 openpyxl
3. **AI 准确率**：自然语言极端歧义场景仍需人工确认，非全自动
4. **工程成熟度**：暂缺 CI / 静态检查，超大表（百万行）流式合并未覆盖
5. **单机架构**：无分布式/多租户，面向团队内部工具定位

---

## 谢谢

**Q & A**

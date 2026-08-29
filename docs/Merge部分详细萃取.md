# Merge 部分详细萃取文档（新手可读版）

> 本文档面向"刚接手项目的技术人员"，目标：读完能理解 Merge 每条链路在干什么、为什么这么设计、性能是怎么一步步优化出来的。
> 每个技术点都按「**是什么 → 为什么 → 怎么实现 → 关键数据**」四段讲，避免只列 file:line 不解释。

---

## 数据引用总则（先读）

本文档所有性能数字均来自仓库 / 桌面历史版本目录里的**归档评测报告**，每条括注证据文件相对路径（相对 `C:\Users\wuzhixian\Desktop\`）。两条铁律：

1. **同集 before/after 才可比**：merge 有"快照版（snapshot）"与"svn 版（real svn）"两套数据源，规模与 merge-base 反查机制不同，**不允许跨版本横比**。
   - 快照版：`test/` 07-30 压测 + `project/` 07-28 全链基线，merge-base 靠 `_meta.json` 反查，表规模 3000 行 / 818 行。
   - svn 版：`pre/` 08-11 R3 路由层基准 + R6 引擎层，merge-base 靠真实 `svn log --stop-on-copy`，74 表含 monster 10w 行。
2. **数字只引证据，不重测不臆造**：凡无法验证处标 `[未验证]` 或注明"设计目标"。

主要证据文件（全文引用缩写）：
- `pre/server/tests/reports/archive/merge_router_bench_20260811_110900_R3.md` —— 简称 **【R3 基准】**
- `pre/server/tests/reports/archive/merge_eval_20260810_120722_nogit_before .md` + `..._120814_nogit_after .md` —— 简称 **【R6 大表 before/after】**（注意文件名带尾随空格）
- `pre/server/tests/reports/archive/merge_eval_20260810_115535_nogit.md` + `..._161007_nogit.md` —— 简称 **【R6 小种子】**
- `test/server/tests/reports/stress_merge_20260730_181128.{txt,json}` —— 简称 **【M-snap 压测】**
- `project/server/tests/_bench_results/baseline_large.json` —— 简称 **【L-0728 全链基线】**
- 完整索引见 `Desktop\性能优化指标整合文档.md` §五。

---

# 第 0 章：先搞懂三个基础概念

读正文前，先理解三个贯穿全文的词。

## 0.1 什么是"三方合并"

想象两个策划同时改同一张 Excel：

```
            原始表（base，公共起点）
           /                    \
   策划A 改了几格          策划B 改了几格
    （source）              （target）
           \                    /
              合并结果（要把两边的改动合到一起）
```

- **base**（基准版本）：两人开始改之前的公共版本（起点）
- **source**（源分支）：其中一方的版本
- **target / ours**（目标分支）：另一方的版本（target 和 ours 是同一份数据的两个叫法，一个强调"合入目标"，一个强调"我方"）
- **合并**：找出"A 改了哪些格、B 改了哪些格"，把两边的改动合成一份

**为什么需要 base**：没有 base，看到 A 和 B 都是 `100`，不知道是"两人都改成 100"（一致，没事）还是"本来 200，A 改成 100，B 也改成 100"（一致）还是"本来 100，都没改"（一致）。有 base 才能判断"谁动了、动成什么样"。

## 0.2 什么是"merge-base"（公共祖先）

就是上面的 base——**两个分支共同的历史起点**。

难点在于：怎么自动找到这个起点？答案靠 SVN 的 `copyfrom` 记录（见第三章）。

## 0.3 什么是"copyfrom"

SVN 里切分支的本质是 `svn copy 原路径 新路径`。这条命令会在 SVN 历史里永久记录：

> "branches/dev1 这个目录，是从 trunk 路径的 r1 版本复制来的"

这条记录叫 **copyfrom**（copyfrom-path = 源路径，copyfrom-rev = 源版本号）。

**关键洞察**：只要顺着 copyfrom 记录往上追，就能追出"分支 → 分支的父级 → 祖父级 → trunk"，追到两个分支都指向同一个路径时，那个路径的更早版本就是公共祖先。

---

# 第 1 章：Merge 过程总体框架

## 1.1 一句话概括

**SVN 定位公共祖先（merge-base，即三方合并里的"基准版本"，两个分支的共同历史起点）→ 拉取三份数据（base/source/target，基准/源分支/目标分支）到本地 → 主键对齐逐格语义判等 → 冲突自动/人工解决 → 引用校验 → 写回。**

## 1.2 完整流程（7 步，每步一句话说明）

```
① 选分支       用户选 trunk（主干）+ 某个 dev 分支（开发分支），或 trunk 下某个子目录（目录合并）
② 定位 merge-base   SVN copyfrom（分支复制来源记录）反查公共祖先（见第 3 章）
③ 拉取三份数据      base 用 svn export（按历史版本导出）导出；source/target 从工作副本直接拷
④ compare 比对      把三份数据按主键对齐，逐格判断"改了/冲突/漏了"
⑤ 冲突解决          无冲突自动合；有冲突给人裁决（含 AI 建议）
⑥ 引用校验          外键跟着主键重映射一起改，改完检测"引用悬空"
⑦ 写回              大表走快路径，小表走 openpyxl（带公式缓存保护）
```

## 1.3 两条合并链路

| 链路 | 干什么 | 入口 |
|---|---|---|
| **跨分支合并** | 把 dev 分支（开发分支）的改动合进另一个分支/trunk | `/api/merge/branch/compare` + `/apply`（比对接口 + 落盘接口） |
| **目录合并** | 把 trunk 下某个子目录（subdev_x，子开发目录）合回父级 | `/api/merge/subdir/compare` + `/apply` |
| （旧）三阶段 | 多个生产者分多次提交，分三阶段汇总 | `merge_stages.py`（已 deprecated 弃用，保留兼容） |

**重要**：两条链路**共享同一套比对算法**（`compare_sheet`，比对工作表函数）和大量 SVN 辅助函数，只是"选分支、定 base"的方式不同，核心 diff（差异）逻辑不重复实现。

## 1.4 跨分支的两种方向（direction）

| 方向 | 中文含义 | 落点 |
|---|---|---|
| `absorb`（吸收） | 把 source（源分支）的改动吸进 target（目标分支） | 落 target，可产生新版本目录 `{name}_r{N+1}` |
| `merge_back`（合回主干） | 把分支合回 trunk | 落 trunk，版本化命名 `{group}_{N+1}.xlsx` |

---

# 第 2 章：使用环境准备（SVN 建立）

## 2.1 为什么需要 SVN

三方合并需要一个"有历史记录"的仓库来存 base。SVN 天生记录 `copyfrom`，所以用它来模拟真实的并行开发场景。

> **版本演进说明**：merge 引擎经历过两代数据源——早期"快照版"（`merge/demo/` 文件夹 + `_meta.json` 反查 merge-base，无需 svn 工具）与现今"svn 版"（真实 `svnadmin create` 仓库 + `svn log --stop-on-copy` 反查）。当前仓库 `merge_branch.py:11-13` 注释明确"不再依赖文件夹模拟快照（该方式已下线）"。本章只讲现行 svn 版；快照版性能数据见【M-snap 压测】/【L-0728 全链基线】，仅作演进基线，不与 svn 版横比。

## 2.2 种子数据（随项目版本控制，约 41MB）

```
merge/_seed_data/
├── trunk/            # 全量基准 74 张表（含 monster 10w 行大表，用来测性能）
├── dev1/             # 分支 1（与 trunk 有差异的改表约 8 张）
├── dev2/             # 分支 2
└── trunk/subdev_1/   # 目录合并 source（5 张私有表）
```

## 2.3 一键搭建 SVN 环境

```bash
uv run python merge/scripts/setup_svn_demo.py --clean
```

这条命令串起 6 个脚本：

| 顺序 | 脚本 | 干什么 |
|---|---|---|
| 1 | `build_svn_real.py` | 建仓库 + 导入 trunk + 切出 dev1/dev2 + checkout（产生 r1-r8 历史） |
| 2 | `build_svn_small_branches.py` | 再切 dev3/dev4/subdev_2/3 小分支 + 埋冲突锚点 |
| 3-6 | `seed_svn_conflicts*.py` | 分批写入冲突数据 |

## 2.4 生成的结构

```
merge/svn/demo_svn/
├── repo/                      # SVN 仓库（file:// 直连，不用起 svnserve 服务）
└── wc/                        # 工作副本（checkout 出来的本地文件）
    ├── trunk/                 # 74 张表 + 3 个子目录
    │   ├── subdev_1/          # ability/const/item_drop/monster/reward
    │   ├── subdev_2/          # const/fabao/guild/mail/tips
    │   └── subdev_3/          # ability/reward/map/interaction/world_buff
    └── branches/
        ├── dev1/  dev2/       # 全量分支（测跨分支合并）
        └── dev3/  dev4/       # 小表分支（测合并速度，排除大表干扰）
```

## 2.5 设计意图（为什么要这么搭）

1. **dev1/dev2 全量** → 测跨分支合并的完整功能
2. **dev3/dev4 只留小表** → 单独测合并耗时，排除 monster 10w 行的 O(n²) 卡顿
3. **冲突锚点**：dev1-4 的 `tips.xlsx` 第一个 sheet 的 B2 格各写了不同标记 → **任意两个分支合并，必然在这个格子上产生冲突**。这样每次测试都有确定性的冲突案例可验。

---

# 第 3 章：merge-base 是怎么定位的（重点）

## 3.1 为什么不直接用 `svn mergeinfo`

`svn mergeinfo`（SVN 的"合并信息"查询命令，查"哪些变更还没合并"）**依赖团队规范地执行过 merge 命令**。实际工作里大家常常手动拷文件、不规范 merge，mergeinfo 就不可靠。

**copyfrom 反查**（反查分支从哪复制的客观记录）只看"分支从哪复制的"这条客观记录——**切分支必然留下 copyfrom**，所以它可靠。

## 3.2 第一步：查单个分支的"出生点"（`_resolve_branch_point`，解析分支出生点函数）

对一个分支目录执行：

```bash
svn log -v --stop-on-copy --xml <分支路径>
```

- `--stop-on-copy`：遇到"这个目录被复制创建"的那条记录就停，不再往上追
- `-v`：输出路径变更详情（带 `copyfrom-path` 复制来源路径 和 `copyfrom-rev` 复制来源版本号）

结果分三种：

| 情况 | copyfrom-path（复制来源路径） | copyfrom-rev（复制来源版本号） | 标记 |
|---|---|---|---|
| 正常切分支（有复制记录） | 如 `/trunk` | 如 r1 | `inferred=false`（非推断，真记录） |
| 纯新建（手工 mkdir+add，没复制） | 路径自身 | 首次提交 rev-1 | `inferred=true`（推断的） |
| 第一次提交就是 r1 或 svn 挂了 | 无 | 无 | `ok:false`（查询失败） |

**inferred 是什么**：inferred（推断标记）——纯新建的目录没有真实 fork 点（分叉点），rev 是"猜"的（首次提交减 1）。代码用 `inferred=true` 明确标注"这是推断，不是真 fork"，后续计算会跳过它。

## 3.3 第二步：沿 copyfrom 链往上追（`_copyfrom_chain_svn`，copyfrom 祖先链追溯函数）

分支可能不是从 trunk 直接 fork（分叉），而是"从分支的分支"fork：

```
trunk@r1 → 分支A@r5 → 分支B（B 从 A 复制）
```

所以要**顺着 copyfrom 一层层往上追**，得到完整祖先链：

```python
cur = 当前分支
while 还有父级且不是纯新建:
    记录 {当前分支, copyfrom_path, copyfrom_rev}
    cur = 父级路径          # 继续往上追
```

例子：B 从 A@r5 复制，A 从 trunk@r1 复制 → 链 = `[{B→A@r5}, {A→trunk@r1}]`（追到 trunk 是纯新建就停）。

## 3.4 第三步：两条链交叉求 LCA（`_lca_svn`，最近公共祖先求值函数）

LCA（Lowest Common Ancestor，最近公共祖先）——两个分支各自有祖先链，找**两条链第一次指向同一个路径**的那个点：

```
源分支 A 的链：A → copyfrom trunk@r1
目标分支 B 的链：B → copyfrom trunk@r2

两边都指向 trunk → 交叉点 = trunk
取更早的版本 min(r1, r2) = r1
所以 base = trunk@r1
```

**为什么取更早版本**：r1 时刻的内容是两个分支**都包含**的公共部分；r2 时刻 A 已经分出去了，不公共。

## 3.5 降级链（自动找不到时的逐级兜底）

```
① 双方 LCA 交叉                    ← 首选，最准
② 子目录场景：一侧是另一侧的子目录
   → base = 父目录 @ 子目录的 copyfrom_rev
③ 单侧 inferred（merge_back 时 trunk 是纯新建）
   → 沿分支那一侧的链找 fork 点
④ 单侧 fork 兜底（target 优先）
⑤ 全失败 → 报错，要求用户手工传 merge_base_override（手工指定基准文件的参数）
```

**关键原则**：找不到就明确报错让用户指定，**绝不瞎猜一个 base**——因为 base 错了，整个合并结果都是错的。

---

# 第 4 章：比对算法（compare）详解 + 性能迭代

## 4.1 compare_sheet 主流程（6 步）

`compare.py:483`，输入三份数据的二维数组，输出"每行每格的状态"：

```
① 提取数据：从三份文件里抽出当前 sheet 的二维数组 + 批注 + 公式
② 表头 diff：各文件表头列名集合对比，标"增列/删列/重排"（只告警不改对齐）
③ 主键对齐：按第一列主键建 key_map，把三份数据的行对上
④ 行分类：每行判定 matched / inserted / deleted / missing_row
⑤ 逐格比对：每格判定"没变 / 单向改 / 真冲突"
⑥ ID 重映射：多分支新增了同主键的行 → 拆行重映射
```

## 4.2 行分类（4 种）

| 类型 | 中文含义 | 例子 |
|---|---|---|
| `matched`（匹配行） | 基准有、衍生也有 | 正常行 |
| `inserted`（新增行） | 基准没有、衍生新增 | 新加了一行 |
| `deleted`（删除行） | 基准有、衍生全删了 | 删了一行 |
| `missing_row`（漏行） | 基准有、但某个全量覆盖分支整行漏拷 | **P0 漏行，要报警** |

`missing_row`（漏行）和 `deleted`（删除）的区别很关键：`deleted` 是"明确删了"（有意的），`missing_row` 是"某个分支压根没拷这行"（无意的）——后者是事故（数据静默消失），必须告警。

## 4.3 单元格三级分类

| 类型 | 中文含义 |
|---|---|
| 没变 | 所有衍生值和基准一致 |
| `changed`（单向修改） | 只有一个衍生值不同，可自动采纳 |
| `conflict`（真冲突） | 多个衍生值互不相同，需人工 |

判定核心是**语义相等归一**（`_semantic_key` 语义归一函数）：把值先归一化再比，让 `"100"` 和 `100` 判为相等，消除"类型/格式差异造成的假冲突"。**只用于判等，不改原值**。

## 4.4 比对算法的 5 次迭代（性能主线）

这是最体现"工程能力"的部分——每一版都精准换掉上一版的唯一瓶颈。

| 版本 | 方法 | 换掉的瓶颈 | 效果 |
|---|---|---|---|
| A | 原始三重循环 | 基线 | 10w行×100列≈500亿次 → 卡死 |
| B | `id()` 哈希预建行索引（用行对象的内存地址做键，快速查"某行在哪个文件"） | 找行用 `list.index` O(n) 线性扫 | 找行 O(n)→O(1) |
| C | sparse 稀疏化（没差异的行只留主键，不存全部格子） | 无差异行也物化 100 个格子的 dict | 内存 GB→MB |
| D | numpy 向量化（一次算好整张表的归一化值，再用底层 C 代码整块比较） | 逐格调 Python 函数 `_semantic_key`（语义归一函数） | 纯数据大表 10-50× |
| E | 公式/批注回退逐格循环 | 向量化不懂公式/批注 | 正确性兜底 |

### 版本 A：三重循环（基线）

朴素做法：行 × 列 × 文件三个 `for` 嵌套，逐格两两比较。10w 行 × 100 列 × 多文件 ≈ 500 亿次 Python 函数调用，直接卡死。

### 版本 B：哈希索引（解决"找行"慢）

问题：要找到"衍生文件里主键匹配的那一行"，用了 `list.index()`（Python 列表的线性查找方法，从头一个个找，找到为止）线性扫，每找一行 O(n)（n 行就要扫 n 次）。

解决：预先建 `{id(行) → 行号}` 哈希表（`id()` 是 Python 取对象内存地址的内置函数，用它当行的唯一标记快速定位），之后 O(1)（常数时间，不管多少行都是一步找到）定位。

### 版本 C：sparse 稀疏化（解决"内存"爆）

问题：B 版解决了速度，但每行无论有没有差异，都物化成完整字典（10w×100 个 dict 对象），内存和 JSON 都是 GB 级。

解决：**没差异的行只存一个主键格**，不展开 100 个格子。

```python
if sparse and row_type == 'matched' and not row_has_diff:
    cells = [pk_cell]    # 只有主键，而不是 100 个 cell
```

正确性三保证（砍掉的信息从别处补）：
1. 导出走基准克隆（没差异的行直接复用基准原样，原封不动照抄基准版本）
2. `id_resolver`（ID 重映射器）只需要主键格——因为重映射只关心"这行的主键是多少、要不要换新 ID"，不关心其他 99 列的内容
3. `ref_integrity`（引用完整性检查器）对未变动行由基准兜底——因为没差异的行外键值就等于基准里的外键值，不需要在 sparse 结果里重复存，检查时回基准文件查即可

### 版本 D：numpy 向量化（解决"函数调用"慢）

问题：C 版省了内存，但每格还要调一次 `_semantic_key`（语义归一函数，把单元格值统一成"类型+数值"元组再比较，消除 `"100"` vs `100` 这类假差异）归一化 + 逐格比较，1000 万次 Python 函数调用。

解决：把"逐格算 → 逐格比"改成"**批量算 → 广播比**"：

```python
# 1. 预先算好每个文件的语义键矩阵（每格只归一化一次）
#    key_mats：语义键矩阵，shape (n_rows, n_cols)，每格存归一化后的 (类型, 值) 元组
key_mats[fname] = kmat

# 2. base 与所有衍生矩阵做一次 numpy 广播比较
#    base_keys != key_mats[fname]：整张表一次判"哪些格不一样"，
#    底层 C 代码做逐格元组比较，不再经过 Python 解释器
has_diff = np.zeros((n_rows, n_cols), dtype=bool)   # 差异标记矩阵，初值全 False
for fname in other_files:
    has_diff |= (base_keys != key_mats[fname])   # 整个矩阵一次广播


为什么快：归一化从"每次比较重算两次"→ 每格只算一次；比较从 Python 三层循环 → numpy C 层广播。纯数据大表 **10-50 倍**。

> **量化佐证（来自【L-0728 全链基线】）**：07-28 `baseline_large.json` 里 `merge_multibranch` 对 3 分支 818 行仅 `compare_time 16.3s`，但 10 万行单表 compare 曾预计"数分钟"（`note_10w_timeout`）。M7 记录 `list.index()` O(n²) 让 10w 行直接卡死（`jj/合并引导问题排查报告.md` M7 项）。A→D 的递进把"10w 行卡死/数分钟"压到 R6 实测 **compare ~3.9s**（见第 8 章总表）。

### 版本 E：公式/批注回退（正确性兜底）

问题：numpy 向量化只处理"值"，但公式单元格的判定（公式文本是否一致、引用值是否变化、批注三方 diff）是结构化逻辑，向量化做不了。

解决：按 sheet 内容分流：

```python
if not formulas_active and not comments_active and not detect_missing:
    vec = _compare_sheet_vectorized(...)   # 纯数据 → 向量化快路径
    if vec is not None:
        return vec
# 含公式/批注 → 回退逐格循环，走完整三方判定
```

**主线总结**：A 先能跑 → B 提速找行 → C 降内存 → D 提计算 → E 保正确。一条递进链，每版换一个瓶颈。

---

# 第 5 章：ID 重映射 + 引用完整性

## 5.1 问题：为什么会有"ID 撞车"

两个分支各自新增了一行，都用了主键 99：

```
分支 A：新增一行 id=99（A 的灵兽）
分支 B：新增一行 id=99（B 的道具）  ← 和 A 撞了
```

合并时这两行是**两条不同的新行**（不是同一行的冲突），但主键撞了，必须给其中一个换新 ID。

## 5.2 ID 重映射流程（`id_resolver.py`，ID 重映射器）

> **先澄清一个常见误解**：ID 重映射**只针对 `inserted`（新增行）**，不碰 `matched`（已有行）。因为合并前各分支都在**同一个基准（base）**上改，已有的行（base 里有主键）两边改的是同一行——那种"两边改同一行且内容不同"是 compare 阶段的 `conflict`（真冲突），归 compare 处理，**不是 ID 重映射的活**。
> ID 重映射只处理一种情况：**两边各新增了一行，恰好用了同一个新主键**——这是"两个新行撞 ID"，不是"同行内容冲突"。

流程：

1. **检测**：只看 `inserted`（新增行）；找出"同一个主键、来自多个不同文件"的行
2. **判断内容是否真不同**：逐文件取整行签名比较，内容完全相同就合并成一条（两边新增的是同一条数据，不冲突）
3. **双模式处理**（仅针对"多分支同主键 + 内容不同"）：

| 模式 | 中文含义 | 适用 |
|---|---|---|
| `split`（拆分，默认） | 内容不同 → 拆成两条独立行，后到的重映射新 ID | 明确是两条不同的新数据（两边各自新加的不同东西，只是 ID 撞了） |
| `conflict`（冲突） | 不拆不重映射，标记 `_pk_conflict` 交人工 | 无法自动区分"两条不同新行"还是"同一条数据被两边改了不同内容"，保守交人工 |

> **为什么 `conflict` 模式存在**：多分支新增同 ID 时，代码无法 100% 确定是"两个独立新行撞 ID"还是"同一条记录在两分支被各自修改"。`split` 默认按"两条独立新行"处理（业务上最常见），`conflict` 留给需要人工判断的场景。

4. **分配新主键**：从 `max(已用主键)+1`（当前已用最大主键再加 1）起自增，跳过已占用；防死循环上限 10 万次；纯数字写 `int`（整数，避免导出成文本）

## 5.3 关键设计：为什么映射表要带"分支标记"

映射表结构：`[{file, old_pk, new_pk, reason}]`——即每条映射记录四个字段：`file`（哪个文件）、`old_pk`（旧主键）、`new_pk`（新主键）、`reason`（换键原因）。注意是 `(file, old_pk) → new_pk`（文件+旧主键 → 新主键），不是裸 `old_pk → new_pk`（只看旧主键）。

**原因**：假设分支 A 和分支 B 都各自新增了 99，重映射后：
- A 的 99 → 保留 99
- B 的 99 → 改成 100

如果映射表只有 `99 → 100`，那么"分支 A 内部某行外键引用了 99"也会被误改成 100——**但 A 的 99 根本没变**！

带 `(file, old_pk)` 就能区分：`(B, 99) → 100` 只改 B 里的引用，A 里的 99 不受影响。

## 5.4 引用完整性（`ref_integrity.py`，引用完整性检查器）

重映射后，引用旧 ID 的外键要跟着改，改完再检测"悬空引用"：

```python
# remap_lookup：重映射查询表 {(文件, 旧主键): 新主键}
remap_lookup = {(m["file"], m["old_pk"]): m["new_pk"] for m in id_mapping}
for fn, v in list(versions.items()):
    new_pk = remap_lookup.get((fn, v_str))   # 命中 → 同步外键值
    if new_pk: versions[fn] = new_pk
# 外键值不在本表主键集、也不在跨表主键集 → dangling（悬空引用）
```

**悬空引用（dangling ref）** = 某行外键指向了一个不存在的 ID。这是合并后最容易埋的雷（引用断裂 → 游戏读不到对应配置，轻则显示异常，重则加载失败）。

## 5.5 跨表 ID 段校验（`id_scope.py`，跨表 ID 段校验器）

除了同表撞车，还有**跨表撞车**：两个不同的 .xlsx 文件用了同一个 ID（比如 fashion 表和 item 表都有 id=100）。

- 段定义读 `resources/id_mgr.xlsx` 的 SETTING sheet（每个模块占一段 ID 区间）
- 扫全目录所有 .xlsx，同一个 id 出现在 ≥2 个不同文件 → 冲突（P0）
- 单遍 `iter_rows` 提取 ID 列建索引（性能优化）

---

# 第 6 章：公式缓存保护（重点，P0 问题）

## 6.1 问题背景：什么是公式缓存，为什么会丢

Excel 的公式单元格里存的是公式文本（如 `=B2+C2`），**计算结果是"缓存"在文件里的**。

- 编表工具链用 xlrd 读（xlrd 是只读 Excel 的老牌 Python 库，读公式时**只返回缓存值**，不会现场计算），xlrd 读公式单元格**返回缓存值**，没缓存就返回空
- 但 openpyxl 没有公式引擎，`save`（保存）时会**清空缓存值**（公式文本保留，结果没了）
- 结果：openpyxl 改表保存 → 缓存清零 → 编表读到空 → 输出 0/空

实测：原始 `match_dan.xlsx` 365 个公式缓存全在，openpyxl save 后归零（0/365）。

> **量化佐证**：R9-A1 公式守门落地后 `needs_manual_fix`（需人工修复标记）audit 覆盖率 100%（on/hold 模式）、off 静默率 100%、31 测试零回归（`优化全过程.md` R9-A1 节）。公式检测快判 zip 扫 `<f>` 标签：10w 行 ~0.05s vs openpyxl 全量读 ~6s（约 120x）；公式只在 ~6.7% 的表出现，无公式表零开销跳过。

## 6.2 校验时机：写回时，不是 compare 时

compare 阶段只读不写，不碰缓存。只有 `save`（写回/导出）才可能清缓存，所以校验挂在**写盘这一步**。

核心函数 `_save_with_formula_cache`（带公式缓存保护的保存函数，`routers/diff.py:598`）：

```python
before = validator.snapshot_before(src_path)   # ① save 前快照缓存值
wb.save(dest_path)                              # ② openpyxl 写盘（可能清缓存）
if not before:                                   # ③ 无公式 → 直接过，零开销
    return {"needs_manual_fix": False, ...}
result = validator.validate_and_fix(dest_path, before)  # ④ 对比 + 必要时重算
```

## 6.3 公式检测快判（省 99% 表的开销）

先用 zip 直接扫 XML 字节，找 `<f>` 标签（openpyxl 存公式的标签）：

- 10w 行扫一遍 ~0.05s，vs openpyxl 全量读 ~6s
- 文本转义 `&lt;f` 不会误判
- 失败回退 openpyxl

**没有公式的表直接跳过整个校验流程**，零开销——因为公式只在极少数表里（约 6.7%）。

## 6.4 快照方式（snapshot_before）

openpyxl 读两遍：
- `data_only=False`（不取计算结果，只取公式文本）：识别哪些格是公式（值以 `=` 开头）
- `data_only=True`（取计算结果，不取公式文本）：读这些公式的**缓存值**

快照按 `(路径, 修改时间, 文件大小)` 缓存，FIFO（先进先出）上限 64 个文件，文件变了自动失效。

## 6.5 校验 + 修复流程（validate_and_fix）

```
① 无公式 → 直接通过（fast-path，快速路径）
② 回读对比快照：
   lost（丢失）：save 前有缓存值 / save 后没了 → 真丢失，需要重算
   changed（变更）：前后值不一致 → 可能是输入源变了，缓存正常更新
③ 只有 changed 且保存后全非空 → 判定"输入源变更导致结果更新，缓存完好"
④ 有 lost → 用 LibreOffice 重算 → 二次校验
⑤ 重算后还是空 → needs_manual_fix=True（标记人工处理）
```

## 6.6 LibreOffice 重算

```bash
soffice --headless --norestore -env:UserInstallation=file:///profile \
        --convert-to xlsx --calc --outdir tmp <文件路径>
# headless 无界面模式 / --calc 触发重算
```

- 60 秒超时
- per-file 互斥锁（防并发写同一文件）
- 输出到 tmp（临时目录）再替换原文件（避免覆盖失败）
- 路径解析：`LIBREOFFICE_PATH` 环境变量 → PATH → Windows 常见安装路径兜底
- 重算后粗略校验批注/样式保留

## 6.7 大表快路径天然绕过

`fast_apply` 用 zip+XML 直改，**只改目标单元格、不动公式单元格**，所以快路径本身不会破坏公式缓存——这是它比 openpyxl 全量 save 的另一个优势。

## 6.8 测试覆盖

`test_formula_cache.py` 6 场景：非公式 / VLOOKUP 重算 / 嵌套 IF / SUM 聚合 / 循环引用 / 无公式 fast-path。

---

# 第 7 章：AI 建议 + 置信度

> 本章覆盖两条建议链路、置信度的**三因子加权计算公式**、并行/缓存/预取技术与作者归并。代码主文件：`server/services/agent_service.py`（建议生成）、`server/routers/merge_branch.py`（预取）、`server/routers/agent.py`（HTTP 入口）、`server/agent/prompts.py`（LLM system prompt）。

## 7.1 先分清两套东西

| 名称 | 是什么 | 谁来做 | 触发时机 |
|---|---|---|---|
| **AI 建议** | 真正调 LLM 给建议 | `suggest_merge` / `suggest_merge_batch` 链路 | 用户点"AI 建议"或 compare 后预取 |
| **⭐推荐** | 规则版多数表决 | `recommend_version`，纯代码零 LLM | compare 结果直接算，无需用户点 |

两者**互补不冲突**：⭐推荐是"秒回的第一眼"，AI 建议是"点开后更聪明的第二眼"。前端并排视图先显示 ⭐推荐，用户不满意才点 AI 建议。

## 7.2 ⭐推荐（规则版，多数表决）

没调 LLM 也能给的快速推荐，逻辑极简但有效：

```python
# 语义键去重："100" 和 100 算同一票（复用 compare 的 _semantic_key）
统计各版本值 → 某值出现 ≥2 次（多数）：
    → 与基准一致 → 推荐保留基准
    → 与基准不同 → 推荐那个多数值
→ 全不同（每个版本都不同）→ 保守回退基准（提示人工评估）
```

**设计动机**：多数表决是三方合并的经典启发式——"多数派即真理"在低频冲突场景正确率足够高，且零 token 零延迟。只有"全不同"（真分叉）才需要 LLM 或人工。

**与 AI 建议的分工**：多数表决能解"两人都改成同一值"的假分叉；LLM 解"值不同、rev 不同、需语义判断"的真冲突。

## 7.3 AI 建议完整链路

```
用户点"AI 建议" → POST /api/agent/suggest-merge
→ asyncio.to_thread 卸到线程池（不阻塞 event loop）
→ AgentService.suggest_merge：
    有 version_meta（SVN 修订信息）→ 先调 LLM
    无 meta 或 LLM 失败 → 回退规则
→ LLM 输入：表名/sheet/列名/行键/基准值 + 各版本（rev=/date= 修订信息）
→ LLM 输出：JSON（suggested_version + reasoning + confidence）
→ 校验 suggested_version 必须是输入衍生版本之一（防 LLM 乱编）
→ 置信度融合（7.4 三因子公式）
```

**关键实现细节**（`agent_service.py:_suggest_merge_via_llm`，LLM 生成建议的核心函数）：

1. **prompt 拼装**（prompt = 提示词，发给 LLM 的指令文本）：`lines.append(f"{fn}|rev={rev}|date={date}{base_mark}: {val!r}")`——每个版本带 SVN 修订信息与 `[基准·仅参考]` 标记。
2. **JSON 抽取**：LLM 输出常带 markdown 围栏（``` 包裹的代码块）或前后杂话，用 `_re.search(r"\{.*\}", text, _re.S)`（正则表达式搜索，匹配 `{...}` 内容）只截 JSON 对象；批量用 `r"\[.*\]"` 截数组。
3. **防幻觉校验**（幻觉 = LLM 编造不存在的内容）：`sv not in versions or sv == base_file → return None`——LLM 若编造版本名或采纳基准，整条丢弃回退规则。
4. **失败即回退**：`llm.invoke`（调用 LLM 的方法）任何异常（超时/额度/解析失败）都 `return None`，调用方落规则，保证**建议永远有返回值**，不因 LLM 故障卡死前端。

## 7.4 置信度实现：三因子加权融合

### 7.4.1 公式

```text
final_confidence = 0.60 × llm_conf        # LLM 自评（主信号）
                 + 0.25 × rev_conf        # 修订号领先度（客观）
                 + 0.15 × value_conf      # 值域合理性（客观）

final = clamp(round(final, 4), 0.0, 1.0)
```

三个权重（`_CONF_W_LLM/_REV/_VALUE`）合计 1.0，LLM 自评占 6 成、客观因子占 4 成。

### 7.4.2 LLM 自评 `llm_conf`（0.3-0.95）

LLM 按 system prompt 的档位表自评（`prompts.py:71-76`）：

| 区间 | 依据 |
|---|---|
| 0.85-0.95 | rev 差距 ≥2 + 值在值域 + 列语义明确 |
| 0.65-0.80 | rev 相邻 / 只有"较新"一条依据 |
| 0.40-0.55 | 值异常 / 语义模糊 |
| 0.30-0.40 | 完全无法判断 |
| 禁止 1.0 | LLM 不许说百分百确定 |

解析失败默认 0.5（`conf = float(obj.get("confidence", 0.5))`）。

### 7.4.3 修订号因子 `rev_conf`（`_rev_confidence`）

被采纳版本相对其他候选的 **rev 领先档数**：

```python
lead = sv_rev - max(other_revs)   # 被采纳版与最新候选的 rev 差
lead >= 3 → 0.95   # 明显刻意修改
lead == 2 → 0.8
lead == 1 → 0.6
lead <= 0 → 0.4    # 采纳了"更旧"版本，负信号拉低
无 rev 信息 → None # 不校正
```

### 7.4.4 值域因子 `value_conf`（`_value_confidence`）

按列名语义判定被采纳值是否"合理"：

```python
ID/编号列：float(val) > 0 → 0.9，否则 0.5
数值列：   有限数值（非 NaN/Inf）→ 0.8，否则 0.4
文本列：   长度 ≥3 → 0.7，非空 <3 → 0.5
无法判断（未知列 / 空值）→ None  # 不校正
```

### 7.4.5 `None` 兜底规则（关键设计）

规则因子为 `None`（无 rev / 无列名语义）时，融合时用 `llm_conf` 替代该因子：

```python
r = llm_conf if rev_conf is None else rev_conf
v = llm_conf if value_conf is None else value_conf
fused = 0.60*llm_conf + 0.25*r + 0.15*v
```

**为什么**：无规则信息时，`0.25*0.5 + 0.15*0.5 = 0.2` 的中性项会把 LLM 自评稀释 20%。用 `None → llm_conf` 兜底后，`fused = (0.60+0.25+0.15)*llm_conf = llm_conf`，**无规则信息时置信度原样等于 LLM 自评**，不无端拉低。

### 7.4.6 数值示例

| 场景 | llm_conf | rev_conf | value_conf | final |
|---|---|---|---|---|
| 高置信 + rev 领先 3 档 + 值合理 | 0.90 | 0.95 | 0.80 | **0.8975** |
| 中置信 + rev 相邻 + 值异常 | 0.70 | 0.60 | 0.40 | **0.645** |
| 无 rev 无列名（纯文本兜底） | 0.60 | None | None | **0.60** |
| LLM 高分但 rev 落后 + 值 NaN | 0.90 | 0.40 | 0.40 | **0.70** |

### 7.4.7 规则回退的置信度（写死常量，不融合）

LLM 失败/无 `version_meta` 时落规则，置信度写死：ID 列 0.75 / 数值列 0.6 / 文本列 0.55 / 兜底 0.3。

**为何不融合**：低置信阈值默认 0.8，`conf < 0.8` 批量采纳时跳过留人工。规则回退最高 0.75 < 0.8，**天然被排除在自动采纳之外**——"规则给的建议只展示、不自动采纳，只有 LLM 高置信建议才自动采纳"。若规则也套融合公式，数值列"值最大"配上高 rev 领先会顶破 0.8，破坏这条安全护栏。

## 7.5 AI 建议参考哪些因子（喂给 LLM 的信号）

单格 prompt 实际拼装（`agent_service.py:3528-3530`）：

```text
表=pet sheet=灵兽 列=攻击力 行键=饕餮
基准值=800
各版本（含SVN修订）：
  dev1|rev=15|date=2026-08-11: 1200
  dev2|rev=3|date=2026-08-10: 900   [基准·仅参考]
建议采纳哪个版本。suggested_version 必须是上面文件名之一。
```

5 个因子：

| # | 因子 | 作用 | 来源 |
|---|---|---|---|
| 1 | SVN rev | rev 大 = 较新 = 刻意修改，倾向采纳 | `version_meta[fn]["rev"]` |
| 2 | SVN date | 辅助判断时间先后 | `version_meta[fn]["date"]` |
| 3 | 列名语义 | ID/编号/数值/枚举白名单 → 判值是否合理 | `col_name` |
| 4 | 单元格值 | 判值异常/接近 | `versions` 各值 |
| 5 | `[基准·仅参考]` 标记 | 强制 LLM 不得采纳 base | `base_file` |

## 7.6 并行技术：批量 LLM + 逐格线程池 + 后台预取

### 7.6.1 三级批量策略（`suggest_merge_batch`）

```text
① 缓存优先：key=(表, sheet, 列名, versions哈希)，命中直接返回（LRU+TTL）
② 批量 LLM（≥2 未缓存格且都有 version_meta）：一次调用覆盖全部未缓存格
③ 回退逐格并行：ThreadPoolExecutor(max_workers=min(8, 格数))
```

**批量 LLM 的核心**（`_suggest_merge_batch_via_llm`）：把 N 个冲突格拼进**一个 prompt**，LLM 一次返回 JSON 数组，按 `index` 归位：

```python
results = [None] * len(items)
for obj in arr:
    idx = int(obj.get("index", -1))
    results[idx] = {...}   # 按输入序号归位，乱序也能正确对应
```

收益：N 次网络往返（每格 15-45s）→ 1 次（约 30-60s）。**单格**不走批量（本来就 1 次调用，批量反而多拼 prompt 开销）。

### 7.6.2 逐格线程池回退

批量失败（LLM 超时/输出无法解析成数组）时，`ex.map(_one, uncached_items)` 并发逐格调 `suggest_merge`。线程池而非进程池——等 LLM 响应是 IO 密集，GIL 不影响；worker 上限 `min(8, 格数)` 防止 LLM 后端被并发打爆。单格内部再失败则落规则（7.4.7），**每一层都有兜底**。

### 7.6.3 后台预取（`_prefetch_ai_suggestions`）

compare 返回后立即后台算建议，用户点开已是热的：

```text
compare 结果 → 收集所有冲突格 → 按冲突数降序排 sheet
→ 取前 5 个 sheet（_PREFETCH_SHEET_LIMIT=5）
→ 后台线程池（max_workers=2）逐个 sheet 调 suggest_merge_batch 填缓存
```

三重成本控制：

1. **仅 AgentService 已初始化才预取**——避免后台线程触发 60+ 表索引重建 + 文件监听器
2. **只预取冲突最多的前 5 sheet**——大合并（74 表）不会耗光 LLM 配额
3. **在途去重 `_prefetch_inflight`**——用户点格与预取撞同一 sheet 时跳过，不重复调 LLM

预取异常**全吞**（`except Exception: pass`），绝不影响 compare 主流程返回。

### 7.6.4 缓存实现（`_suggest_cache`）

```python
OrderedDict 作 LRU：
- TTL 1 小时（_suggest_cache_ttl）
- 上限 2000 条（超过 popitem(last=False) 淘汰最旧）
- 键 = (table_stem, sheet, col_name, _json_hash(versions))
```

`_json_hash` 对 `versions` dict 做稳定哈希（`json.dumps(sort_keys=True)` + md5），保证"同格子同候选值"命中缓存，版本值一变自动失效。

## 7.7 作者归并（减少假冲突的另一招）

`_pick_author_representatives`：同作者多次提交，按作者分组，代表值取"最后一次修改"（版本号最大）。

**为什么减少冲突**：同一个人改了 3 次、值各不相同，其实只需看最后一次。归并后 distinct 集合变小 → 只剩一个不同值 → 判单向变更自动采纳，而不是报冲突。**只有跨作者的差异才报真冲突**。

> **与置信度的关系**：作者归并在 compare 阶段收窄冲突面（源头减量），置信度融合在建议阶段提升采纳质量（末端提质），两者正交，一起把"人工裁决的格子数"压到最小。

---

# 第 8 章：大表比对的性能解决方案

## 8.1 读引擎 calamine（Rust）

问题：openpyxl（Python 的 Excel 读写库）读 10w 行要 5.5s。

解决：优先用 `python-calamine`（Rust 语言写的 Excel 解析器，性能远超 Python 实现），10w 行 **0.05s**，快 100 倍。

> **量化佐证（【R3 基准】#8）**：`_dir_sheet_sets`（目录 sheet 集合扫描函数）222 次文件打开从 17.5s → 0.8s（openpyxl 0.3-0.5s/文件 → calamine 0.1ms/文件），是 subdir_compare（子目录比对）2.33x 的主因。

细节：
- calamine 只读值（等价于 data_only，只取计算结果不取公式），不读公式/批注（所以只用于 compare 阶段读数据，写回不用它）
- `_fix_calamine_value`（calamine 值修正函数）把 float 整数值转 int（浮点整数转整数），保证主键 `"1"` vs `"1.0"` 匹配一致
- 文件损坏回退 openpyxl 流式读

## 8.2 性能数据总表

| 优化项 | 收益（before → after） | 证据 |
|---|---|---|
| calamine Rust 读引擎 | 222 次打开 17.5s → **0.8s** | 【R3 基准】收益归因 #8 |
| svn info 批量预填 | ~190s → **0.2s**（148 次 subprocess 子进程调用 → 1 次） | 【R3 基准】#7 |
| subdir_compare（子目录比对） | 21785ms → 9342ms（**2.33x**） | 【R3 基准】monkeypatch A/B |
| branch_compare（跨分支比对） | 99524ms → 89513ms（**1.11x**） | 【R3 基准】 |
| ProcessPool 表级并行 | **2.0x**（8 表 × 450k 格：3.69s → 1.84s） | R6 samples；ThreadPool 仅 0.96x |
| 目录缓存预热 | `/dirs` 128.3ms → **0ms**（LRU/TTL 30s） | 【R3 基准】 |
| 未变更表跳过 | 74 表跳过 **64 表（86%）**，branch 54.2s → 49.3s | R6 #33 demo_svn |
| 假冲突消除（语义归一） | 假冲突率 0.5 → **0.0**（seed id=2，"100" vs 100.0） | R6 #24 demo_svn |
| 假 source_deleted（源分支被误判删除） | 69 → **0** | 【R3 基准】#9 |
| 10w 行大表 | compare ~3.9s + apply ~6.3s ≈ **10.4s** | 【R6 大表 after】 |
| sparse 内存 | GB 级 → **MB 级** | 【R6 大表】免深拷贝 |
| numpy 向量化 | 纯数据大表 **10-50×** | compare 版本 D 设计 + M7 消除 O(n²) |

> **口径提醒**：
> - "10w 行大表 10.4s"来自【R6 大表 after】`merge_eval`（合并评测脚本）进程内引擎层（compare 3943ms + apply 6336ms），**不含路由层 svn 反查**；svn 版 `branch_compare` 74 表全量 89.5s 里 ~88s 在引擎层 compare loop（比对循环）（【R3 基准】注意事项）。
> - 快照版（3000 行）07-30 压测 branch/subdir 均 ~18s（【M-snap 压测】），与 svn 版数字**不可横比**（数据源、规模、merge-base 机制都不同）。
> - 引擎层 10w 行 R6 before→after 为 10355.7 → 10409.2ms（±0.5% 持平），因 #24 语义归一在此 S-H 种子无命中场景；"假冲突 0.5→0"实绩来自 svn demo_svn 的 dev1/dev2 id=2 种子（见【R6 小种子】解读）。

## 8.3 并行方案（`parallel_compare.py`，并行比对模块）

**进程池 vs 线程池，按表数量自动选**：

```
表数 ≥ 4 → ProcessPool（多进程，绕开 GIL 全局解释器锁，CPU 密集才有效）
表数 < 4  → ThreadPool（线程池，小表 IO 阶段释放 GIL，省进程启动开销）
worker 上限 min(表数, 4, CPU核数)   # worker 工作进程数取三者最小值
单表失败不拖垮整批；整体超时/异常回退 ThreadPool
```

**为什么不用纯 ThreadPool**：Python 的 GIL（Global Interpreter Lock，全局解释器锁）导致多线程跑 CPU 密集任务时没有加速（实测 0.96x，几乎无收益）。多进程才是真正的并行。

**历史**：R3 先试 ThreadPool（无收益）→ R6 改 ProcessPool（2.0x）→ 后续把触发阈值从 9999 降到 4。

## 8.4 fast_apply 快路径（大表写回）

**问题**：10w 行表 openpyxl 全量 load+save（读入内存+保存）约 13s，但合并通常只改十几格——为了改十几格重写整个文件，浪费。

**解决**：直接操作文件底层的 XML，只改目标格：

```
zip 解包 → lxml 改目标 sheet 的 XML → 重打包
# lxml：C 语言实现的 XML 处理库，比 Python 快一个数量级
不 load 整个 workbook（工作簿），不重写没改的部分
```

**资格判定**（满足才走快路径，否则回退 openpyxl）：
- 文件 ≥512KB（小表不值得走快路径）
- 不含合并单元格
- 含公式/批注时：仅"更新格"放行；有"插行/删行"结构变更则回退

**lxml 是 C 实现**：25MB 的 sheet XML 序列化（转成 XML 字符串）从 3.4s → 1s。

> **量化佐证（【R6 大表】）**：10w 行 4 sheet 大表 apply(fast_xml)（快路径落盘）6376.6ms（before）→ 6335.8ms（after），快路径本身只处理十几格改动、秒级完成，且**不触碰公式单元格**（天然保护公式缓存）。

---

# 第 9 章：PPT 每页组织方案（按页设计，含形式与内容）

> 目标：把 Merge 萃取内容落到 PPT（`技术分享-Agent与三方合并.pptx.md` 第 4 章），每页给**内容要点 + 形式（文本 / 表格 / 代码 / 图）+ 性能数据引用**。Marp 约定：`---` 分页，`##` 为页标题，`|` 为表格，` ``` ` 为代码/流程图块。
> 全篇 12 页，遵循"每页只讲一件事"原则，数据页配证据标注。

---

## 9.0 全页清单（速览）

| 页 | 标题 | 形式 | 核心数据 |
|---|---|---|---|
| P1 | Merge 三方合并模型 | 图 + 文本 | — |
| P2 | Merge 总体框架 7 步 | 图 + 文本 | — |
| P3 | 两条合并链路 | 表格 | — |
| P4 | merge-base 定位（重点） | 图 + 表格 | — |
| P5 | 比对算法 5 版本迭代 | 表格 + 文本 | numpy 10-50× |
| P6 | 语义相等归一 | 代码 + 文本 | 假冲突 0.5→0 |
| P7 | ID 重映射 + 引用完整性 | 图 + 文本 | — |
| P8 | 公式缓存保护 | 图 + 文本 | 365 缓存归零实证 |
| P9 | AI 建议 + 置信度 | 表格 | 批量 N 往返→1 次 |
| P10 | 大表性能数据总表 | 表格 | subdir 2.33x 等 |
| P11 | 性能总览（svn 版 vs 快照版） | 表格 | 见 P10/P11 |
| P12 | 答辩话术速记 | 文本 | — |

---

## P1｜Merge 三方合并模型

**形式**：图形（ASCII 树）+ 3 行文本。

**内容**：

```text
          merge-base（公共祖先，SVN copyfrom 反查）
          /        \
      ours       theirs（两个分支最新版本）
          \        /
         merge result
```

**文本**：
- merge-base 反查：`svn log --stop-on-copy` 定位 copyfrom-rev，替代手工 fork 快照
- 两种流程：跨分支（absorb/merge_back）+ 同分支子目录合回（subdir）
- 数据源：真实 SVN 工作副本 `merge/svn/demo_svn/wc`（74 表含 monster 10w 行），快照版已下线

---

## P2｜Merge 总体框架 7 步

**形式**：流程图（编号箭头）+ 一句话说明。

**内容**：

```text
① 选分支 → ② 定位 merge-base → ③ 拉三份数据 → ④ compare 比对
        → ⑤ 冲突解决 → ⑥ 引用校验 → ⑦ 写回
```

**文本**：base 用 `svn export` 按历史版本导出；source/target 从工作副本直接拷；compare 主键对齐逐格语义判等；大表走 fast_apply 快路径。

---

## P3｜两条合并链路

**形式**：表格。

| 链路 | 干什么 | 入口 | merge-base 方式 |
|---|---|---|---|
| 跨分支合并 | dev 分支改动合进另一分支/trunk | `/api/merge/branch/compare` + `/apply` | 双方 copyfrom 链 LCA 交叉 |
| 目录合并 | trunk 下子目录合回父级 | `/api/merge/subdir/compare` + `/apply` | 子目录 copyfrom-rev 定为 base |
| （旧）三阶段 | 多生产者分三次汇总 | `merge_stages.py`（deprecated） | 保留兼容 |

**文本**：两条链路**共享同一套 `compare_sheet` 比对算法**，只有"选分支、定 base"不同，核心 diff 逻辑不重复实现。

---

## P4｜merge-base 定位（重点）

**形式**：图形（copyfrom 链）+ 表格（三种出生点）+ 降级链列表。

**内容**：

```text
trunk@r1 → 分支A@r5 → 分支B（B 从 A 复制）
链：B → copyfrom A@r5；A → copyfrom trunk@r1
LCA：两边都指向 trunk → base = trunk@min(r1, r2)
```

**表格（分支出生点三情况）**：

| 情况 | copyfrom-path | 标记 |
|---|---|---|
| 正常切分支 | 如 /trunk@r1 | inferred=false |
| 纯新建（手工 mkdir+add） | 路径自身 | inferred=true |
| svn 挂了 / r1 首提交 | 无 | ok:false |

**文本**：降级链 ① LCA 交叉 → ② 子目录场景 → ③ 单侧 inferred → ④ 单侧 fork 兜底 → ⑤ 报错要手工传 `merge_base_override`。原则：**找不到就报错，绝不瞎猜 base**。

---

## P5｜比对算法 5 版本迭代

**形式**：表格 + 一句话递进链。

| 版本 | 方法 | 换掉的瓶颈 | 效果 |
|---|---|---|---|
| A | 原始三重循环 | 基线 | 10w×100 列 ≈500 亿次 → 卡死 |
| B | `id()` 哈希预建行索引 | `list.index` O(n) 线性扫 | 找行 O(n)→O(1) |
| C | sparse 稀疏化 | 无差异行物化 100 格 dict | 内存 GB→MB |
| D | numpy 向量化 | 逐格调 `_semantic_key` | 纯数据大表 10-50× |
| E | 公式/批注回退逐格循环 | 向量化不懂公式/批注 | 正确性兜底 |

**文本**：递进链 A 能跑 → B 提找行 → C 降内存 → D 提计算 → E 保正确。量化佐证：10w 行从"卡死/数分钟"（【L-0728 全链基线】note_10w_timeout）压到 R6 实测 compare ~3.9s。

---

## P6｜语义相等归一（核心算法）

**形式**：代码块 + 3 个判等示例 + 数据。

**内容**：

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

**文本**：`"100"` vs `100`、`"a "` vs `"a"`、`0.1` vs `"0.10"` → 判相等。**只用于判等，不改原值**。量化：假冲突率 0.5 → 0.0（svn demo_svn seed id=2，【R6 小种子】解读）。

---

## P7｜ID 重映射 + 引用完整性

**形式**：图形（撞车场景）+ 分支标记映射表。

**内容**：

```text
分支 A 新增 id=99（A 的灵兽）
分支 B 新增 id=99（B 的道具）  ← 撞车
split（默认）：拆两条独立行，后到者 max+1
conflict：视为同一行冲突，交人工
```

**文本**：映射表带分支标记 `(file, old_pk) → new_pk`，非裸 `old_pk → new_pk`。动机：A 的 99 没变，不能因 B 的 99 重映射而误改 A 内部外键引用。`ref_integrity` 同步外键后扫悬空引用（dangling ref）。

---

## P8｜公式缓存保护

**形式**：图形（save 前后快照）+ 实证数字。

**内容**：

```text
① snapshot_before（data_only 读缓存值）
② openpyxl save（可能清缓存）
③ validate_and_fix（对比 lost/changed）
④ lost → LibreOffice 重算 → 二次校验 → 仍空则 needs_manual_fix
```

**文本**：
- 实证：`match_dan.xlsx` 365 个公式缓存 openpyxl save 后归零（0/365）
- 快判：zip 扫 `<f>` 标签，10w 行 ~0.05s vs openpyxl 全量读 ~6s（~120x）
- 落地：R9-A1 公式守门 audit 覆盖率 100% / 31 测试零回归

---

## P9｜AI 建议 + 置信度

**形式**：表格（两条链路）+ 公式（置信度）+ 表格（LLM 自评区间）。

**内容**：

| 名称 | 是什么 | 谁来做 |
|---|---|---|
| AI 建议 | 真调 LLM 给建议 | `suggest_merge` 链路 |
| ⭐推荐 | 规则版多数表决 | `recommend_version`，零 LLM |

**置信度三因子加权公式**：

```text
final = 0.60·LLM自评 + 0.25·rev领先度 + 0.15·值域合理性
       （None 因子用 LLM自评兜底，不稀释）
```

**LLM 自评区间**：

| 区间 | 依据 |
|---|---|
| 0.85-0.95 | rev 差 ≥2 + 值域合理 + 列语义明确 |
| 0.65-0.80 | rev 相邻 / 单一依据 |
| 0.40-0.55 | 值异常 / 语义模糊 |
| 0.30-0.40 | 完全无法判断 |
| 禁止 1.0 | LLM 不许说百分百确定 |

**文本**：批量接口把 N 次逐格往返（每格 15-45s）压成 1 次（30-60s）；缓存 LRU 2000/TTL 1h；预取冲突最多前 5 sheet。规则回退置信度写死（0.75/0.6/0.55/0.3）不融合，保 0.8 低置信阈值护栏。

---

## P10｜大表性能数据总表

**形式**：表格（每行带证据）。

| 优化项 | 收益（before → after） | 证据 |
|---|---|---|
| calamine Rust 读引擎 | 222 次打开 17.5s → 0.8s | 【R3 基准】#8 |
| svn info 批量预填 | ~190s → 0.2s（148→1 次 subprocess） | 【R3 基准】#7 |
| subdir_compare | 21785ms → 9342ms（2.33x） | 【R3 基准】 |
| branch_compare | 99524ms → 89513ms（1.11x） | 【R3 基准】 |
| ProcessPool 并行 | 2.0x（3.69s→1.84s，8 表） | R6 samples |
| 未变更表跳过 | 74 表跳过 64（86%） | R6 #33 |
| 10w 行大表 | compare ~3.9s + apply ~6.3s ≈ 10.4s | 【R6 大表】 |
| numpy 向量化 | 10-50× | 版本 D |
| sparse 内存 | GB → MB | 版本 C |

**文本**：口径——引擎层 vs 路由层不同范围；快照版（3000 行 ~18s）不可与 svn 版横比。

---

## P11｜性能总览：svn 版 vs 快照版（隔离对比）

**形式**：双列表格。

| 维度 | 快照版（snapshot） | svn 版（real svn） |
|---|---|---|
| 数据源 | `merge/demo/` 文件夹 | `merge/svn/demo_svn/{repo,wc}` |
| merge-base | `_meta.json` 反查 | `svn log --stop-on-copy` |
| 规模 | 3000 行 / 818 行 | 74 表含 monster 10w 行 |
| 代表指标 | subdir ~17854ms（07-30） | subdir 9342ms（08-11 R3） |
| 结论 | 正确但小表耗时偏高 | 路由层 + 引擎层双优化后 2.33x |

**文本**：铁律——**两版数字不可横比**（数据源、规模、merge-base 机制都不同）。所有提升% 必须同集 before→after。

---

## P12｜答辩话术速记

**形式**：纯文本（bullet）。

- 开场：一句话讲清"三方合并"——base 是共同起点，没 base 分不清谁改了
- 重点页（P4 merge-base）：copyfrom 是 SVN 客观记录，切分支必留，比 mergeinfo 可靠
- 亮点页（P5 五版本迭代）：每版换一个瓶颈，递进链体现工程方法论
- 数据页（P10/P11）：先讲"同集才可比"，再给 subdir 2.33x、假冲突 0.5→0
- 收尾边界：公式跨行/跨表复杂场景靠 LibreOffice 兜底，非全自动

---

## 9.x 与现有 PPT 第 4 章的映射

现有 `技术分享-Agent与三方合并.pptx.md` 第 4 章 4.1-4.8 是**内容分区**（非页面级），本方案 P1-P12 是**答辩页面级**。映射关系：

| 现有节 | 对应方案页 |
|---|---|
| 4.1 三方合并模型 | P1 |
| 4.2 行匹配与差异分类 | P5（比对算法）+ P6（语义归一） |
| 4.3 语义相等归一 | P6 |
| 4.4 列策略自动合并 | P9（推荐/表决） |
| 4.5 冲突推荐与多数表决 | P9 |
| 4.6 ID 冲突重映射 | P7 |
| 4.7 公式与引用处理 | P8 |
| 4.8 高级合并特性 | P7（批注 diff）+ P8（漏行） |
| 5.x 性能优化（Merge 侧） | P10 + P11 |

> 落地建议：把现有第 4 章 + 第 5 章 Merge 侧页面，按 P1-P12 重组；Agent 侧性能（5.1-5.4）保留在 Agent 章节不动。

---

# 附录：术语速查表（新手常问）

| 术语 | 一句话解释 |
|---|---|
| merge-base / base | 两个分支的公共历史起点 |
| copyfrom | SVN 记录的"分支从哪复制来的"信息 |
| LCA | 最近公共祖先（Lowest Common Ancestor） |
| 三方合并 | base + source + target 三份数据合并 |
| 语义归一 | 把值统一成"类型+数值"再比，消假冲突 |
| sparse 稀疏化 | 没差异的行只存主键格，省内存 |
| GIL | Python 全局解释器锁，多线程跑 CPU 任务无效 |
| dangling ref | 外键指向了不存在的 ID |
| 公式缓存 | Excel 存公式计算结果的地方，openpyxl save 会清它 |

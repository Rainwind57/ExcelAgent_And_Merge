# Merge 部分详细萃取文档（新手可读版）

> 本文档面向"刚接手项目的技术人员"，目标：读完能理解 Merge 每条链路在干什么、为什么这么设计、性能是怎么一步步优化出来的。
> 每个技术点都按「**是什么 → 为什么 → 怎么实现 → 关键数据**」四段讲，避免只列 file:line 不解释。

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

- **base**：两人开始改之前的公共版本（起点）
- **source**：其中一方的版本
- **target / ours**：另一方的版本
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

**SVN 定位公共祖先（merge-base）→ 拉取三份数据（base/source/target）到本地 → 主键对齐逐格语义判等 → 冲突自动/人工解决 → 引用校验 → 写回。**

## 1.2 完整流程（7 步，每步一句话说明）

```
① 选分支       用户选 trunk + 某个 dev 分支（跨分支），或 trunk 下某个子目录（目录合并）
② 定位 merge-base   SVN copyfrom 反查公共祖先（见第 3 章）
③ 拉取三份数据      base 用 svn export 按历史版本导出；source/target 从工作副本直接拷
④ compare 比对      把三份数据按主键对齐，逐格判断"改了/冲突/漏了"
⑤ 冲突解决          无冲突自动合；有冲突给人裁决（含 AI 建议）
⑥ 引用校验          外键跟着主键重映射一起改，改完检测"引用悬空"
⑦ 写回              大表走快路径，小表走 openpyxl（带公式缓存保护）
```

## 1.3 两条合并链路

| 链路 | 干什么 | 入口 |
|---|---|---|
| **跨分支合并** | 把 dev 分支的改动合进另一个分支/trunk | `/api/merge/branch/compare` + `/apply` |
| **目录合并** | 把 trunk 下某个子目录（subdev_x）合回父级 | `/api/merge/subdir/compare` + `/apply` |
| （旧）三阶段 | 多个生产者分多次提交，分三阶段汇总 | `merge_stages.py`（已 deprecated，保留兼容） |

**重要**：两条链路**共享同一套比对算法**（`compare_sheet`）和大量 SVN 辅助函数，只是"选分支、定 base"的方式不同，核心 diff 逻辑不重复实现。

## 1.4 跨分支的两种方向（direction）

| 方向 | 含义 | 落点 |
|---|---|---|
| `absorb` | 把 source 的改动吸进 target | 落 target，可产生新版本目录 `{name}_r{N+1}` |
| `merge_back` | 把分支合回 trunk | 落 trunk，版本化命名 `{group}_{N+1}.xlsx` |

---

# 第 2 章：使用环境准备（SVN 建立）

## 2.1 为什么需要 SVN

三方合并需要一个"有历史记录"的仓库来存 base。SVN 天生记录 `copyfrom`，所以用它来模拟真实的并行开发场景。

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

`svn mergeinfo` 查的是"哪些变更还没合并"，**依赖团队规范地执行过 merge 命令**。实际工作里大家常常手动拷文件、不规范 merge，mergeinfo 就不可靠。

**copyfrom 反查**只看"分支从哪复制的"这条客观记录——**切分支必然留下 copyfrom**，所以它可靠。

## 3.2 第一步：查单个分支的"出生点"（`_resolve_branch_point`）

对一个分支目录执行：

```bash
svn log -v --stop-on-copy --xml <分支路径>
```

- `--stop-on-copy`：遇到"这个目录被复制创建"的那条记录就停，不再往上追
- `-v`：输出路径变更详情（带 `copyfrom-path` 和 `copyfrom-rev`）

结果分三种：

| 情况 | copyfrom-path | copyfrom-rev | 标记 |
|---|---|---|---|
| 正常切分支（有复制记录） | 如 `/trunk` | 如 r1 | `inferred=false` |
| 纯新建（手工 mkdir+add，没复制） | 路径自身 | 首次提交 rev-1 | `inferred=true` |
| 第一次提交就是 r1 或 svn 挂了 | 无 | 无 | `ok:false` |

**inferred 是什么**：纯新建的目录没有真实 fork 点，rev 是"猜"的（首次提交减 1）。代码用 `inferred=true` 明确标注"这是推断，不是真 fork"，后续计算会跳过它。

## 3.3 第二步：沿 copyfrom 链往上追（`_copyfrom_chain_svn`）

分支可能不是从 trunk 直接 fork，而是"从分支的分支"fork：

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

## 3.4 第三步：两条链交叉求 LCA（`_lca_svn`）

两个分支各自有祖先链，找**两条链第一次指向同一个路径**的那个点：

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
⑤ 全失败 → 报错，要求用户手工传 merge_base_override
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

| 类型 | 含义 | 例子 |
|---|---|---|
| `matched` | 基准有、衍生也有 | 正常行 |
| `inserted` | 基准没有、衍生新增 | 新加了一行 |
| `deleted` | 基准有、衍生全删了 | 删了一行 |
| `missing_row` | 基准有、但某个全量覆盖分支整行漏拷 | **P0 漏行，要报警** |

`missing_row` 和 `deleted` 的区别很关键：deleted 是"明确删了"，missing_row 是"某个分支压根没拷这行"——后者是事故（数据静默消失），必须告警。

## 4.3 单元格三级分类

| 类型 | 含义 |
|---|---|
| 没变 | 所有衍生值和基准一致 |
| `changed` | 只有一个衍生值不同（单向修改，可自动采纳） |
| `conflict` | 多个衍生值互不相同（真冲突，需人工） |

判定核心是**语义相等归一**（`_semantic_key`）：把值先归一化再比，让 `"100"` 和 `100` 判为相等，消除"类型/格式差异造成的假冲突"。**只用于判等，不改原值**。

## 4.4 比对算法的 5 次迭代（性能主线）

这是最体现"工程能力"的部分——每一版都精准换掉上一版的唯一瓶颈。

| 版本 | 方法 | 换掉的瓶颈 | 效果 |
|---|---|---|---|
| A | 原始三重循环 | 基线 | 10w行×100列≈500亿次 → 卡死 |
| B | `id()` 哈希预建行索引 | 找行用 `list.index` O(n) 线性扫 | 找行 O(n)→O(1) |
| C | sparse 稀疏化 | 无差异行也物化 100 个格子的 dict | 内存 GB→MB |
| D | numpy 向量化 | 逐格调 Python 函数 `_semantic_key` | 纯数据大表 10-50× |
| E | 公式/批注回退逐格循环 | 向量化不懂公式/批注 | 正确性兜底 |

### 版本 A：三重循环（基线）

朴素做法：行 × 列 × 文件三个 `for` 嵌套，逐格两两比较。10w 行 × 100 列 × 多文件 ≈ 500 亿次 Python 函数调用，直接卡死。

### 版本 B：哈希索引（解决"找行"慢）

问题：要找到"衍生文件里主键匹配的那一行"，用了 `list.index()` 线性扫，每找一行 O(n)。

解决：预先建 `{id(行) → 行号}` 哈希表，之后 O(1) 定位。

### 版本 C：sparse 稀疏化（解决"内存"爆）

问题：B 版解决了速度，但每行无论有没有差异，都物化成完整字典（10w×100 个 dict 对象），内存和 JSON 都是 GB 级。

解决：**没差异的行只存一个主键格**，不展开 100 个格子。

```python
if sparse and row_type == 'matched' and not row_has_diff:
    cells = [pk_cell]    # 只有主键，而不是 100 个 cell
```

正确性三保证（砍掉的信息从别处补）：
1. 导出走基准克隆（没差异的行直接复用基准原样）
2. `id_resolver` 只需要主键格
3. `ref_integrity` 对未变动行由基准兜底

### 版本 D：numpy 向量化（解决"函数调用"慢）

问题：C 版省了内存，但每格还要调一次 `_semantic_key` 归一化 + 逐格比较，1000 万次 Python 函数调用。

解决：把"逐格算 → 逐格比"改成"**批量算 → 广播比**"：

```python
# 1. 预先算好每个文件的语义键矩阵（每格只归一化一次）
key_mats[fname] = kmat          # shape (n_rows, n_cols)

# 2. base 与所有衍生矩阵做一次 numpy 广播比较
has_diff = np.zeros((n_rows, n_cols), dtype=bool)
for fname in other_files:
    has_diff |= (base_keys != key_mats[fname])   # 整个矩阵一次广播
```

为什么快：归一化从"每次比较重算两次"→ 每格只算一次；比较从 Python 三层循环 → numpy C 层广播。纯数据大表 **10-50 倍**。

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

## 5.2 ID 重映射流程（`id_resolver.py`）

1. **检测**：只看 `inserted` 行；找出"同一个主键、来自多个不同文件"的行
2. **判断内容是否真不同**：逐文件取整行签名比较，内容完全相同就合并成一条（不算冲突）
3. **双模式处理**：

| 模式 | 行为 | 适用 |
|---|---|---|
| `split`（默认） | 内容不同 → 拆成两条独立行，后到的重映射新 ID | 确实是两条不同的新数据 |
| `conflict` | 视为同一行冲突，不拆不重映射，交人工 | 需要人判断是不是同一个东西 |

4. **分配新主键**：从 `max(已用主键)+1` 起自增，跳过已占用；防死循环上限 10 万次；纯数字写 `int`（避免导出成文本）

## 5.3 关键设计：为什么映射表要带"分支标记"

映射表结构：`[{file, old_pk, new_pk, reason}]`——注意是 `(file, old_pk) → new_pk`，不是裸 `old_pk → new_pk`。

**原因**：假设分支 A 和分支 B 都各自新增了 99，重映射后：
- A 的 99 → 保留 99
- B 的 99 → 改成 100

如果映射表只有 `99 → 100`，那么"分支 A 内部某行外键引用了 99"也会被误改成 100——**但 A 的 99 根本没变**！

带 `(file, old_pk)` 就能区分：`(B, 99) → 100` 只改 B 里的引用，A 里的 99 不受影响。

## 5.4 引用完整性（`ref_integrity.py`）

重映射后，引用旧 ID 的外键要跟着改，改完再检测"悬空引用"：

```python
remap_lookup = {(m["file"], m["old_pk"]): m["new_pk"] for m in id_mapping}
for fn, v in list(versions.items()):
    new_pk = remap_lookup.get((fn, v_str))   # 命中 → 同步外键值
    if new_pk: versions[fn] = new_pk
# 外键值不在本表主键集、也不在跨表主键集 → dangling（悬空引用）
```

**悬空引用** = 某行外键指向了一个不存在的 ID。这是合并后最容易埋的雷（引用断裂 → 游戏## 5.5 跨表 ID 段校验（`id_scope.py`）

除了同表撞车，还有**跨表撞车**：两个不同的 .xlsx 文件用了同一个 ID（比如 fashion 表和 item 表都有 id=100）。

- 段定义读 `resources/id_mgr.xlsx` 的 SETTING sheet（每个模块占一段 ID 区间）
- 扫全目录所有 .xlsx，同一个 id 出现在 ≥2 个不同文件 → 冲突（P0）
- 单遍 `iter_rows` 提取 ID 列建索引（性能优化）

---

# 第 6 章：公式缓存保护（重点，P0 问题）

## 6.1 问题背景：什么是公式缓存，为什么会丢

Excel 的公式单元格里存的是公式文本（如 `=B2+C2`），**计算结果是"缓存"在文件里的**。

- 编表工具链用 xlrd 读，xlrd 读公式单元格**返回缓存值**，没缓存就返回空
- 但 openpyxl 没有公式引擎，`save` 时会**清空缓存值**（公式文本保留，结果没了）
- 结果：openpyxl 改表保存 → 缓存清零 → 编表读到空 → 输出 0/空

实测：原始 `match_dan.xlsx` 365 个公式缓存全在，openpyxl save 后归零（0/365）。

## 6.2 校验时机：写回时，不是 compare 时

compare 阶段只读不写，不碰缓存。只有 `save`（写回/导出）才可能清缓存，所以校验挂在**写盘这一步**。

核心函数 `_save_with_formula_cache`（`routers/diff.py:598`）：

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
- `data_only=False`：识别哪些格是公式（值以 `=` 开头）
- `data_only=True`：读这些公式的**缓存值**

快照按 `(路径, 修改时间, 文件大小)` 缓存，FIFO 上限 64 个文件，文件变了自动失效。

## 6.5 校验 + 修复流程（validate_and_fix）

```
① 无公式 → 直接通过（fast-path）
② 回读对比快照：
   lost：save 前有缓存值 / save 后没了 → 真丢失，需要重算
   changed：前后值不一致 → 可能是输入源变了，缓存正常更新
③ 只有 changed 且保存后全非空 → 判定"输入源变更导致结果更新，缓存完好"
④ 有 lost → 用 LibreOffice 重算 → 二次校验
⑤ 重算后还是空 → needs_manual_fix=True（标记人工处理）
```

## 6.6 LibreOffice 重算

```bash
soffice --headless --norestore -env:UserInstallation=file:///profile \
        --convert-to xlsx --calc --outdir tmp <文件路径>
```

- 60 秒超时
- per-file 互斥锁（防并发写同一文件）
- 输出到 tmp 再替换原文件（避免覆盖失败）
- 路径解析：`LIBREOFFICE_PATH` 环境变量 → PATH → Windows 常见安装路径兜底
- 重算后粗略校验批注/样式保留

## 6.7 大表快路径天然绕过

`fast_apply` 用 zip+XML 直改，**只改目标单元格、不动公式单元格**，所以快路径本身不会破坏公式缓存——这是它比 openpyxl 全量 save 的另一个优势。

## 6.8 测试覆盖

`test_formula_cache.py` 6 场景：非公式 / VLOOKUP 重算 / 嵌套 IF / SUM 聚合 / 循环引用 / 无公式 fast-path。

---

# 第 7 章：AI 建议 + 置信度

## 7.1 先分清两套东西

| 名称 | 是什么 | 谁来做 |
|---|---|---|
| **AI 建议** | 真正调 LLM 给建议 | 已实现，`suggest_merge` 链路 |
| **⭐推荐** | 规则版多数表决 | `recommend_version`，纯代码不调 LLM |

## 7.2 ⭐推荐（规则版，多数表决）

没调 LLM 也能给的快速推荐，逻辑很简单：

```python
统计各版本值（用语义键去重）："100" 和 100 算同一票 某值出现 ≥2 次（多数）：
    → 和基准一致 → 推荐保留基准
    → 和基准不同 → 推荐那个多数值
→ 全不同 → 保守回退基准（提示人评估）
```

**一句话**：多数派说了算，没有多数派就回基准。

## 7.3 AI 建议完整链路

```
用户点"AI 建议" → POST /api/agent/suggest-merge
→ asyncio.to_thread 卸到线程池（不卡主线程）
→ suggest_merge：
    有 version_meta（SVN 修订信息）→ 先调 LLM
    无 meta 或 LLM 失败 → 回退规则
→ LLM 输入：表名/sheet/列名/行键/基准值 + 各版本（rev=/date= 修订信息）
→ LLM 输出：JSON（suggested_version + reasoning + confidence）
→ 校验 suggested_version 必须是输入衍生版本之一（防 LLM 乱编）
```

## 7.4 置信度实现

**LLM 置信度**（在 system prompt 里规定，让 LLM 自己评估）：

| 区间 | 依据 |
|---|---|
| 0.85-0.95 | rev 差距 ≥2 + 值在值域 + 列语义明确 |
| 0.65-0.80 | rev 相邻 / 只有"较新"一条依据 |
| 0.40-0.55 | 值异常 / 语义模糊 |
| 0.30-0.40 | 完全无法判断 |
| 禁止 1.0 | LLM 不许说百分百确定 |

**规则回退的置信度**（写死的）：ID 列 0.75 / 数值列 0.6 / 文本列 0.55 / 兜底 0.3。

**低置信阈值**：前端默认 0.8，`conf < 0.8` 批量采纳时**跳过留人工**。注意 0.8 这个阈值会自动排除所有规则回退（最高才 0.75），即"规则给的建议不自动采纳，只有 LLM 高置信建议才自动采纳"。

## 7.5 AI 建议参考哪些指标（喂给 LLM 的信号）

1. SVN 修订时间先后（`rev=`/`date=`，rev 大 = 较新 = 倾向采纳）
2. 列名/列语义（ID/编号/数值/枚举白名单）
3. 单元格值内容
4. 行键/基准值
5. 基准标记 `[基准·仅参考]`（强制 LLM 不得采纳基准）

## 7.6 时间消耗 + 缓存 + 并行

**缓存**：TTL 1 小时 + LRU 上限 2000 条，键 = `(表名, sheet, 列名, 版本哈希)`——同一个格子重复点建议直接命中缓存。

**批量接口三级策略**：
1. 缓存优先
2. 批量 LLM（≥2 格且都有 version_meta）：**一次 LLM 覆盖全部未缓存格**，把 N 次往返（每格 15-45s）压成 1 次（约 30-60s）
3. 回退逐格并行：`ThreadPoolExecutor(max_workers=min(8, 格数))`

**预取**：compare 返回后，后台预取"冲突最多的前 5 个 sheet"的 AI 建议填缓存——用户点开时已经是热的。

**超时**：LLM 调用无显式 timeout（依赖底层默认），失败 try/except 回退规则。

## 7.7 作者归并（减少假冲突的另一招）

`_pick_author_representatives`：同作者多次提交，按作者分组，代表值取"最后一次修改"（版本号最大）。

**为什么减少冲突**：同一个人改了 3 次、值各不相同，其实只需要看最后一次。归并后 distinct 集合变小 → 只剩一个不同值 → 判单向变更自动采纳，而不是报冲突。**只有跨作者的差异才报真冲突**。

---

# 第 8 章：大表比对的性能解决方案

## 8.1 读引擎 calamine（Rust）

问题：openpyxl 读 10w 行要 5.5s。

解决：优先用 `python-calamine`（Rust 写的解析器），10w 行 **0.05s**，快 100 倍。

细节：
- calamine 只读值（等价于 data_only），不读公式/批注（所以只用于 compare 阶段读数据，写回不用它）
- `_fix_calamine_value` 把 float 整数值转 int，保证主键 `"1"` vs `"1.0"` 匹配一致
- 文件损坏回退 openpyxl 流式读

## 8.2 性能数据总表

| 优化项 | 收益 |
|---|---|
| calamine Rust 读引擎 | 222 次打开 17.5s → **0.8s** |
| svn info 批量预填 | ~190s → **0.2s** |
| subdir_compare | 21.8s → 9.3s（**2.33x**） |
| ProcessPool 表级并行 | **2.0x**（8 表 × 450k 格：3.69s → 1.84s） |
| 目录缓存预热 | `/dirs` 128.3ms → **0ms**（LRU/TTL 30s） |
| 未变更表跳过 | 74 表跳过 **64 表（86%）** |
| 10w 行大表 | compare ~3.9s + apply ~6.3s ≈ **10.4s** |
| sparse 内存 | GB 级 → **MB 级** |
| numpy 向量化 | 纯数据大表 **10-50×** |

## 8.3 并行方案（`parallel_compare.py`）

**进程池 vs 线程池，按表数量自动选**：

```
表数 ≥ 4 → ProcessPool（多进程绕开 GIL，CPU 密集才有效）
表数 < 4  → ThreadPool（小表 IO 阶段释放 GIL，省进程启动开销）
worker 上限 min(表数, 4, CPU核数)
单表失败不拖垮整批；整体超时/异常回退 ThreadPool
```

**为什么不用纯 ThreadPool**：Python 的 GIL（全局解释器锁）导致多线程跑 CPU 密集任务时没有加速（实测 0.96x，几乎无收益）。多进程才是真正的并行。

**历史**：R3 先试 ThreadPool（无收益）→ R6 改 ProcessPool（2.0x）→ 后续把触发阈值从 9999 降到 4。

## 8.4 fast_apply 快路径（大表写回）

**问题**：10w 行表 openpyxl 全量 load+save 约 13s，但合并通常只改十几格——为了改十几格重写整个文件，浪费。

**解决**：直接操作文件底层的 XML，只改目标格：

```
zip 解包 → lxml 改目标 sheet 的 XML → 重打包
不 load 整个 workbook，不重写没改的部分
```

**资格判定**（满足才走快路径，否则回退 openpyxl）：
- 文件 ≥512KB（小表不值得走快路径）
- 不含合并单元格
- 含公式/批注时：仅"更新格"放行；有"插行/删行"结构变更则回退

**lxml 是 C 实现**：25MB 的 sheet XML 序列化从 3.4s → 1s。

---

# 第 9 章：答辩讲解建议（按页组织）

| 页 | 内容 | 讲解重点 |
|---|---|---|
| 1 | Merge 总体框架 7 步流程 | 一张流程图讲全，先讲"三方合并"概念 |
| 2 | 环境准备：SVN 仓库 + 种子数据 | 目录树图 + 6 步脚本链 + 冲突锚点 |
| 3 | 两条链路方法总览 | absorb/merge_back + subdir 差异 |
| 4 | merge-base 定位 | copyfrom 反查 + 降级链（重点讲清楚） |
| 5 | 比对算法 5 版本迭代 | 版本 A→E 递进链，突出 numpy 10-50× |
| 6 | ID 重映射 + 引用完整性 | split/conflict + 分支标记动机 |
| 7 | 公式缓存保护 | save 前后快照 + LibreOffice 重算 |
| 8 | AI 建议 + 置信度 | 两条链路 + 置信度区间表 |
| 9 | 大表性能方案 | 性能数据总表 |

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

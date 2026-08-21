# 主干目录（trunk）
#
# 固定的主干基准目录。所有生产者基于此目录拷贝到自己的生成者目录进行修改。
#
# 文件命名约定：
#   {table}.xlsx          = 主干基准（无 _数字 后缀，is_base_file() 为 True）
#
# 本目录是阶段2 的「目标目录」：阶段1 产出的中间版本会 merge 到此目录的基准文件。
# merge 前会自动备份为 {table}.xlsx.bak_{timestamp}，然后覆盖写回。
#
# 当前演示样本（从原 merge 根目录迁移）：
#   ability.xlsx      主干基准
#   item.xlsx         主干基准
#   match_stat.xlsx   主干基准（含公式列）
#
# ── SVN 派生新流程与 legacy 手工流程并存 ──
# trunk 既是 legacy 阶段2/3 的目标目录，也是新流程两种合并模式的常见落点：
#   - 跨分支合回 trunk（direction=merge_back）→ /api/merge/branch/apply
#     落点 trunk，版本化命名 {group}_{N+1}.xlsx（复用 get_next_merge_version）
#   - 子目录合回 trunk 本体（target_dir=trunk）→ /api/merge/subdir/apply
#     落点 target_dir，同样版本化命名 {group}_{N+1}.xlsx
# 新流程的 merge-base 由 SVN copyfrom 自动反查（svn_history.py 的 branch-point），
# 不再依赖 merge/mergebase/ 手工 fork 快照。两种数据源物理隔离：
#   方式一：无 SVN 环境时走 merge/demo/ 的文件夹模拟快照（_meta.json 记 copyfrom，
#           由 merge/scripts/build_svn_demo.py 生成）；
#   方式二：真实 SVN 走 merge/svn/ 下 svnadmin 初始化的仓库与工作副本。
# legacy 三阶段端点（/api/merge/stage1|2|3/*）保留不改行为，已标 deprecated。

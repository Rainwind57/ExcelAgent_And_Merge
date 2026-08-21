# 生成者目录（src）
#
# 生产者基于 trunk 目录拷贝到此目录进行修改。一个生产者对应一个子目录（按分支/作者名）。
#
# 子目录命名约定：
#   src/{branch_name}/          = 某生产者的工作目录（真实生产者提交区）
#       {table}_1.xlsx          = 该生产者的多次提交之一（版本号按提交顺序递增）
#       {table}_2.xlsx          = 第二次提交
#       {table}_3.xlsx          = 第三次提交
#
# devbranch 子目录特殊：作为阶段1 缓冲区，不在其中直接提交文件，
# 而是汇聚各真实生产者（devbranch1/devbranch2）阶段1 产出的中间版本：
#   src/devbranch/{table}_merged.xlsx  = 某生产者阶段1 产出的中间版本
#
# 阶段1：在真实生产者子目录内部（devbranch1/devbranch2）把多次提交合并成中间版本
#   {table}_merged.xlsx：
#   - 取最早提交作基准（或生产者指定的基准），其余作衍生
#   - 复用 compare_sheet + id_resolver 三方比对
#   - 内部冲突必须先解决，阻断导出
#   - 产出 {table}_merged.xlsx 复制到缓冲区 src/devbranch/ 供阶段2 使用
#
# 阶段2：取 trunk/{table}.xlsx 作基准、src/devbranch/{table}_merged.xlsx 作衍生，merge 回 trunk。
#
# 真实生产者演示样本（由 merge/scripts/build_merge_materials.py 从 samples/ 散放文件幂等生成）：
#   src/devbranch1/{table}_1.xlsx ~ {table}_2.xlsx   生产者1 的两次提交
#   src/devbranch2/{table}_1.xlsx ~ {table}_2.xlsx   生产者2 的两次提交
#
# ── SVN 派生新流程与 legacy 手工流程并存 ──
# 本目录的 {branch}/{table}_N.xlsx 文件名后缀模拟提交历史，服务于 legacy 三阶段
# 流程（/api/merge/stage1|2|3/*，见 server/routers/merge_stages.py，已 deprecated）。
#
# 新流程不再依赖文件名后缀模拟提交，改由真实 SVN copyfrom 版本号自动定位 merge-base：
#   - 跨分支合并（absorb / merge_back）→ /api/merge/branch/compare|apply
#     （server/routers/merge_branch.py，两种数据源物理隔离：方式一 demo 快照
#      merge/demo/，方式二真实 SVN 工作副本 merge/svn/）
#   - 同分支子目录合回目标目录 → /api/merge/subdir/compare|apply
#     （server/routers/merge_subdir.py，子目录样本位于 trunk 下各开发子目录）
# 新流程的 merge-base 通过 `svn log --stop-on-copy` 反查 copyfrom-rev 得到，
# 不再需要 merge/mergebase/ 下手工拷贝的 fork 快照（该目录仅 legacy 流程使用）。
# build_merge_materials.py 传 --from-svn-fixture 时会用 merge/svn/fixture/ 的真实
# SVN 提交历史覆盖部分分组样本，不传时维持纯文件复制模式（fallback，无 SVN 依赖）。
# 两种流程并存，legacy 不删除不改行为，待前端切换后再评估下线。

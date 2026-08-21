# devbranch 缓冲区（阶段1 中间版本汇聚目录）
#
# 本目录是阶段1→阶段2 之间的中间版本缓冲区，不是生产者提交区，
# 不在此手动放置 {table}_1/2/3.xlsx 提交文件。
#
# 内容由后端自动产出：
#   {table}_merged_{branch}.xlsx   某生产者阶段1 consolidate 的中间版本
#                                  （如 item_merged_devbranch1.xlsx）
#   _stage1_manifest.json          阶段门禁清单，记录各中间版本的
#                                  {branch, group, base_file, created, stage1_ok}
#
# 阶段2 取 trunk/{table}.xlsx 作 base、本目录 {table}_merged_{branch}.xlsx 作衍生，
# 校验 manifest 中 stage1_ok=True 且文件存在后方可比对合回，版本化产出到 trunk。

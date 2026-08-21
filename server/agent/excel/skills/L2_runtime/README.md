# L2 运行时层（使用经验）：由 skill_updater 从运行证据 promote 生成。
# 文件：
#   column_aliases.runtime.yaml   运行时列别名（低置信度/纠正命中累计 ≥3 次后 promote）
#   table_relations.runtime.json  跨表热路径权重（session 内连续操作两表 → co_occur++）
# 本目录文件不被 schema_infer.regenerate_skills 覆盖（自动生成只写 skills/ 顶层平铺文件）。

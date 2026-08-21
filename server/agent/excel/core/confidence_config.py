"""定位置信度集中配置。

把原先散落在 agent.py / table_locator.py / fuzzy_matcher.py 等文件中的
硬编码置信度常量收敛到一处，方便统一调参、跨层比较。

当前仅供 row/column 定位层试点使用（其余层置信度常量仍在各自文件中，
后续验证有效后再迁移）。
"""

from __future__ import annotations

# 置信度达到该值视为"可直接采用，无需特殊提示"
ACCEPT_THRESHOLD = 0.75

# 行定位：各匹配层级对应的基础置信度（层级越靠后越不可靠）
ROW_METHOD_CONFIDENCE: dict[str, float] = {
    "exact": 1.00,
    "startswith": 0.95,
    "contains_direct": 0.90,
    "contains_paren_stripped": 0.75,
    "contains_num_stripped": 0.55,
    # 索引快路径：经 row_index 倒排命中，略低于遍历同档
    # （索引可能滞后于文件实际内容，且命中后另读单元格校验防过期）
    "index_exact": 0.98,
    "index_startswith": 0.93,
    "index_contains": 0.88,
}

# 同一层级命中多行（歧义）时的置信度折扣系数
ROW_AMBIGUOUS_PENALTY = 0.70

# 剥离类别前缀（如"灵兽""道具"）后递归命中时的置信度折扣系数
ROW_PREFIX_STRIP_PENALTY = 0.85

# 列定位：低于该分数在结果提示中追加"置信度偏低"提示
COLUMN_LOW_CONFIDENCE_HINT = "（置信度偏低，建议核对）"

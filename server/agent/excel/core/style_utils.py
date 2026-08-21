"""单元格样式复制工具。

写表操作（write_cell/append_row/insert_row）写入值时调用 copy_cell_style
保留目标单元格或同列上一数据行的样式（font/fill/border/alignment/number_format/protection），
避免改表后 alignment 丢失、日期变序列号、数字格式断裂。

与 sort_sheet 的样式深拷贝思路一致，用 copy.copy 浅拷贝避免引用共享污染。
"""

from __future__ import annotations

from copy import copy
from typing import Optional


def copy_cell_style(src_cell, dst_cell) -> None:
    """把 src_cell 的样式复制到 dst_cell（浅拷贝各样式对象，避免引用共享）。

    复制范围：font / fill / border / alignment / number_format / protection。
    src_cell 无样式（has_style=False）时跳过，dst 保持默认。
    """
    if not src_cell.has_style:
        return
    if src_cell.has_style:
        dst_cell.font = copy(src_cell.font)
        dst_cell.fill = copy(src_cell.fill)
        dst_cell.border = copy(src_cell.border)
        dst_cell.alignment = copy(src_cell.alignment)
        dst_cell.number_format = src_cell.number_format
        dst_cell.protection = copy(src_cell.protection)


def copy_row_style(ws, src_row: int, dst_row: int, max_col: Optional[int] = None) -> None:
    """整行复制样式：src_row 各列样式 → dst_row 对应列。

    Args:
        ws: openpyxl Worksheet 对象。
        src_row: 源行号（1-based）。
        dst_row: 目标行号（1-based）。
        max_col: 复制列数上限，None 时用 ws.max_column。
    """
    if max_col is None:
        max_col = ws.max_column
    for c in range(1, max_col + 1):
        copy_cell_style(ws.cell(src_row, c), ws.cell(dst_row, c))


def inherit_column_style(ws, row: int, col: int, data_start_row: int) -> None:
    """write_cell 后兜底：若 dst 单元格原本无样式 → 从同列上一数据行继承 alignment+number_format。

    用于 write_cell 写入新格（或原本无样式的单元格）时保证列对齐/数字格式一致。
    有样式则不动（保留目标单元格原样式）。
    """
    dst = ws.cell(row, col)
    if dst.has_style:
        return
    # 从上一数据行向上找首个有样式的同行单元格
    for r in range(row - 1, data_start_row - 1, -1):
        src = ws.cell(r, col)
        if src.has_style:
            dst.alignment = copy(src.alignment)
            dst.number_format = src.number_format
            return


def get_column_number_format_majority(ws, col: int, row: int,
                                      data_start_row: int, n: int = 3) -> str:
    """读同列最近 n 行的 number_format，取众数。

    用于 date/datetime 列写值前查相近行实际格式，避免 yaml 固定 format
    覆盖表内已有格式（如表里用 mm/dd/yyyy，yaml 写 yyyy-mm-dd）。

    Args:
        ws: openpyxl Worksheet。
        col: 列号（1-based）。
        row: 当前写值行号（1-based），向上取样。
        data_start_row: 数据起始行，取样不低于此行。
        n: 取样行数，默认 3。

    Returns:
        众数 number_format 字符串；全空/无数据返回空串（调用方兜底）。
    """
    from collections import Counter
    formats: list[str] = []
    for r in range(row - 1, max(data_start_row - 1, row - n - 1), -1):
        c = ws.cell(r, col)
        nf = c.number_format
        # 过滤 openpyxl 默认 'General'（无显式格式）
        if nf and nf != "General":
            formats.append(nf)
    if not formats:
        return ""
    # 取众数；同票时取首个出现的（最近行优先）
    counter = Counter(formats)
    return counter.most_common(1)[0][0]

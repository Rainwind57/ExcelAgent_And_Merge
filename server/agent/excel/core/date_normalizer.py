"""日期多形式归一化。

用户说"2024年1月1日" / "2024/1/1" / "2024-01-01" / 时间戳，统一解析为
datetime 对象。openpyxl 写入 datetime 后自动按单元格 number_format 显示，
配合 value_constraints.yaml 的 type: date/datetime + format 字段保证
日期列写入后格式一致，Excel 能识别为日期参与排序/筛选。

纯结构化解析，不依赖 LLM。
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Optional


def parse_date(text) -> Optional[datetime]:
    """解析多形式日期字符串为 datetime 对象。

    支持形式（按优先级匹配）：
      1. `2024-01-01 12:00:00` / `2024-01-01 12:00` / `2024-01-01`
      2. `2024/1/1 12:00:00` / `2024/1/1`
      3. `2024年1月1日 12:00:00` / `2024年1月1日`
      4. Unix 时间戳：10 位秒 / 13 位毫秒
      5. ISO 8601：`2024-01-01T12:00:00`

    Args:
        text: 字符串或已是 datetime 的对象。None/空串返回 None。

    Returns:
        datetime 对象；无法识别返回 None。
    """
    if text is None:
        return None
    if isinstance(text, datetime):
        return text
    if isinstance(text, (int, float)):
        return _from_timestamp(text)
    s = str(text).strip()
    if not s:
        return None
    # 已是 datetime 字符串变体
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%Y年%m月%d日 %H:%M:%S",
        "%Y年%m月%d日 %H:%M",
        "%Y年%m月%d日",
    ):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    # ISO 8601
    iso_m = re.match(r"^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})$", s)
    if iso_m:
        try:
            return datetime(*[int(x) for x in iso_m.groups()])
        except ValueError:
            pass
    iso_d = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", s)
    if iso_d:
        try:
            return datetime(int(iso_d.group(1)), int(iso_d.group(2)), int(iso_d.group(3)))
        except ValueError:
            pass
    # 纯数字 → 时间戳
    if re.fullmatch(r"\d{10}|\d{13}", s):
        try:
            return _from_timestamp(int(s))
        except (ValueError, OSError):
            return None
    return None


def _from_timestamp(ts) -> Optional[datetime]:
    """时间戳（秒或毫秒）转 datetime。"""
    try:
        if ts > 10_000_000_000:  # 毫秒
            return datetime.fromtimestamp(ts / 1000)
        return datetime.fromtimestamp(ts)
    except (ValueError, OSError, OverflowError):
        return None


def normalize(text, fmt: str = "yyyy-mm-dd") -> Optional[datetime]:
    """归一化日期文本为 datetime 对象。

    fmt 参数保留与 value_constraints.yaml 的 format 字段对接的语义，
    实际显示格式由 write_cell 按 number_format 设置，本函数只负责解析。

    Returns:
        datetime 对象；无法识别返回 None。
    """
    return parse_date(text)

"""列名解析器：表头为空时回退用类型行(row2)前缀翻译成中文当列名。

背景：部分 sheet 第一列(主键)表头为空，但类型行(row2)含完整列名标注
(如 ability_id:int)。本模块把这类标注的英文前缀翻译成中文(ability→神通)，
组合成可被自然语言寻址的列名(神通id)，让无表头主键列也能被 add/set/locate 命中。

仅当表头单元格为空、且类型行单元格形如 name:type 时才回退；非空表头原样返回，
保证不影响已有列名。翻译表复用 index_builder_hints.yaml 的 stem_to_domain 并补充常见词。
"""
from __future__ import annotations

import re
from typing import Any

# 英文实体前缀 → 中文。复用 skills/index_builder_hints.yaml 的 stem_to_domain 并补充。
_EN_TO_CN = {
    "ability": "神通",
    "spell": "技能",
    "pet": "灵兽",
    "building": "建筑",
    "item": "道具",
    "hero": "英雄",
    "fabao": "法宝",
    "mail": "邮件",
    "guild": "帮派",
    "assistant": "仙友",
    "passive": "被动",
    "combat": "战斗",
    "buff": "buff",
    "city": "城池",
    "space": "场景",
    "quest": "任务",
    "reward": "奖励",
    "activity": "活动",
    "residence": "洞府",
    "model": "模型",
    "icon": "图标",
    "name": "名称",
    "desc": "描述",
    "level": "等级",
    "equipment": "装备",
    "npc": "NPC",
    "const": "常量",
    "map": "地图",
    "quest": "任务",
}

# 合法列名前缀字符（字母/下划线开头，允许字母数字下划线点）
_IDENT_RE = re.compile(r"[A-Za-z_][\w.]*")


def _type_row_name_prefix(type_raw: Any) -> str:
    """从类型行单元格提取列名前缀。ability_id:int → ability_id；非法返回 ''。

    仅接受 name:type 形式（含冒号），避免把无类型行的数据行误当类型行。
    """
    if type_raw is None:
        return ""
    s = str(type_raw).strip()
    if not s or ":" not in s:
        return ""
    name = s.rsplit(":", 1)[0].strip()
    if not name or not _IDENT_RE.fullmatch(name):
        return ""
    return name


def _translate(prefix_name: str) -> str:
    """ability_id → 神通id；前缀不在表则保留英文（好过空）。"""
    # 剥末尾 id / _id
    m = re.match(r"^(.*?)(_?id)$", prefix_name, re.IGNORECASE)
    if m:
        body = m.group(1).strip("_")
        if not body:
            return prefix_name
        cn = _EN_TO_CN.get(body.lower())
        if cn:
            return f"{cn}id"
        return prefix_name
    # 无 id 后缀：整体翻译（如 name → 名称）
    cn = _EN_TO_CN.get(prefix_name.lower())
    return cn if cn else prefix_name


def resolve_header_cell(header_raw: Any, type_raw: Any) -> Any:
    """返回列的有效值：表头非空原样返回；表头空则用类型行前缀翻译；都空返回 None。

    保留 None 语义（与原 ws.cell 行为一致），让下游 ``str(h) if h is not None else ""``
    逻辑不变；仅空表头列被填入翻译后的列名。
    """
    if header_raw is not None and str(header_raw).strip() != "":
        return header_raw
    prefix = _type_row_name_prefix(type_raw)
    if not prefix:
        return None
    return _translate(prefix)


def build_headers(ws, header_row: int, type_row: int | None = None) -> list:
    """构建有效列值列表：读 header_row，空列用 type_row 回退。

    供 cli_interface._header_of / read_browse 复用，保证运行时 read_header 与
    索引 header_names 一致。
    """
    max_col = ws.max_column or 0
    result: list = []
    for c in range(1, max_col + 1):
        hv = ws.cell(header_row, c).value
        tv = ws.cell(type_row, c).value if type_row else None
        result.append(resolve_header_cell(hv, tv))
    return result

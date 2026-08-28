"""基于 CodeMaker Serve API 的 NL 意图解析器。

通过 HTTP 调用 codemaker serve 的同步端点（见 CodemakerClient.prompt →
POST /session/{id}/message），让 LLM 解析用户自然语言意图为 NLIntent。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from .nl_parser import NLIntent
from ...codemaker_client import CodemakerClient, CodemakerClientConfig, PromptResult

logger = logging.getLogger(__name__)

_DEFAULT_MODEL = os.environ.get("CODEMAKER_MODEL", "")

_PARSE_SYSTEM_PROMPT = """你是精通 CA 配表规范、编表管线、字段语义的配表规范专家。作为意图解析器，分析用户的中文自然语言指令，输出一个 JSON 数组，每个元素是一条独立的操作指令。

## 专家准则
你理解配表领域：每张表有主键(通常首列 id)与外键关系(如 reward 引用 item.id)、列有类型(int/float/bool/str)与枚举白名单(如"品质"int 列对应中文枚举白/绿/蓝/紫/橙)、数值列有合理分布区间、名称列有命名规范(实体类别+"名称")。解析时结合语义：识别业务对象(灵兽/建筑/道具/英雄/神通/法宝/邮件/帮派)路由到正确表 stem；区分名称列(中文)与 id 引用列(int)；中文枚举值(如"紫")保留原文交后端转换，数字值保留数字；跨表新增引用时用 <new_id>/命名占位符避免编造 id。

## 关键规则
1. 复合操作拆分：仅当用户明确表达多个**独立动作**时才拆分（如"增加道具类型TEST1,增加道具类型TEST2"→2条）。若是一个动作的多个字段赋值（如"增加建筑名称为瞭望塔，赋值它的id是99999"），必须合并成**1条** add，所有字段放进同一个 fields。
2. 代词消解：用户语句中的"它""其""该"等代词指代当前正在操作的对象（如新增的行）。要把代词指代的字段正确归入当前指令的 fields，不要把代词后面的内容当成独立指令。
   - **新增+设置代词**：如"新增灵兽名称朱雀，然后设置它的成长率是2.0"——"它"指代刚新增的朱雀行，应合并为**1条 add**，fields={"灵兽名称":"朱雀","成长率":"2.0"}，**不要拆成 add+set 两条**。
3. 字段提取：每条指令必须提取完整的 {列名: 值} 映射放进 fields。列名用配表中的实际列名（如"建筑名称""建筑编号""id""攻击力"），值去掉引号和"为/是/="等连接词。**多字段必须全部提取**（如"新增灵兽，名称朱雀，品质3，成长率1.5"→fields 含 名称/品质/成长率三项，不能只取名称）。
4. **定位列名 locator_field**：当用户明确指出按哪一列定位时（如"删除神通id为3333"→locator_field="神通id"；"查询建筑名称为瞭望塔"→locator_field="建筑名称"；"把法宝id为1001的等级改为5"→locator_field="法宝id"），必须把该列名写入 locator_field。仅给对象名/值而未指明列名时（如"查询饕餮的所有信息"），locator_field 为 null。列名用配表实际列名，不要自造。
5. 必须只输出 JSON 数组，不要任何其他文字。
6. **列表对象名新增**：当用户用"增加/新增/添加 + 实体类别 + 多个对象名（顿号"、"或逗号分隔）"列举对象，且**未指明任何列名**时，按以下规则处理：
   - 实体类别（如"灵兽""宠物""建筑""道具""英雄"）后面紧跟第一个对象名时，对象名取实体类别之后到下一个分隔符之间的文本（如"增加灵兽子鼠、丑牛、银狐"中第一个对象名是"子鼠"，不是"灵兽子鼠"）。
   - 每个对象名拆成**1条独立的 add**，对象名写入该实体的主名称列。主名称列名规则：实体类别 + "名称"（如"灵兽"→"灵兽名称"，"建筑"→"建筑名称"，"道具"→"道具名称"，"英雄"→"英雄名称"）。table_hint 用实体类别（如"灵兽"→table_hint="pet"或"灵兽"，"建筑"→"building"）。
7. **实体→表路由**：table_hint 必须用实体类别对应的**英文表名 stem**，不要因对象名语义改表。**完整映射见上方【可用表路由】块（覆盖全部表，以其为准）**；常见示例：灵兽/宠物→"pet"，建筑→"building"，道具→"item"，英雄→"hero"，神通/技能→"ability"，法宝→"fabao"，邮件→"mail"，帮派→"guild"。例："新增灵兽朱雀"→table_hint="pet"（朱雀是灵兽对象名，不是 spirit 表）。
8. **缺失值**：若 set 指令缺少值（如"改为"后无内容），仍输出该字段，值给空字符串 ""，由后端校验拒绝；不要因此丢弃整条指令。
9. **枚举值转换**：如果用户用中文描述 int 类型列的值（如"品质紫""元素火"），不要判断对错，保留中文原文（如"紫""火"），后端会自动查枚举映射转换为数字。**注意：如果用户直接用数字（如"品质3"），则保留数字不变**。
10. **日期值识别**：日期/时间类列（如"活动开始时间""开服时间""发送时间"）的值，保留用户原文形式（如"2024年1月1日""2024/1/1""2024-01-01""2024-01-01 12:00:00"或时间戳），后端会自动归一化为 Excel 日期对象。不要尝试自行转换格式或判断合法性，原文照收。
11. **行号覆盖识别**：当用户在上一轮命中多行歧义后，本轮明确指定行号时（如"用行6""选行6""第6行""选第6行"），在指令中输出 `row_override` 字段为该行号整数（如 `row_override: 6`）。仅当用户显式以"行N/第N行"形式指定行号时才输出，普通指令不要输出该字段（置 null 或省略）。其余字段（table_hint/locator_value 等）仍按原规则解析，后端会用 row_override 跳过行定位直接读该行。
12. **跨表依赖占位符**：当一条指令要**新增一个实体**、后续指令又要在**另一张表**里引用这个刚新增实体的 ID（如"新增 NPC 铁匠老张…再在刷新表/对话表里引用该 NPC"），后续指令里引用该 ID 的字段值写占位符 `<new_id>`（不要自己编 ID）。后端编排器会把 `<new_id>` 替换为前一步实际产出的新 ID。示例见下方"跨表新增引用"。
    - **多实体链（命名占位符）**：当一条指令会新增**多个互相引用**的实体（如 NPC→对话→选项：NPC 引用 interaction_id、对话引用 conv_id、对话引用多个 option_id），用**命名占位符**区分：`<new_prefab_id>`、`<new_interaction_id>`、`<new_conv_id>`、`<option_1_id>`、`<option_2_id>`……并在**产出该 ID 的那条意图**上加 `produces` 字段标明它产出的占位符名（如新增对话那条 `"produces":"new_conv_id"`）。后端据 `produces` 做依赖拓扑排序 + 按名替换，因此**意图书写顺序可不受依赖约束**（生产者写在消费者后面也行）。不确定产出名时才回退单一 `<new_id>`。

## 操作类型 (action)
- set: 修改/设置数据（fields 含定位行要改的列+值，locator_value 定位行）
- add: 新增/添加数据行（fields 含新行所有字段，locator_value 为 null）
- col_add: 新增列（extras.col_name 为新列名）
- col_delete: 删除列（extras.col_name 为列名）
- col_rename: 重命名列（extras.col_name 旧名, extras.col_new_name 新名）
- delete: 删除数据（locator_value 定位行）
- get: 查询/查看数据（locator_value 定位行，fields 可空或指定要查的列）

## 输出格式（严格只输出 JSON 数组）
[{"action":"add","table_hint":"表名关键词或 null","sheet_hint":"sheet名提示或 null","locator_field":"定位列名或 null","locator_value":"定位值或 null","fields":{"列名":"值"},"extras":{"col_name":"列操作时的列名","col_new_name":"重命名时的新列名"},"produces":"本意图产出的命名占位符（多实体链才需，如 new_conv_id）或省略","row_override":null}]

## 示例
输入: "增加道具类型TEST1,增加道具类型TEST2"
输出: [{"action":"add","table_hint":"item","sheet_hint":null,"locator_value":null,"fields":{"道具类型":"TEST1"}},{"action":"add","table_hint":"item","sheet_hint":null,"locator_value":null,"fields":{"道具类型":"TEST2"}}]

输入: "增加灵兽子鼠、丑牛、银狐"
输出: [{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"子鼠"}},{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"丑牛"}},{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"银狐"}}]

输入: "增加建筑名称为瞭望塔，赋值它的id是99999"
输出: [{"action":"add","table_hint":"building","sheet_hint":"BuildingType","locator_value":null,"fields":{"建筑名称":"瞭望塔","id":99999}}]

输入(跨表新增引用): "新增NPC铁匠老张放到entity_prefab，再在spawn_world_entity里刷新它"
输出: [{"action":"add","table_hint":"entity_prefab","sheet_hint":null,"locator_value":null,"fields":{"实体名字":"铁匠老张"}},{"action":"add","table_hint":"spawn_world_entity","sheet_hint":null,"locator_value":null,"fields":{"实体Prefab ID":"<new_id>","实体名字":"铁匠老张"}}]

输入(多实体链·命名占位符): "新增NPC张三，点击弹出对话'你好'，选项'确定'"
输出: [{"action":"add","table_hint":"entity_prefab","sheet_hint":"Base","produces":"new_prefab_id","fields":{"entity_name":"张三","interaction_id":"<new_interaction_id>"}},{"action":"add","table_hint":"interaction","sheet_hint":"InteractionConv","produces":"new_conv_id","fields":{"对话内容":"你好","选项1":"<option_1_id>"}},{"action":"add","table_hint":"interaction","sheet_hint":"InteractionConvOption","produces":"option_1_id","fields":{"选项内容":"确定"}}]
（注意：选项**文本**写 InteractionConvOption 的"选项内容"列；InteractionConv 的"选项1/选项2"是**选项 ID 引用**（int），填 `<option_N_id>` 占位符，勿把文本写进"选项N"。）

输入: "把ability表里饕餮的攻击力改为200"
输出: [{"action":"set","table_hint":"ability","sheet_hint":null,"locator_value":"饕餮","fields":{"攻击力":"200"}}]

输入: "新增一个道具，名称是回复药水，品质是稀有"
输出: [{"action":"add","table_hint":"item","sheet_hint":null,"locator_value":null,"fields":{"名称":"回复药水","品质":"稀有"}}]

输入: "新增一个灵兽，名称朱雀，品质3，成长率1.5"
输出: [{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"朱雀","品质":"3","成长率":"1.5"}}]

输入: "新增灵兽名称朱雀，然后设置它的成长率是2.0"
输出: [{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"朱雀","成长率":"2.0"}}]

输入: "查询饕餮的所有信息"
输出: [{"action":"get","table_hint":null,"sheet_hint":null,"locator_field":null,"locator_value":"饕餮","fields":{}}]

输入: "删除神通id为3333的信息"
输出: [{"action":"delete","table_hint":"ability","sheet_hint":null,"locator_field":"神通id","locator_value":"3333","fields":{}}]

输入: "把法宝id为1001的等级改为5"
输出: [{"action":"set","table_hint":"fabao","sheet_hint":null,"locator_field":"法宝id","locator_value":"1001","fields":{"等级":"5"}}]

输入: "在assistant_ability这个表格中的AssistantAbility这个sheet中新增列名为TEST，并给已有行赋值为True"
输出: [{"action":"col_add","table_hint":"assistant_ability","sheet_hint":"AssistantAbility","locator_value":null,"fields":{},"extras":{"col_name":"TEST"}},{"action":"set","table_hint":"assistant_ability","sheet_hint":"AssistantAbility","locator_value":null,"fields":{"TEST":"True"}}]

输入: "用行6"
输出: [{"action":"get","table_hint":null,"sheet_hint":null,"locator_field":null,"locator_value":null,"fields":{},"row_override":6}]"""

_PARSE_MULTI_SYSTEM_PROMPT = _PARSE_SYSTEM_PROMPT


# ── R7: 自适应 Prompt 系统 ──────────────────────────────
from enum import Enum


class PromptMode(str, Enum):
    """操作模式分流，每模式用专用 prompt 精简 LLM 解析范围。"""
    QUERY = "query"     # 查询/查看/显示
    MODIFY = "modify"   # 改成/设为/改为/修改
    ADD = "add"         # 新增/添加/增加
    DELETE = "delete"   # 删除/移除/去掉
    AUTO = "auto"       # 无法归类，走完整 LLM 解析


def _try_rule_parse_multi(text: str) -> Optional[list]:
    """规则快速解析 ≤2 条简单意图，成功返回 NLIntent 列表，失败返回 None 降级 LLM。

    覆盖模式：
      - "新增XX名称YY" → add
      - "删除XX名称YY" → delete
      - "把XX名称YY的ZZ改为WW" → set
      - "增加道具类型TEST1,增加道具类型TEST2" → 2 adds（顿号/逗号分隔）
    不支持：跨表引用、代词消解、多字段 add、复合动作合并。这些走 LLM。
    """
    import re as _re
    from .nl_parser import NLIntent

    t = text.strip()
    # 拆分：按逗号/顿号/换行/分号切子句，忽略空子句
    parts = [p.strip() for p in _re.split(r'[,，、\n；;]+', t) if p.strip()]
    if len(parts) > 2:
        return None  # >2 条走 LLM

    results = []
    # 模式1: "新增/添加/增加 XX名称YY"（需要分隔词：名称/的/类型/id/编号）
    add_re = _re.compile(
        r'(?:新增|添加|增加|加一个|加一条)\s*'
        r'(?:一个|一条|个)?\s*'
        r'(.+?)(?:名称|的|类型|id|编号)\s*[是为：:]?\s*(.+)',
        _re.IGNORECASE
    )
    # 模式2: "删除/移除/去掉 XX名称YY"
    del_re = _re.compile(
        r'(?:删除|移除|去掉|删掉|清除)\s*'
        r'(?:一个|一条|个)?\s*'
        r'(.+?)(?:名称|id|编号)\s*[是为：:]?\s*(.+)',
        _re.IGNORECASE
    )
    # 模式3: "把/将 XX名称YY的ZZ改为/设为WW"
    set_re = _re.compile(
        r'(?:把|将)\s*(.+?)(?:名称|id|编号)\s*[是为：:]?\s*(.+?)的'
        r'([^\s，,]+?)\s*(?:改为|设为|改成|修改为|换成|调整|变为)\s*(.+)',
        _re.IGNORECASE
    )

    for part in parts:
        m = set_re.match(part)
        if m:
            results.append(NLIntent(
                raw=part, action="set",
                table_hint=m.group(1).strip() or None,
                locator_value=m.group(2).strip(),
                target_field=m.group(3).strip(),
                value=m.group(4).strip(),
                extras={"fields": {m.group(3).strip(): m.group(4).strip()}},
            ))
            continue
        m = del_re.match(part)
        if m:
            results.append(NLIntent(
                raw=part, action="delete",
                table_hint=m.group(1).strip() or None,
                locator_value=m.group(2).strip(),
            ))
            continue
        m = add_re.match(part)
        if m:
            fields = {}
            val = m.group(2).strip()
            hint = m.group(1).strip()
            # 尝试拆字段："名称朱雀，品质3" → {名称:朱雀, 品质:3}
            if '，' in val or ',' in val or '、' in val:
                field_parts = _re.split(r'[,，、]', val)
                for fp in field_parts:
                    fp = fp.strip()
                    kv = _re.split(r'[是为：:]', fp, maxsplit=1)
                    if len(kv) == 2:
                        fields[kv[0].strip()] = kv[1].strip()
                    else:
                        fields[hint + '名称'] = fp
            else:
                # 单值：实体名+名称列
                fields[hint + '名称'] = val if hint else val
            results.append(NLIntent(
                raw=part, action="add",
                table_hint=hint or None,
                extras={"fields": fields},
            ))
            continue
        return None  # 不匹配任何模式，降级 LLM

    return results if results else None


# 关键词集合：命中即归类（顺序无关，单次扫描分流）
_MODE_KEYWORDS: dict[PromptMode, tuple[str, ...]] = {
    PromptMode.QUERY: ("查询", "查看", "显示", "找一下", "列出", "是什么", "有哪些", "多少", "get", "show", "list", "find"),
    PromptMode.MODIFY: ("改成", "设为", "改为", "修改", "换成", "调整", "变为", "set ", "update", "change"),
    PromptMode.ADD: ("新增", "添加", "增加", "加一个", "加一条", "add ", "insert", "create"),
    PromptMode.DELETE: ("删除", "移除", "去掉", "删掉", "清除", "delete", "remove", "drop"),
}


def mode_classify(text: str) -> PromptMode:
    """关键词分流。命中单一操作语义→该模式；命中多种→复合，返回 AUTO。

    优先级（单一命中时）：DELETE > ADD > MODIFY > QUERY。
    复合指令（如"增加道具A，删除道具B"同时含 add/delete 语义）不强制归为
    单一模式——专用 prompt 会丢弃非本模式动作，故交 AUTO 完整 prompt 拆分。
    """
    if not text:
        return PromptMode.AUTO
    t = text.strip().lower()
    matched: list[PromptMode] = []
    for mode in (PromptMode.DELETE, PromptMode.ADD, PromptMode.MODIFY, PromptMode.QUERY):
        if any(kw in t for kw in _MODE_KEYWORDS[mode]):
            matched.append(mode)
    if not matched:
        return PromptMode.AUTO
    if len(matched) >= 2:
        return PromptMode.AUTO
    return matched[0]


# QUERY 专用 prompt：只解析表名+列名+行值，强制 action=get
_QUERY_PROMPT = """你是表格查询意图解析器。只解析查询类指令，输出 JSON 数组（每条一个独立查询）。

## 规则
1. action 固定为 "get"
2. 提取 table_hint（英文表名 stem，完整映射见上方【可用表路由】块，以其为准；常见：灵兽/宠物→pet，建筑→building，道具→item，英雄→hero，神通/技能→ability，法宝→fabao，邮件→mail，帮派→guild）
3. 提取 locator_value（查询的对象名/值，如"查询饕餮"→locator_value="饕餮"）。**对象名含阶/级/序号/数字/罗马后缀时必须保留完整名**（如"饕餮一阶"不可截成"饕餮"，"技能3""装备II"同理），否则定位失败
4. locator_field：用户明确指出按哪列查时填列名（如"查询建筑名称为瞭望塔"→locator_field="建筑名称"），否则 null
5. fields：用户指定要查的列时填 {列名: null}，否则空 {}
6. 只输出 JSON 数组，无其他文字

## 示例
输入: "查询饕餮的所有信息"
输出: [{"action":"get","table_hint":null,"sheet_hint":null,"locator_field":null,"locator_value":"饕餮","fields":{}}]
输入: "查看灵兽饕餮一阶的所有属性"
输出: [{"action":"get","table_hint":"pet","sheet_hint":null,"locator_field":null,"locator_value":"饕餮一阶","fields":{}}]
输入: "查看建筑名称为瞭望塔的等级"
输出: [{"action":"get","table_hint":"building","sheet_hint":null,"locator_field":"建筑名称","locator_value":"瞭望塔","fields":{"等级":null}}]
输入: "显示fabao表中spirit为gold的行"
输出: [{"action":"get","table_hint":"fabao","sheet_hint":null,"locator_field":"spirit","locator_value":"gold","fields":{}}]"""

# MODIFY 专用 prompt：解析表名+行定位+目标列+新值，强制 action=set
_MODIFY_PROMPT = """你是表格修改意图解析器。只解析修改类指令，输出 JSON 数组（每条一个独立修改）。

## 规则
1. action 固定为 "set"
2. 提取 table_hint（英文表名映射同 QUERY）
3. locator_field：用户指出的定位列名（如"法宝id为1001"→locator_field="法宝id"），未指明列名时 null
4. locator_value：定位值（如"1001"）
5. fields：要改的列+新值 {列名: 值}，多字段全部提取（如"把名称改为X，等级改为5"→fields={"名称":"X","等级":"5"}）
6. 值去掉引号和"为/是/="等连接词；枚举中文值保留原文（如"紫"），数字保留数字
7. row_override：用户以"行N/第N行"指定行号时填整数，否则 null
8. 只输出 JSON 数组，无其他文字

## 示例
输入: "把法宝id为1001的等级改为5"
输出: [{"action":"set","table_hint":"fabao","sheet_hint":null,"locator_field":"法宝id","locator_value":"1001","fields":{"等级":"5"},"row_override":null}]
输入: "将 fabao_type 为 attack 的法宝 spirit 改成 100"
输出: [{"action":"set","table_hint":"fabao","sheet_hint":null,"locator_field":"fabao_type","locator_value":"attack","fields":{"spirit":"100"},"row_override":null}]
输入: "用行6"
输出: [{"action":"set","table_hint":null,"sheet_hint":null,"locator_field":null,"locator_value":null,"fields":{},"row_override":6}]"""

# ADD 专用 prompt：解析表名+全部新列值，强制 action=add
_ADD_PROMPT = """你是表格新增意图解析器。只解析新增类指令，输出 JSON 数组（每条一个独立新增）。

## 规则
1. action 通常为 "add"；但请依指令语义判定真实动作（若实为修改/删除现有行则用 "set"/"delete"），**无法判定时才回退 "add"**
2. 提取 table_hint（英文表名映射同 QUERY）
3. locator_value 固定为 null（新增无定位）
4. fields：新行所有字段 {列名: 值}，多字段全部提取
5. 代词消解："它/其/该"指代正在新增的行，后续字段合并进同一 add（如"新增灵兽朱雀，设它成长率2.0"→1条 add，fields={"灵兽名称":"朱雀","成长率":"2.0"}）
6. 列表对象名新增："增加灵兽子鼠、丑牛"→拆成多条 add，每条对象名写入主名称列（实体类别+"名称"）
7. 主名称列名：灵兽→灵兽名称，建筑→建筑名称，道具→道具名称，英雄→英雄名称
8. 值去引号和连接词；枚举中文保留原文，数字保留数字
9. 只输出 JSON 数组，无其他文字

## 示例
输入: "新增一个灵兽，名称朱雀，品质3，成长率1.5"
输出: [{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"朱雀","品质":"3","成长率":"1.5"}}]
输入: "增加灵兽子鼠、丑牛、银狐"
输出: [{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"子鼠"}},{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"丑牛"}},{"action":"add","table_hint":"pet","sheet_hint":null,"locator_value":null,"fields":{"灵兽名称":"银狐"}}]
输入: "新增道具名称回复药水，品质稀有"
输出: [{"action":"add","table_hint":"item","sheet_hint":null,"locator_value":null,"fields":{"名称":"回复药水","品质":"稀有"}}]"""

# DELETE 专用 prompt：解析表名+行定位+是否级联，强制 action=delete
_DELETE_PROMPT = """你是表格删除意图解析器。只解析删除类指令，输出 JSON 数组（每条一个独立删除）。

## 规则
1. action 固定为 "delete"
2. 提取 table_hint（英文表名映射同 QUERY）
3. locator_field：用户指出的定位列名（如"神通id为3333"→locator_field="神通id"），未指明列名时 null
4. locator_value：定位值（如"3333"）
5. fields 固定为空 {}
6. row_override：用户以"行N/第N行"指定行号时填整数，否则 null
7. 只输出 JSON 数组，无其他文字

## 示例
输入: "删除神通id为3333的信息"
输出: [{"action":"delete","table_hint":"ability","sheet_hint":null,"locator_field":"神通id","locator_value":"3333","fields":{},"row_override":null}]
输入: "移除法宝名称为青莲宝色旗的行"
输出: [{"action":"delete","table_hint":"fabao","sheet_hint":null,"locator_field":"名称","locator_value":"青莲宝色旗","fields":{},"row_override":null}]
输入: "删行6"
输出: [{"action":"delete","table_hint":null,"sheet_hint":null,"locator_field":null,"locator_value":null,"fields":{},"row_override":6}]"""


_MODE_PROMPTS: dict[PromptMode, str] = {
    PromptMode.QUERY: _QUERY_PROMPT,
    PromptMode.MODIFY: _MODIFY_PROMPT,
    PromptMode.ADD: _ADD_PROMPT,
    PromptMode.DELETE: _DELETE_PROMPT,
    PromptMode.AUTO: _PARSE_SYSTEM_PROMPT,
}


class CodemakerNLParser:
    """基于 CodeMaker Serve API 的 NL 意图解析器。

    通过 HTTP 调用 codemaker serve，利用 LLM 解析用户意图。
    codemaker 不可用或解析失败时抛 RuntimeError（fail-fast）。

    依赖：codemaker serve 需先启动（codemaker serve --port 8666）
    """

    def __init__(self, client: CodemakerClient | None = None, model: str | None = None,
                 directory: str = "", enable_skill: bool = True):
        self.client = client or CodemakerClient()
        self.model = model or _DEFAULT_MODEL
        self.directory = directory
        self.enable_skill = enable_skill  # D9: skill 知识注入门控
        self._session_id: str = ""
        self._last_error_type: str = ""  # 最近一次 LLM 调用失败类型，供 parse 抛错时携带
        # 熔断：连续失败达阈值后跳过 LLM 调用，降级走规则路径，避免连环超时拖垮整批
        self._fail_count = 0
        self._circuit_threshold = max(1, int(os.getenv("CODEMAKER_PARSE_CIRCUIT_THRESHOLD", "2") or "2"))

    def _circuit_tripped(self) -> bool:
        """熔断是否开启（连续失败达阈值）。"""
        return self._fail_count >= self._circuit_threshold

    def _record_llm_outcome(self, ok: bool) -> None:
        """记录 LLM 调用结果：成功重置计数，失败递增。"""
        if ok:
            self._fail_count = 0
        else:
            self._fail_count += 1

    def _ensure_session(self) -> str:
        """确保有一个活跃的 codemaker 会话。失败抛 RuntimeError。

        directory 必须与 orchestrator 的分类会话一致传递——codemaker serve
        的 /api/session 在缺少 directory 时可能返回 400（Bad Request），
        此前未传导致 CRUD 解析阶段建会话必现 400，而分类阶段（有传 directory）
        正常，表现为"意图分类成功但 CRUD 执行异常"。

        异常带 error_type 属性，供 agent_service 映射具体中文提示。
        """
        if self._session_id:
            return self._session_id
        result = self.client.create_session(directory=self.directory, model=self.model)
        if result.ok:
            self._session_id = result.session_id
            return self._session_id
        err = RuntimeError(f"创建 codemaker 会话失败：{result.error}")
        err.error_type = getattr(result, "error_type", "")
        raise err

    def _bump_llm(self, site: str) -> None:
        """LLM 调用计数：inc + merge_to_instance，使心跳 peek_total 实时可见。

        agent.run 开头把 agent._llm_counter 下传到 parser._llm_counter；parse/
        parse_multi 经此打点。token 估算留 0（调用次数是心跳主指标）。
        """
        c = getattr(self, "_llm_counter", None)
        if c is None:
            return
        try:
            c.inc(site)
            c.merge_to_instance()
        except Exception:
            pass

    def parse(self, text: str, context: str = "") -> NLIntent:
        """使用 codemaker LLM 解析自然语言意图。

        R7：先 mode_classify 关键词分流，命中精简模式用专用 prompt 降低 LLM 负担。
        """
        if not text or not text.strip():
            return NLIntent(raw=text or "", action="get")

        self._last_error_type = ""
        mode = mode_classify(text)
        result = self._parse_via_llm(text, context, mode)
        if result is None:
            err = RuntimeError(f"codemaker 解析失败：{text!r}")
            err.error_type = self._last_error_type
            raise err
        return result

    def parse_multi(self, text: str, context: str = "",
                    error_feedback: str = "") -> list[NLIntent]:
        """使用 LLM 解析复合指令，输出多条 NLIntent。

        5.7/6.3：解析失败（超时/LLM 不可用/空结果）时**快速返回空列表**，不再
        抛异常——避免上层因异常再触发一次解析、白白多耗一个超时周期。调用方
        （`TableAgent.run`）见空列表即返回错误提示。失败类型仍存 `_last_error_type`。

        D3 retry-loop：error_feedback 非空时（写操作失败重试），把错误列+正确 schema
        反馈拼进 prompt，引导 LLM 重新产出 fields。

        性能优化：≤2 条简单意图走规则解析（零 LLM），跳过 90s 超时风险。
        """
        if not text or not text.strip():
            return [NLIntent(raw=text or "", action="get")]

        self._last_error_type = ""
        # 快速路径：≤2 条简单意图 → 规则解析，省一次 LLM 往返
        if not error_feedback:
            rule_results = _try_rule_parse_multi(text)
            if rule_results is not None:
                return rule_results
        mode = mode_classify(text)
        results = self._parse_multi_via_llm(text, context, mode, error_feedback=error_feedback)
        if not results:
            print(f"[CodemakerNLParser] 多指令解析失败（快速返回空）：{text!r}")
            return []
        return results

    @staticmethod
    def _build_prompt(text: str, context: str = "",
                      mode: PromptMode = PromptMode.AUTO) -> str:
        """按 mode 选专用 prompt：QUERY/MODIFY/ADD/DELETE 精简版，AUTO 完整版。

        R7 自适应：parse/parse_multi 先 mode_classify 分流，命中精简模式 →
        LLM 只解析该模式字段范围，负担轻、延迟低；AUTO 走完整通用 prompt。
        """
        prompt = _MODE_PROMPTS.get(mode, _PARSE_SYSTEM_PROMPT)
        if context:
            return f"{prompt}\n\n## 对话上下文（此前已完成的操作）\n{context}\n\n## 当前指令\n{text}"
        return f"{prompt}\n\n现在解析：{text}"

    # ── P1: skill 知识访问器（委托 skill_context，供解析阶段注入） ──
    @staticmethod
    def get_table_route() -> dict:
        """完整实体关键词 → 表名 stem 路由（替代 7 条硬编码）。"""
        from ..core.skill_context import get_table_route as _gtr
        return _gtr()

    @staticmethod
    def get_columns(stem: str, sheet: str = ""):
        """目标表真实列名（来自 _table_index.json）。"""
        from ..core.skill_context import get_columns as _gc
        return _gc(stem, sheet)

    @staticmethod
    def get_enums(stem: str) -> str:
        """目标表枚举值映射（LLM 可读文本）。"""
        from ..core.skill_context import get_enums as _ge
        return _ge(stem)

    def _build_prompt_with_skills(self, text: str, context: str = "",
                                  mode: PromptMode = PromptMode.AUTO,
                                  error_feedback: str = "") -> str:
        """P1：在基础 prompt 之上注入 skill 知识（表路由/列名/枚举/列类型 schema）+ few-shot 案例。

        先 skill 预解析算候选表，再把完整表路由 + 候选表列名 + 枚举作为 context
        拼进 prompt，让 LLM 在解析阶段即可正确路由、对齐列名、归一化枚举。
        注入失败（数据缺失/异常）时回退基础 prompt，保证解析不中断。

        D9: enable_skill=False 时跳过 skill_context 拼接，仅返回 base prompt
        （不含表路由/列名/列类型 schema/枚举映射提示），让 on/off 真 A/B。
        D3：error_feedback 非空时（写操作失败重试），在 prompt 末尾追加错误反馈块，
        引导 LLM 按正确 schema 重新产出 fields。
        N1：few-shot 案例注入——从 dialog_examples/dialog_failures 检索同表历史
        优秀/失败案例，作为解析示范拼进 prompt，让 LLM 参考相似输入的正确解析结构。
        """
        base = self._build_prompt(text, context, mode)
        if error_feedback:
            base = f"{base}\n\n## 错误反馈（上次产出失败，请修正后重新产出 fields）\n{error_feedback}"
        # D9: enable_skill 门控
        if not self.enable_skill:
            return base
        try:
            from ..core.skill_context import build_skill_context
            skill_ctx = build_skill_context(text)
        except Exception:
            logger.warning("skill_context 构建失败（已降级回退基础 prompt）", exc_info=True)
            skill_ctx = ""
        # N1: few-shot 案例注入（同表历史优秀/失败案例，解析示范）
        few_shot = self._build_few_shot_block(text)
        # 填表规则注入：rules/fill/*.md 用户手打知识，强约束拼进 prompt 前缀
        fill_rules = ""
        try:
            from ..core.rules_loader import load_fill_rules
            from ..core.skill_context import pre_route
            fill_rules = load_fill_rules(pre_route(text))
        except Exception:
            logger.warning("填表规则加载失败（已降级跳过）", exc_info=True)
            fill_rules = ""
        prefix = ""
        if skill_ctx:
            prefix = skill_ctx
        if few_shot:
            prefix = f"{prefix}\n\n{few_shot}" if prefix else few_shot
        if fill_rules:
            prefix = f"{prefix}\n\n{fill_rules}" if prefix else fill_rules
        if not prefix:
            return base
        return f"{prefix}\n\n{base}"

    def _build_few_shot_block(self, text: str) -> str:
        """N1：检索同表历史案例，构建 few-shot 示范块。

        从 dialog_examples（成功）+ dialog_failures（失败）检索，各取 top-2，
        提取 user_text + 正确解析摘要作为示范。失败案例标注"常见错误"警示。
        无案例数据或检索失败返回空串（不阻断解析）。
        """
        try:
            from ..core.skill_context import pre_route
            stems = pre_route(text)
            if not stems:
                return ""
            stem = stems[0]  # 取首选候选表
            from ..core.dialog_logger import get_dialog_logger
            dl = get_dialog_logger()
            examples = dl.query_examples(stem, limit=2, grade="excellent")
            failures = dl.query_examples(stem, limit=2, grade="failure")
            if not examples and not failures:
                return ""
            lines = ["## 历史案例参考（同表，供解析结构参考，勿照搬值）"]
            for ex in examples:
                if not isinstance(ex, dict):
                    continue
                ut = ex.get("user_text", "").strip()
                act = ex.get("intent_action", "")
                if not ut:
                    continue
                # 从 steps 提取操作摘要（定位→计划→校验→写入）
                steps = ex.get("steps") or []
                step_names = [s.get("name", "") for s in steps
                              if isinstance(s, dict)][:4]
                summary = " → ".join(step_names) if step_names else act
                lines.append(f"- ✅「{ut}」→ action={act} 流程:{summary}")
            for fa in failures:
                if not isinstance(fa, dict):
                    continue
                ut = fa.get("user_text", "").strip()
                act = fa.get("intent_action", "")
                if not ut:
                    continue
                msg = (fa.get("agent_message") or "").strip()
                # 失败案例只取首句作警示
                warn = msg.split("\n")[0][:80] if msg else "解析失败"
                lines.append(f"- ❌「{ut}」→ 常见错误：{warn}")
            return "\n".join(lines) if len(lines) > 1 else ""
        except Exception:
            logger.debug("few-shot失败（已降级）", exc_info=True)
            return ""

    @staticmethod
    def _fields_from_item(item: dict) -> dict:
        """从 LLM 输出的单条指令提取 fields 字典。

        优先用 fields 字段；兼容旧格式的 target_field/value（单字段）。
        """
        fields = item.get("fields") or {}
        if not isinstance(fields, dict):
            fields = {}
        # 兼容旧格式：target_field/value → fields
        tf = item.get("target_field")
        tv = item.get("value")
        if tf and tv is not None and tf not in fields:
            fields[tf] = tv
        return fields

    @staticmethod
    def _coerce_row_override(item: dict) -> Optional[int]:
        """从 LLM 输出提取 row_override（行号覆盖）。

        接受 int 或可解析为正整数的 str；非法值返回 None。
        """
        rv = item.get("row_override")
        if rv is None:
            return None
        if isinstance(rv, bool):
            return None
        if isinstance(rv, int):
            return rv if rv > 0 else None
        if isinstance(rv, str):
            s = rv.strip()
            if s.isdigit():
                return int(s)
        return None

    _VALID_ACTIONS = ("get", "set", "add", "delete")
    _ACTION_ALIASES = {
        "query": "get", "read": "get", "select": "get", "find": "get",
        "modify": "set", "update": "set", "change": "set", "edit": "set",
        "insert": "add", "create": "add", "new": "add",
        "remove": "delete", "del": "delete", "drop": "delete",
    }

    @classmethod
    def _validate_intent(cls, intent: "NLIntent") -> "NLIntent":
        """P3(3.4)：LLM 输出后校验 —— action 归一化 + table_hint 合法性降级。

        - action 非法 → 映射同义词，仍非法则降级为只读 "get"（安全兜底）。
        - table_hint 非合法 stem → 若能经路由映射为 stem 则替换；否则清空
          （降级为 null，让 _resolve_table 用 locator_value 等其它信号定位）。
        字段/列名不在此强校验（列匹配交执行阶段 ColumnMatcher 处理别名）。
        """
        act = (intent.action or "").strip().lower()
        if act not in cls._VALID_ACTIONS:
            act = cls._ACTION_ALIASES.get(act, "get")
        intent.action = act

        hint = (intent.table_hint or "").strip()
        if hint:
            try:
                from ..core.skill_context import known_stems, get_table_route
                stems = known_stems()
                if hint not in stems:
                    mapped = get_table_route().get(hint)
                    intent.table_hint = mapped if mapped in stems else None
            except Exception:
                logger.warning("table_hint 路由校验失败（已跳过 hint 归一化）", exc_info=True)
        # D2: 就近对 LLM 解析的 fields 做 match_best 校验+自纠（命中高置信度规范化列名，
        # 未命中标记 unresolved 不阻塞，交执行期升级）。降低对 LLM 输出质量敏感度。
        cls._validate_fields(intent)
        # locator 兜底：delete 缺 locator_value 但 fields 含主键候选（id/编号/xxx_id）时回填，
        # 避免 Step5 才 fail+进 verify-repair 浪费 LLM（早定位）。
        # set 不回填：fields 的 id 可能是要改的列（主键一般不改，但防误）。
        if act == "delete" and not (intent.locator_value or "").strip():
            _fields = intent.extras.get("fields") if isinstance(intent.extras, dict) else None
            if isinstance(_fields, dict):
                for k, v in list(_fields.items()):
                    if not isinstance(k, str):
                        continue
                    kl = k.lower()
                    sv = "" if v is None else str(v).strip()
                    if sv and (kl == "id" or "编号" in k or kl.endswith("_id")):
                        intent.locator_value = sv
                        if not (intent.locator_field or "").strip():
                            intent.locator_field = k
                        break
        return intent

    @classmethod
    def _validate_fields(cls, intent: "NLIntent") -> None:
        """就近校验 LLM 解析的 fields 列名并自纠。

        table_hint 解析为已知 stem 后，从 skill_context.get_columns 取该表列名，
        构造 ColumnMatcher 对每个 field key 调 match_best：
          - 命中且 score >= 0.85 → 规范化为真实列名（降低策略0 text.find 误命中）
          - 未命中 → 记入 extras["_unresolved_fields"] 供执行期升级
        失败静默（不阻断 parse）。
        """
        stem = (intent.table_hint or "").strip()
        if not stem:
            return
        extras = intent.extras if intent.extras is not None else {}
        fields = extras.get("fields")
        if not isinstance(fields, dict) or not fields:
            return
        try:
            from ..core.skill_context import get_columns
            from ..locator.column_matcher import ColumnMatcher
            sheets_cols = get_columns(stem)
            if not isinstance(sheets_cols, dict) or not sheets_cols:
                return
            # 选目标 sheet：sheet_hint 命中优先，否则首个有列的 sheet（并回填 sheet_hint）
            sheet_hint = (intent.sheet_hint or "").strip()
            target_cols = None
            if sheet_hint and sheets_cols.get(sheet_hint):
                target_cols = sheets_cols[sheet_hint]
            else:
                for sn, cols in sheets_cols.items():
                    if cols:
                        target_cols = cols
                        if not intent.sheet_hint:
                            intent.sheet_hint = sn
                        break
            if not target_cols:
                return
            matcher = ColumnMatcher(target_cols)
            normalized: dict = {}
            unresolved: list[str] = []
            changed = False
            for k, v in fields.items():
                m = matcher.match_best(k)
                if m is not None and m.score >= 0.85:
                    normalized[m.column] = v
                    if m.column != k:
                        changed = True
                else:
                    normalized[k] = v
                    if m is None:
                        unresolved.append(k)
            if changed:
                extras["fields"] = normalized
                intent.extras = extras
            if unresolved:
                extras["_unresolved_fields"] = unresolved
                intent.extras = extras
        except Exception:
            logger.warning("fields 就近校验失败（已跳过，交执行期处理）", exc_info=True)

    def _parse_via_llm(self, text: str, context: str = "",
                       mode: PromptMode = PromptMode.AUTO) -> NLIntent | None:
        """调用 codemaker serve API 做意图解析。

        兼容两种 LLM 响应：JSON 数组（取第一个元素）或 JSON 对象。
        """
        # 熔断：连续失败达阈值 → 跳过 LLM，降级走规则路径
        if self._circuit_tripped():
            return None
        sid = self._ensure_session()
        if not sid:
            return None

        prompt = self._build_prompt_with_skills(text, context, mode)
        resp = self.client.prompt(sid, prompt, model=self.model, stage="parse",
                                  cancel_event=getattr(self, "_cancel_event", None))
        self._bump_llm("parse")

        if not resp.ok:
            self._record_llm_outcome(False)
            self._last_error_type = getattr(resp, "error_type", "")
            print(f"[CodemakerNLParser] LLM 调用失败: {resp.error}")
            return None
        self._record_llm_outcome(True)

        # 从响应中提取 JSON
        data = self.client.extract_json_from_response(resp.response_text)
        if data is None:
            print(f"[CodemakerNLParser] 无法从响应提取 JSON: {resp.response_text[:200]}")
            return None

        # 兼容数组格式（取第一个元素）
        if isinstance(data, list) and len(data) > 0:
            data = data[0]

        if not isinstance(data, dict) or "action" not in data:
            return None

        intent = NLIntent(
            action=data.get("action", "get"),
            table_hint=data.get("table_hint"),
            sheet_hint=data.get("sheet_hint"),
            locator_field=data.get("locator_field"),
            locator_value=data.get("locator_value"),
            target_field=data.get("target_field"),
            value=data.get("value"),
            raw=text,
        )
        fields = self._fields_from_item(data)
        if fields:
            intent.extras["fields"] = fields
        ro = self._coerce_row_override(data)
        if ro is not None:
            intent.row_override = ro
        return self._validate_intent(intent)

    def _parse_multi_via_llm(self, text: str, context: str = "",
                             mode: PromptMode = PromptMode.AUTO,
                             error_feedback: str = "") -> list[NLIntent] | None:
        """调用 codemaker serve API 做多指令意图解析，返回意图列表。"""
        # 熔断：连续失败达阈值 → 跳过 LLM，降级走规则路径
        if self._circuit_tripped():
            return None
        sid = self._ensure_session()
        if not sid:
            return None

        prompt = self._build_prompt_with_skills(text, context, mode, error_feedback=error_feedback)
        # 复杂多意图（跨表 DAG）单次解析常需 >45s。用「单次长超时」替代
        # 「45s×2 次重试」：同样约 90s 墙钟预算下，两次 45s 都跑不完的解析，
        # 一次 90s 反而能完成（timeout 显式传值 → client 单次不重试）。
        # 默认拉到 180s：准确率优先，复杂跨表链（如 10 子任务）需更长思考时间。
        multi_timeout = int(os.environ.get("CODEMAKER_PARSE_MULTI_TIMEOUT", "180"))
        resp = self.client.prompt(sid, prompt, timeout=multi_timeout, model=self.model,
                                  cancel_event=getattr(self, "_cancel_event", None))
        self._bump_llm("parse_multi")

        if not resp.ok:
            self._record_llm_outcome(False)
            self._last_error_type = getattr(resp, "error_type", "")
            print(f"[CodemakerNLParser] LLM multi-parse 调用失败: {resp.error}")
            return None
        self._record_llm_outcome(True)

        # 从响应中提取 JSON 数组
        data = self.client.extract_json_from_response(resp.response_text)
        if data is None:
            print(f"[CodemakerNLParser] 无法从响应提取 JSON: {resp.response_text[:200]}")
            return None

        # 兼容两种格式：数组直接返回，单对象包装成数组
        items: list[dict] = []
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict) and "action" in data:
            items = [data]
        else:
            print(f"[CodemakerNLParser] 响应格式不符合预期: {data}")
            return None

        results: list[NLIntent] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            try:
                extras = item.get("extras", {}) or {}
                intent = NLIntent(
                    action=item.get("action", "get"),
                    table_hint=item.get("table_hint"),
                    sheet_hint=item.get("sheet_hint"),
                    locator_field=item.get("locator_field"),
                    locator_value=item.get("locator_value"),
                    target_field=item.get("target_field"),
                    value=item.get("value"),
                    raw=text,
                )
                fields = self._fields_from_item(item)
                if fields:
                    intent.extras["fields"] = fields
                if isinstance(extras, dict):
                    for k, v in extras.items():
                        if v is not None:
                            intent.extras[k] = v
                # produces：多实体链的命名占位符标签（顶层字段），供编排器拓扑排序
                prod = item.get("produces")
                if isinstance(prod, str) and prod.strip():
                    intent.extras["produces"] = prod.strip()
                ro = self._coerce_row_override(item)
                if ro is not None:
                    intent.row_override = ro
                results.append(self._validate_intent(intent))
            except Exception as e:
                # 单条解析异常不阻断其余指令
                print(f"[CodemakerNLParser] 跳过异常指令项: {e}; item={str(item)[:120]}")
                continue
        return results if results else None

    def ask(self, question: str) -> str:
        """通用 LLM 问答接口（非解析模式，获得完整文本回复）。

        用于需要 AI 推理但不需要结构化的场景（如 merge 建议、数据校验说明）。
        """
        sid = self._ensure_session()
        resp = self.client.prompt(sid, question, model=self.model,
                                  cancel_event=getattr(self, "_cancel_event", None))
        if not resp.ok:
            err = RuntimeError(f"codemaker 调用失败：{resp.error}")
            err.error_type = getattr(resp, "error_type", "")
            raise err
        return resp.response_text

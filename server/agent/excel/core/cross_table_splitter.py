"""跨表数据结构与检测信号模块。

保留内容（数据结构 / 数据驱动，非硬编码模板）：
  1. `SplitIntent` —— 拆分后独立意图的通用数据容器，被 decompose_agent.py、
     school_chain_expander.py 及多处 pipeline/测试代码复用。
  2. `detect_cross_table_action` / `_load_cross_table_keywords` / `CROSS_TABLE_TYPES`
     —— 跨表模式检测信号，供 multi_intent_splitter.py 防止把 NPC/进化链等跨表
     整段切碎，供 locator_agent.py 做 LLM 路由分类值校验。
  3. `get_cascade_hints` —— 委托 cascade_resolver 的数据驱动级联提示查询接口。

§已删除内容：原有的 11 个硬编码模板生成函数（`_build_*_intents` 等，基于业务
关键词正则拆字段生成具体表字段），因其属于业务模板硬编码而非通用数据结构，
已按批准范围整体移除。相关生产调用点默认由
`CODEMAKER_DECOMPOSE_DISABLE_TEMPLATE_FALLBACK` 环境变量关闭（默认值 "1"），
移除对默认生产行为无影响。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class SplitIntent:
    """拆分后的独立意图"""
    text: str                              # 描述文本
    table_hint: str                        # 目标表 stem
    sheet_hint: Optional[str] = None       # 目标 sheet
    action: str = "add"                    # 操作类型
    fields: dict[str, str] = field(default_factory=dict)  # 字段映射
    locator_value: Optional[str] = None    # 行定位值（set/delete/get 时用）
    locator_field: Optional[str] = None    # 行定位列名（set/delete/get 时用，如 "id"）
    # 复合主键（如 (residence_id, obstacle_id)）。非空时优先于单值。
    locator_values: list[str] = field(default_factory=list)
    locator_fields: list[str] = field(default_factory=list)
    produces: Optional[str] = None         # 产出的命名占位符（供编排器拓扑，如 new_interaction_id）
    # §A Outline Planner：所属 operation 的稳定标识（outline 阶段分配），供
    # Step1~Step4 按 op 归因/回灌。为空表示未走 outline 阶段（旧路径兼容）。
    op_id: Optional[str] = None


def _load_cross_table_keywords() -> dict:
    """从 parser_config.yaml 加载跨表词表。失败回退默认(同原硬编码)，保证零回归。"""
    default = {
        "add_prefix": ["新增", "增加", "添加", "加一个", "加一件", "加一条", "加一颗", "加一本", "加一件新"],
        "entity_types": ["NPC", "商人", "守卫", "传送员", "传送使者", "导师", "教习", "任务NPC", "对话NPC"],
        "pet_types": ["灵兽", "宠物", "召唤兽"],
        "item_types": ["武器", "药品", "法宝", "装备", "道具", "防具", "饰品"],
        "mail_types": ["邮件", "邮件模板"],
        "quest_types": ["支线", "主线", "日常", "环式"],
        "school_types": ["神通", "门派神通"],
        "quantity": ["把", "个", "件", "颗", "本", "条", "支", "张"],
    }
    try:
        from pathlib import Path as _P
        import yaml as _yaml
        cfg = _P(__file__).resolve().parent.parent / "skills" / "parser_config.yaml"
        if not cfg.exists():
            return default
        with open(cfg, encoding="utf-8") as f:
            raw = _yaml.safe_load(f) or {}
        ct = raw.get("cross_table") or {}
        if not isinstance(ct, dict) or not ct:
            return default
        merged = dict(default)
        for k, v in ct.items():
            if isinstance(v, list):
                merged[k] = v
        return merged
    except Exception:
        return default


_CT_KW = _load_cross_table_keywords()


#: detect_cross_table_action 全部合法分类值（供 route 校验，防 LLM 幻觉类型名）
CROSS_TABLE_TYPES = (
    "pet", "evolve", "npc_dialogue", "npc_teleport", "npc_combat",
    "npc_reward", "npc_composite", "item", "mail", "quest",
    "school_ability_spell", "combat_reward", "residence_building",
)


def detect_cross_table_action(text: str, route: Optional[dict] = None) -> Optional[str]:
    """检测文本中是否包含跨表操作关键词。

    §系统性重构 Phase1（LLM 主导，规则兜底）：route 由上层 LocatorAgent
    ._llm_classify_route 产出的 LLM 分类结果（含 cross_table_type 字段）。
    route.ok=True 且 cross_table_type 合法时直接采信 LLM 判断（含"该判定为
    None"的情形——LLM 判它不是跨表操作也算数），跳过下方全部正则分支。
    route=None 或 route.ok=False 或 cross_table_type 值非法（幻觉类型名）时
    走原正则判定（不删规则，纯降级兜底）。

    此举修复正则硬编码同义词覆盖不全的问题（如"放一个NPC"因"放"字不在
    has_entity 动作词白名单漏判 npc_dialogue，掉进通用 LLM 拆分多层串行
    兜底导致卡 3-4 分钟）。

    Returns:
        "evolve" - 进化链操作
        "npc_dialogue" - NPC+对话+选项 多表新增
        "npc_teleport" - 传送 NPC（点击后传送/坐标传送）
        "npc_combat" - 战斗 NPC（擂台/战斗/挑战）
        "npc_reward" - 奖励 NPC（奖励/获得）
        "npc_composite" - 复合 NPC（对话+选项+奖励+邮件 全链路）
        None - 无跨表操作
    """
    if route and route.get("ok"):
        ct = route.get("cross_table_type")
        if ct is None or ct in CROSS_TABLE_TYPES:
            return ct
        # 非法类型名（LLM 幻觉）：忽略 route，走下方规则兜底
    # pet 灵兽新增（含资质/元素/进化）优先于 evolve（纯进化链）
    # "新增灵兽...进化路径"应走 pet 模式产出完整属性，evolve 仅处理"A进化成B"纯链
    if re.search(r'新增(?:一只|一个)?(?:' + '|'.join(_CT_KW['pet_types']) + r')', text):
        return "pet"
    if re.search(r'进化[成为到]|进化链|进化路径', text):
        return "evolve"
    # 实体类型扩展：NPC / 商人 / 守卫 / 传送员 / 接任务的人 等可交互实体
    # 优先于 item 模式判定（"道具商人"含"道具"但本质是 NPC，需先判实体）
    # §对话树段修复：动作词扩到含"配/再配/配一个"（与 _ACTION_PATTERNS 一致），
    # 否则"再配一个引导 NPC...对话...选项"因"配"不匹配 has_entity=False → 漏命中
    # npc_dialogue 模板 → 走 _splitter_baseline 兜底产 fields 空 → "无法解析"。
    _ent = r'(?:' + '|'.join(_CT_KW['entity_types']) + r')'
    has_entity = bool(re.search(
        r'(?:新增|增加|添加|配|再配|建一个|建个|造一个)[^。；;]{0,20}?' + _ent,
        text))
    if has_entity and '对话' in text and '选项' in text:
        if ('奖励' in text or 'reward_id' in text) and ('邮件' in text or 'mail' in text.lower()):
            return "npc_composite"
        return "npc_dialogue"
    if has_entity:
        if re.search(r'传送|传送到|坐标传送|传送使者', text):
            return "npc_teleport"
        if re.search(r'擂台|战斗|挑战|进入战斗', text):
            return "npc_combat"
        if re.search(r'奖励|获得', text):
            return "npc_reward"
    # ── 纯类型新增模式（无 NPC 实体头部）──
    # 触发词放宽：兼容「加/加一个/加一件/加一条/新增/增加/添加」等口语化表述
    _ADD_PREFIX = r'(?:' + '|'.join(_CT_KW['add_prefix']) + r')'
    _qty = r'(?:' + '|'.join(_CT_KW['quantity']) + r')'
    _item = r'(?:' + '|'.join(_CT_KW['item_types']) + r')'
    _mail = r'(?:' + '|'.join(_CT_KW['mail_types']) + r')'
    _quest = r'(?:' + '|'.join(_CT_KW['quest_types']) + r')'
    _school = r'(?:' + '|'.join(_CT_KW['school_types']) + r')'
    # item：武器/药品/法宝/装备 等跨 sheet 同表新增
    # 量词允许"一/二/两"+把/个/件（"新增一把武器"/"新增一个药品"原漏匹配 → 走 LLM 路由错 sheet）
    if re.search(_ADD_PREFIX + rf'(?:一?{_qty})?{_item}', text):
        return "item"
    # mail：邮件模板+全服邮件（"新增一封全服邮件"/"新增邮件模板" 原仅认"邮件模板"漏匹配）
    # mail_types 含 邮件/邮件模板，第一分支匹配含"邮件"子串（含"邮件模板"），原第三分支冗余合并
    if re.search(_ADD_PREFIX + rf'(?:一?(?:个|封))?(?:全服|全局)?{_mail}', text) or \
       re.search(r'mail_id\s*\d', text):
        return "mail"
    # quest：支线/主线/日常/环式任务
    if re.search(_ADD_PREFIX + rf'(?:一个)?{_quest}?(?:打怪)?任务', text):
        return "quest"
    # school_ability：神通+神通等级+技能组+法术 四表联动
    if re.search(_ADD_PREFIX + rf'(?:一个)?新?{_school}', text) or ('神通编号' in text and '技能组' in text):
        return "school_ability_spell"
    # combat+reward：战斗+奖励包联动（"打赢给奖励包"暗示需建战斗和奖励表）
    if ('战斗' in text and '奖励包' in text and '战场' in text) or \
       ('配成在战场' in text and '赢了给' in text):
        return "combat_reward"
    # residence_building：建筑类型+实例+交互动画
    # F2: 放宽检测关键词 → 命中模板跳过 DecomposeAgent 风暴（近零 LLM）
    if ('洞府' in text and ('建筑类型' in text or '功能建筑' in text or '建筑类别' in text)) \
       or '种田' in text or '炼丹房' in text or '炼造' in text \
       or ('建筑实例' in text and '图纸道具' in text):
        return "residence_building"
    return None


# ── 级联数据驱动（关系图谱查询，供未命中模式时 LLM 上下文增强）─────

def get_cascade_hints(stem: str) -> dict:
    """查询 add stem 表时的级联提示（基于 table_relations.json 关系图谱）。

    数据驱动接口：替代原硬编码 _build_* 模板对已知模式（NPC/item/pet/mail/quest）
    的字段覆盖，未命中模式时上层可调本函数获取关系图谱级联提示，注入 LLM
    上下文辅助拆分多 op。

    返回:
      {depends_on: [{target_stem, source_col, target_col, sheet}],  # add 此表时需已存在
       referenced_by: [{source_stem, source_col, target_col, sheet}]} # add 此表后可能需同步建
    """
    from .cascade_resolver import get_cascade_hints as _get
    return _get(stem)

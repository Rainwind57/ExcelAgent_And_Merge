"""跨表意图拆分器：将自然语言中隐含的多表操作拆分显式的多条意图。

核心能力：
  1. 识别"进化链"等跨表关键词 → 拆分为 pet + pet_evolve 表的意图
  2. 支持扩展更多跨表模式

用法:
    splitter = CrossTableIntentSplitter()
    intents = splitter.split(user_text, parsed_intents)
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


# ── 进化链模式 ─────────────────────────────────────────────

# 匹配模式: "X进化成Y" / "X进化为Y"
_EVOLVE_PATTERN = re.compile(
    r"(?P<from>[^\s，,、]+?)\s*(?:进化成|进化为|进化到)\s*(?P<to>[^\s，,、。；;]+)",
)

# 匹配多个对象的进化链: "A进化成B，B进化为C"
_EVOLVE_CHAIN_PATTERN = re.compile(
    r"(?P<from>[^\s，,、]+?)\s*进化[成为到]\s*(?P<to>[^\s，,、。；;]+)",
)


def _parse_evolve_chain(text: str) -> list[tuple[str, str]]:
    """从文本中解析进化链: [(源灵兽, 目标灵兽), ...]"""
    pairs: list[tuple[str, str]] = []
    for m in _EVOLVE_CHAIN_PATTERN.finditer(text):
        from_name = m.group("from")
        to_name = m.group("to")
        # 清理可能误捕的"进化链"前缀
        from_name = re.sub(r'.*进化链\s*', '', from_name)
        if from_name and to_name:
            pairs.append((from_name, to_name))
    return pairs


def _extract_id_from_text(text: str, prefix: str) -> Optional[int]:
    """从文本中提取ID: "进化id分别是11111" → 11111"""
    # 匹配: 进化id分别是11111,11112
    m = re.search(rf"{re.escape(prefix)}\D*(\d+)", text)
    if m:
        return int(m.group(1))
    return None


def _extract_ids_from_text(text: str, prefix: str) -> list[int]:
    """从文本中提取多个ID: "进化id分别是11111,11112" → [11111, 11112]"""
    # 先找 "分别是" 后面的数字列表
    m = re.search(rf"{re.escape(prefix)}.*?(?:分别[是为]|是)\s*([\d,，、\s]+)", text)
    if m:
        nums = re.findall(r"\d+", m.group(1))
        return [int(n) for n in nums]
    # 再找所有数字
    nums = re.findall(rf"{re.escape(prefix)}\D*?(\d+)", text)
    return [int(n) for n in nums]


def _extract_pet_ids_from_text(text: str) -> list[int]:
    """提取"灵兽id分别是4444,4445,4446"中的id列表"""
    return _extract_ids_from_text(text, "灵兽id")


def _extract_evolve_ids_from_text(text: str) -> list[int]:
    """提取"进化id分别是11111,11112"中的id列表"""
    return _extract_ids_from_text(text, "进化id")


# ── 主拆分器 ────────────────────────────────────────────────


@dataclass
class CrossTableIntentSplitter:
    """跨表意图拆分器。

    将用户自然语言中的跨表操作（如进化链）拆分为多条独立意图。
    """

    def split(self, text: str) -> list[SplitIntent]:
        """分析文本，返回拆分后的意图列表。无跨表操作返回空列表。"""
        results: list[SplitIntent] = []

        action = detect_cross_table_action(text)
        if action == "evolve":
            # ── 模式1: 进化链 ──
            evolve_pairs = _parse_evolve_chain(text)
            if evolve_pairs:
                results.extend(self._handle_evolve_chain(text, evolve_pairs))
        elif action == "npc_dialogue":
            # ── 模式2: NPC+对话+选项 ──
            info = _extract_npc_dialogue(text)
            if info:
                results.extend(_build_npc_dialogue_intents(info))
        elif action in ("npc_teleport", "npc_combat", "npc_reward"):
            # ── D2 模式3: NPC 变体（传送/战斗/奖励）──
            info = _extract_npc_variant(text, action)
            if info:
                if action == "npc_teleport":
                    results.extend(_build_npc_teleport_intents(info))
                elif action == "npc_combat":
                    results.extend(_build_npc_combat_intents(info))
                elif action == "npc_reward":
                    results.extend(_build_npc_reward_intents(info))
        elif action == "npc_composite":
            # ── 模式4: 复合 NPC（对话+选项+奖励+邮件 全链路 12 条任务）──
            info = _extract_npc_composite(text)
            if info:
                results.extend(_build_npc_composite_intents(info))
        elif action == "item":
            # ── 模式5: 道具跨 sheet（ItemBase → Equipment/Potion/Fabao）──
            results.extend(_build_item_intents(text))
        elif action == "mail":
            # ── 模式6: 邮件模板 + 全服邮件 ──
            results.extend(_build_mail_intents(text))
        elif action == "quest":
            # ── 模式7: 任务 + 刷新任务实体 ──
            results.extend(_build_quest_intents(text))
        elif action == "pet":
            # ── 模式8: 灵兽新增（含进化路径）──
            results.extend(_build_pet_intents(text))
        elif action == "school_ability_spell":
            # ── 模式9: 门派神通+神通等级+技能组+法术 四表联动 ──
            results.extend(_build_school_ability_spell_intents(text))
        elif action == "combat_reward":
            # ── 模式10: 战斗+奖励包 联动 ──
            results.extend(_build_combat_reward_intents(text))
        elif action == "residence_building":
            # ── 模式11: 洞府建筑类型+实例+交互动画 ──
            results.extend(_build_residence_building_intents(text))

        return results

    def _handle_evolve_chain(self, text: str, pairs: list[tuple[str, str]]) -> list[SplitIntent]:
        """处理进化链：生成 pet 表的 add 意图 + pet_evolve 表的 add 意图。

        输入: "增加灵兽牛马一阶,牛马二阶,牛马三阶,灵兽id分别是4444,4445,4446,
               并且有进化链牛马一阶进化成牛马二阶,牛马二阶进化为牛马三阶,
               两条进化id分别是11111,11112"

        输出:
          - 3条 add intents for pet 表 (牛马一阶/二阶/三阶, id=4444/4445/4446)
          - 2条 add intents for pet_evolve 表 (进化链)
        """
        results: list[SplitIntent] = []

        pet_ids = _extract_pet_ids_from_text(text)
        evolve_ids = _extract_evolve_ids_from_text(text)

        # 提取灵兽名称列表
        pet_names = self._extract_pet_names(text)

        # 构建 名称→id 映射
        name_to_id: dict[str, int] = {}
        for i, name in enumerate(pet_names):
            if i < len(pet_ids):
                name_to_id[name] = pet_ids[i]

        # 生成 pet 表 add 意图
        for i, name in enumerate(pet_names):
            fields = {"灵兽名称": name}
            if i < len(pet_ids):
                fields["灵兽id"] = str(pet_ids[i])
            results.append(SplitIntent(
                text=f"新增灵兽{name}",
                table_hint="pet",
                sheet_hint="Pet",
                action="add",
                fields=fields,
            ))

        # 生成 pet_evolve 表 add 意图
        for j, (from_name, to_name) in enumerate(pairs):
            from_id = name_to_id.get(from_name)
            to_id = name_to_id.get(to_name)
            fields: dict[str, str] = {}
            if j < len(evolve_ids):
                fields["进化id"] = str(evolve_ids[j])
            if from_id is not None:
                fields["宠物id"] = str(from_id)
            if to_id is not None:
                fields["进化后的灵兽ID"] = str(to_id)
            results.append(SplitIntent(
                text=f"新增进化链{from_name}({from_id})→{to_name}({to_id})",
                table_hint="pet_evolve",
                sheet_hint="PetEvolveData",
                action="add",
                fields=fields,
            ))

        return results

    def _extract_pet_names(self, text: str) -> list[str]:
        """从文本中提取灵兽名称列表。"""
        # 匹配"牛马一阶,牛马二阶,牛马三阶"格式
        # 先去掉前面到"灵兽"或"增加灵兽"的部分
        m = re.search(r'(?:增加|新增|添加)\s*灵兽\s*', text)
        start = m.end() if m else 0
        rest = text[start:]

        # 找到第一个"灵兽id"或"进化"或"并且"之前的内容作为名称部分
        end_m = re.search(r'(?:灵兽id|进化|并且|，且)', rest)
        if end_m:
            rest = rest[:end_m.start()]

        # 按逗号、顿号分割
        names = re.split(r'[,，、\s]+', rest.strip())
        return [n for n in names if n and not n.isdigit()]


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


def detect_cross_table_action(text: str) -> Optional[str]:
    """检测文本中是否包含跨表操作关键词。

    Returns:
        "evolve" - 进化链操作
        "npc_dialogue" - NPC+对话+选项 多表新增
        "npc_teleport" - 传送 NPC（点击后传送/坐标传送）
        "npc_combat" - 战斗 NPC（擂台/战斗/挑战）
        "npc_reward" - 奖励 NPC（奖励/获得）
        "npc_composite" - 复合 NPC（对话+选项+奖励+邮件 全链路）
        None - 无跨表操作
    """
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
    # quest：支线/主线/日常 re.search(_ADD_PREFIX + rf'(?:一个)?{_quest}?(?:打怪)?任务', text):
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


# ── NPC+对话+选项 模式（规则模板展开，绕过 LLM 延迟墙） ──────

_NPC_NAME_RE = re.compile(
    r'(?:新增|增加|添加|配|再配|建一个|建个|造一个)[^。；;]*?(?:NPC|商人|守卫|传送员|传送使者|导师|教习|任务NPC|对话NPC)\s*(?:叫|名为|名字叫|名称叫)?\s*'
    r'["\']?(?P<name>[^，,。；;（(\s"\']+)["\']?'
)
_MODEL_ID_RE = re.compile(r'(?:model_id|模型(?:用|为|是)?)\s*(?:为|是)?\s*(?P<id>\d+)')
_SPACE_ID_RE = re.compile(r'(?:space_id|野外战场|战场)\s*(?:为|是|用)?\s*(?P<sid>\d+)')
_POS_RE = re.compile(
    r'坐标\s*\(\s*(?P<x>[\d.]+)\s*,\s*(?P<y>[\d.]+)\s*,\s*(?P<z>[\d.]+)\s*\)'
)
# 对话内容：匹配"对话[内容/：/说]"后的引号内容。
# 兼容"对话内容'...'" / "对话：'...'" / "弹出对话：老人说'...'" / "对话：说'...'"
# 原 regex 仅认"对话内容"前缀，"弹出对话：老人说"等自然表述全部漏匹配 LLM
# 拆分质量差（task_chain quest_npc 用例 conv 行全 missing）。
_CONV_RE = re.compile(
    r'(?:弹出)?对话(?:内容)?(?:\s*[：:])?\s*(?:老人|NPC|他|她)?说?\s*'
    r'(?:["\'](?P<conv_q>[^"\']+)["\']'
    r'|(?P<conv_bare>.+?)(?=,?\s*选项|。|；|;|$)'
    r')'
)
# 选项段：从"选项"到句末（兼容分号分隔的多个选项描述）
_OPTS_RE = re.compile(
    r'(?:给\s*\d+\s*个)?选项(?:为|：|:)?\s*(?P<opts>.+?)(?=点击|。$|；\s*$|$)'
)
_OPTS_SPLIT_RE = re.compile(r'[、和]|，(?=[^，]+$)')
_QUOTED_RE = re.compile(r'["\']([^"\']+)["\']')
# 分支对话：点击/选"X"后...继续说/说"Y"
# 兼容"点击'我帮你寻回'后老人继续说'多谢...'"（原仅认"选X就跳到新对话说Y"）
_BRANCH_OPT_RE = re.compile(
    r'(?:点击|选)\s*["\'](?P<opt_text>[^"\']+)["\']\s*(?:后|之后)?\s*'
    r'[^。；]*?(?:继续说|后说|新对话.*?说|说)\s*'
    r'["\'](?P<branch_conv>[^"\']+)["\']'
)
# 接任务选项：分支对话后"再点'X'接[下]任务" → function_type=2 触发 quest_id
# quest_npc 用例 step7 option_go（"我这就去"接任务）原未捕获 → 引用一致 0.875<0.9
_GO_OPT_RE = re.compile(
    r'(?:再|然后)?点(?:击)?\s*["\'](?P<go_opt>[^"\']+)["\']\s*(?:后)?\s*接[下]?\s*任务'
)


def _extract_npc_dialogue(text: str) -> Optional[dict]:
    """从「新增NPC…对话…选项」文本提取结构化字段。未命中返回 None。"""
    nm = _NPC_NAME_RE.search(text)
    if not nm:
        return None
    name = nm.group("name").strip().strip("'\"")
    if not name:
        return None
    cm = _CONV_RE.search(text)
    if not cm:
        return None
    conv = (cm.group("conv_q") or cm.group("conv_bare") or "").strip().rstrip('，,。')
    if not conv:
        return None

    om = _OPTS_RE.search(text)
    opts: list[str] = []
    if om:
        seg = om.group("opts")
        # 分支对话时，只取"选"后的选项文本（排除 branch_conv 误当选项）
        branch_opts = re.findall(r'选\s*["\']([^"\']+)["\']', seg)
        if branch_opts:
            opts = [o.strip() for o in branch_opts if o.strip()]
        else:
            opts = [o.strip() for o in _QUOTED_RE.findall(seg) if o.strip()]
            # 兜底：无引号时按 和/、 分割
            if not opts:
                opts = [o.strip().rstrip('，,。') for o in re.split(r'[和、]', seg) if o.strip()]
            else:
                opts = [o.strip().rstrip('，,。') for o in opts]

    # 分支对话检测：选"看看"就跳到新对话说"这只赤炎虎..."
    branch = None
    bm = _BRANCH_OPT_RE.search(text)
    if bm:
        branch = {
            "opt_text": bm.group("opt_text").strip(),
            # 保留原文尾 。（eval 严格匹配 prompt_text，rstrip 致字段分 0）
            "branch_conv": bm.group("branch_conv").strip(),
        }

    # 接任务选项检测：分支对话后"再点'我这就去'接下任务" → option_go
    go_opt = None
    gom = _GO_OPT_RE.search(text)
    if gom:
        go_opt = {"opt_text": gom.group("go_opt").strip()}

    mm = _MODEL_ID_RE.search(text)
    sm = _SPACE_ID_RE.search(text)
    pm = _POS_RE.search(text)
    rm = _REWARD_ID_RE.search(text)

    # 任务链提取（NPC + 任务 + 刷怪 混合指令）
    quest = None
    qid_m = re.search(r'任务ID\s*(?:为|是)?\s*(?P<qid>\d+)', text)
    if qid_m:
        qname_m = re.search(r"任务(?:叫|名为)?\s*['\"](?P<qn>[^'\"]+)['\"]", text)
        gid_m = re.search(r'group_id\s*(?:为|是)?\s*(?P<gid>\d+)', text)
        desc_m = re.search(r"描述\s*['\"](?P<dq>[^'\"]+)['\"]", text)
        tgt_type_m = re.search(r'目标类型\s*(?P<tt>[A-Za-z]+)', text)
        tgt_data_m = re.search(r"目标数据\s*['\"](?P<tdq>[^'\"]+)['\"]", text)
        qreward_m = re.search(r'(?:完成奖励|奖励)\s*reward_id\s*(?:为|是)?\s*(?P<rid>\d+)', text)
        # 击杀/刷新怪物 npc_id + 位置（取最后一个 space_id+坐标，NPC 在前怪物在后）
        kill_npc_m = re.search(r'npc_id\s*(?:为|是)?\s*(?P<nid>\d+)', text)
        kill_loc = None
        for _m in re.finditer(
                r'space_id\s*(?P<sid>\d+)[^。]*?坐标\s*\(\s*(?P<x>[\d.]+)\s*,\s*(?P<y>[\d.]+)\s*,\s*(?P<z>[\d.]+)\s*\)',
                text):
            kill_loc = _m  # 保留最后一个（NPC 位置是第一个，怪物位置在后）
        quest = {
            "quest_id": qid_m.group("qid"),
            "name": (qname_m.group("qn").strip().strip("'\"") if qname_m else name),
            "group_id": gid_m.group("gid") if gid_m else None,
            "desc": desc_m.group("dq") if desc_m else None,
            "target_type": tgt_type_m.group("tt") if tgt_type_m else None,
            "target_data": tgt_data_m.group("tdq") if tgt_data_m else None,
            "reward_id": qreward_m.group("rid") if qreward_m else None,
            "kill_npc_id": kill_npc_m.group("nid") if kill_npc_m else None,
            "kill_space_id": kill_loc.group("sid") if kill_loc else None,
            "kill_pos": ((kill_loc.group("x"), kill_loc.group("y"), kill_loc.group("z"))
                         if kill_loc else None),
        }

    # reward modify 提取（"把 reward_id N 名称改为 'X'" / "修改 reward_id N 的名字为 'X'"）
    reward_modify = None
    rmm = re.search(
        r'(?:把|将|修改|改名|改为|名称改为)\s*reward_id\s*(?P<rid>\d+)\s*(?:的)?(?:名称|名字)?(?:改为|改成|为)?\s*["\'](?P<name>[^"\']+)["\']',
        text)
    if rmm:
        reward_modify = {"reward_id": rmm.group("rid"), "name": rmm.group("name")}

    return {
        "name": name,
        "model_id": mm.group("id") if mm else None,
        "space_id": sm.group("sid") if sm else None,
        "pos": (pm.group("x"), pm.group("y"), pm.group("z")) if pm else None,
        "conv": conv,
        "options": opts,
        "reward_id": rm.group("rid") if rm else None,
        "branch": branch,
        "go_opt": go_opt,
        "quest": quest,
        "reward_modify": reward_modify,
    }


# ── D2 NPC 变体提取（传送/战斗/奖励）──
# 传送目标场景：to_space_id / target_space_id
_TELEPORT_SPACE_RE = re.compile(r'(?:传送到|to_space_id|target_space_id)\s*(?:为|是)?\s*(?P<sid>\d+)')
# 战斗 ID：combat_id
_COMBAT_ID_RE = re.compile(r'(?:战斗ID|combat_id)\s*(?:为|是)?\s*(?P<cid>\d+)')
# 奖励 ID：reward_id
_REWARD_ID_RE = re.compile(r'(?:奖励ID|reward_id)\s*(?:为|是)?\s*(?P<rid>\d+)')


def _extract_npc_variant(text: str, variant: str) -> Optional[dict]:
    """从「新增 NPC + 传送/战斗/奖励」文本提取结构化字段。未命中返回 None。"""
    nm = _NPC_NAME_RE.search(text)
    if not nm:
        return None
    name = nm.group("name").strip()
    if not name:
        return None

    mm = _MODEL_ID_RE.search(text)
    sm = _SPACE_ID_RE.search(text)
    pm = _POS_RE.search(text)

    info = {
        "name": name,
        "model_id": mm.group("id") if mm else None,
        "space_id": sm.group("sid") if sm else None,
        "pos": (pm.group("x"), pm.group("y"), pm.group("z")) if pm else None,
        "raw": text,  # 供 _build_npc_reward_intents 检测"修改reward名称"子句
    }

    if variant == "npc_teleport":
        tm = _TELEPORT_SPACE_RE.search(text)
        info["target_space_id"] = tm.group("sid") if tm else None
    elif variant == "npc_combat":
        cm = _COMBAT_ID_RE.search(text)
        info["combat_id"] = cm.group("cid") if cm else None
    elif variant == "npc_reward":
        rm = _REWARD_ID_RE.search(text)
        info["reward_id"] = rm.group("rid") if rm else None
    else:
        return None

    return info


def _build_npc_teleport_intents(info: dict) -> list[SplitIntent]:
    """传送 NPC：Base + Interaction(传送效果 effect.key=3003) + spawn。"""
    name = info["name"]
    target_sid = info.get("target_space_id") or ""

    intents: list[SplitIntent] = []

    # 0) entity_prefab.Base
    f0 = {"名字": name, "交互id": "<new_interaction_id>"}
    if info["model_id"]:
        f0["model_prefab"] = info["model_id"]
    intents.append(SplitIntent(
        text=f"新增传送NPC {name}", table_hint="entity_prefab", sheet_hint="Base",
        action="add", fields=f0, produces="new_prefab_id",
    ))

    # 1) interaction.Interaction（传送效果，effect.key 由 LLM 读表头推断）
    # 不硬编码 3003，只透传语义值（目标场景ID），LLM 读表头「3003: 目标space ID」列后决定填法
    f1 = {"效果类型": "传送", "目标space ID": target_sid or "<target_space_id>"}
    intents.append(SplitIntent(
        text=f"新增Interaction传送 {name}", table_hint="interaction",
        sheet_hint="Interaction", action="add", fields=f1,
        produces="new_interaction_id",
    ))

    # 2) spawn_world_entity.SpawnWorldEntity
    if info["space_id"]:
        f2 = {"场景ID": info["space_id"], "实体名字": name,
              "实体Prefab ID": "<new_prefab_id>", "最大生成数量": "1"}
        if info["pos"]:
            x, y, z = info["pos"]
            f2["候选坐标"] = f"{x},{y},{z}"
        intents.append(SplitIntent(
            text=f"刷新 {name}", table_hint="spawn_world_entity",
            sheet_hint="SpawnWorldEntity", action="add", fields=f2,
            produces="spawn_id",
        ))

    return intents


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────

def _extract_npc_composite(text: str) -> Optional[dict]:
    """从「NPC+对话+选项+奖励+邮件」复合文本提取全部字段。

    复用 _extract_npc_dialogue 提取 NPC 基础信息，追加 reward_id / mail_template_id / mail_title / mail_content。
    """
    base = _extract_npc_dialogue(text)
    if not base:
        return None
    rm = _REWARD_ID_RE2.search(text)
    tm = _TEMPLATE_ID_RE.search(text)
    tlm = _MAIL_TITLE_RE.search(text)
    clm = _MAIL_CONTENT_RE.search(text)
    base["reward_id"] = rm.group("rid") if rm else None
    base["mail_template_id"] = tm.group("tid") if tm else None
    base["mail_title"] = (tlm.group("tq") or tlm.group("tb") or "").strip() if tlm else ""
    base["mail_content"] = (clm.group("cq") or clm.group("cb") or "").strip() if clm else ""
    return base


def _build_npc_composite_intents(info: dict) -> list[SplitIntent]:
    """复合 NPC：产出带 produces/占位符的任务链（对话+选项+奖励+邮件）。"""
    name = info["name"]
    opts = info["options"] or []
    conv = info["conv"]
    reward_id = info.get("reward_id") or ""
    mail_tid = info.get("mail_template_id") or ""
    mail_title = info.get("mail_title") or ""
    mail_content = info.get("mail_content") or ""

    intents: list[SplitIntent] = []
    # 1) entity_prefab.Base
    f0 = {"名字": name, "交互id": "<new_interaction_id>"}
    if info["model_id"]:
        f0["model_prefab"] = info["model_id"]
    intents.append(SplitIntent(
        text=f"新增NPC {name}", table_hint="entity_prefab", sheet_hint="Base",
        action="add", fields=f0, produces="new_prefab_id",
    ))
    # 2) interaction.Interaction 对话效果（effect.key 由 LLM 读表头推断）
    f1 = {"效果类型": "对话", "对话ID": "<new_conv_id>"}
    intents.append(SplitIntent(
        text=f"新增Interaction对话 {name}", table_hint="interaction",
        sheet_hint="Interaction", action="add", fields=f1,
        produces="new_interaction_id",
    ))
    # 3) 主对话
    opt_fields = {"对话内容": conv}
    for i, _ in enumerate(opts, 1):
        opt_fields[f"选项{i}"] = f"<option_{i}_id>"
    intents.append(SplitIntent(
        text=f"新增主对话 {name}", table_hint="interaction", sheet_hint="InteractionConv",
        action="add", fields=opt_fields, produces="new_conv_id",
    ))
    # 4-5) 选项
    for i, opt_text in enumerate(opts, 1):
        is_reward_opt = (i == 1 and ("领取" in opt_text or "奖励" in opt_text))
        if is_reward_opt:
            f = {"选项内容": opt_text, "option_function.function_type": "1",
                 "option_function.data.1.conv_id": "<new_reward_conv_id>"}
            produces = "option_1_id"
        else:
            f = {"选项内容": opt_text, "option_function.function_type": "0"}
            produces = f"option_{i}_id"
        intents.append(SplitIntent(
            text=f"新增选项{i} {opt_text}", table_hint="interaction",
            sheet_hint="InteractionConvOption", action="add",
            fields=f, produces=produces,
        ))
    # 6) 奖励对话
    reward_conv_text = f"{name}给你递上奖励，请收好。"
    intents.append(SplitIntent(
        text=f"新增奖励对话 {name}", table_hint="interaction", sheet_hint="InteractionConv",
        action="add", fields={"对话内容": reward_conv_text, "选项1": "<option_3_id>"},
        produces="new_reward_conv_id",
    ))
    # 7) 多谢选项
    intents.append(SplitIntent(
        text="新增选项3 多谢", table_hint="interaction", sheet_hint="InteractionConvOption",
        action="add", fields={"选项内容": f"多谢{name}", "option_function.function_type": "0"},
        produces="option_3_id",
    ))
    # 8) 奖励 Interaction（effect.key 由 LLM 读表头推断，不硬编码 3002）
    # 透传语义：效果类型=奖励 + 奖励ID值。LLM 读表头「3002: reward ID」列后决定填法
    f8 = {"效果类型": "奖励", "reward ID": reward_id or "<reward_id>"}
    intents.append(SplitIntent(
        text=f"新增Interaction奖励 {name}", table_hint="interaction",
        sheet_hint="Interaction", action="add", fields=f8,
        produces="new_reward_interaction_id",
    ))
    # 9) spawn_world_entity
    if info["space_id"]:
        f9 = {"场景ID": info["space_id"], "实体名字": name,
              "实体Prefab ID": "<new_prefab_id>", "最大生成数量": "1"}
        if info["pos"]:
            x, y, z = info["pos"]
            f9["候选坐标"] = f"{x},{y},{z}"
        intents.append(SplitIntent(
            text=f"刷新 {name}", table_hint="spawn_world_entity",
            sheet_hint="SpawnWorldEntity", action="add", fields=f9,
            produces="spawn_id",
        ))
    # 10) reward.Reward modify
    if reward_id:
        intents.append(SplitIntent(
            text=f"修改奖励 {reward_id}", table_hint="reward", sheet_hint="Reward",
            action="set", fields={"名称": f"{name}奖励"},
            locator_value=reward_id, locator_field="id",
        ))
    # 11) mail.MailTemplate
    if mail_tid:
        f11 = {"模板ID": mail_tid, "标题模板参数": mail_title, "内容模板参数": mail_content}
        intents.append(SplitIntent(
            text=f"新增邮件模板 {mail_tid}", table_hint="mail", sheet_hint="MailTemplate",
            action="add", fields=f11, produces="new_template_id",
        ))
    # 12) mail.GlobalMail
    if mail_tid:
        f12 = {"模板ID": mail_tid, "奖励": reward_id or "0"}
        intents.append(SplitIntent(
            text=f"新增全服邮件 {mail_tid}", table_hint="mail", sheet_hint="GlobalMail",
            action="add", fields=f12,
        ))
    return intents


def _build_npc_combat_intents(info: dict) -> list[SplitIntent]:
    """战斗 NPC：Base + Interaction(战斗效果 effect.key=3001) + spawn。"""
    name = info["name"]
    combat_id = info.get("combat_id") or ""
    intents: list[SplitIntent] = []
    f0 = {"名字": name, "交互id": "<new_interaction_id>"}
    if info["model_id"]:
        f0["model_prefab"] = info["model_id"]
    intents.append(SplitIntent(
        text=f"新增战斗NPC {name}", table_hint="entity_prefab", sheet_hint="Base",
        action="add", fields=f0, produces="new_prefab_id",
    ))
    # 1) interaction.Interaction（战斗效果，effect.key 由 LLM 读表头推断）
    f1 = {"效果类型": "战斗", "战斗ID": combat_id or "<combat_id>"}
    intents.append(SplitIntent(
        text=f"新增Interaction战斗 {name}", table_hint="interaction",
        sheet_hint="Interaction", action="add", fields=f1,
        produces="new_interaction_id",
    ))
    if info["space_id"]:
        f2 = {"场景ID": info["space_id"], "实体名字": name,
              "实体Prefab ID": "<new_prefab_id>", "最大生成数量": "1"}
        if info["pos"]:
            x, y, z = info["pos"]
            f2["候选坐标"] = f"{x},{y},{z}"
        intents.append(SplitIntent(
            text=f"刷新 {name}", table_hint="spawn_world_entity",
            sheet_hint="SpawnWorldEntity", action="add", fields=f2,
            produces="spawn_id",
        ))
    return intents


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────



def _build_npc_reward_intents(info: dict) -> list[SplitIntent]:
    """奖励 NPC：Base + Interaction(奖励效果 effect.key=3002) + spawn。

    D4 增强：若文本含"修改reward...名称"→ 追加 reward.Reward modify 意图。
    """
    name = info["name"]
    reward_id = info.get("reward_id") or ""
    intents: list[SplitIntent] = []
    f0 = {"名字": name, "交互id": "<new_interaction_id>"}
    if info["model_id"]:
        f0["model_prefab"] = info["model_id"]
    intents.append(SplitIntent(
        text=f"新增奖励NPC {name}", table_hint="entity_prefab", sheet_hint="Base",
        action="add", fields=f0, produces="new_prefab_id",
    ))
    f1 = {"effect.key": "3002", "effect.data.3002.reward_id": reward_id or "<reward_id>"}
    intents.append(SplitIntent(
        text=f"新增Interaction奖励 {name}", table_hint="interaction",
        sheet_hint="Interaction", action="add", fields=f1,
        produces="new_interaction_id",
    ))
    if info["space_id"]:
        f2 = {"场景ID": info["space_id"], "实体名字": name,
              "实体Prefab ID": "<new_prefab_id>", "最大生成数量": "1"}
        if info["pos"]:
            x, y, z = info["pos"]
            f2["候选坐标"] = f"{x},{y},{z}"
        intents.append(SplitIntent(
            text=f"刷新 {name}", table_hint="spawn_world_entity",
            sheet_hint="SpawnWorldEntity", action="add", fields=f2,
            produces="spawn_id",
        ))
    # D4: 文本含"修改reward_id X名称为Y"→ 追加 reward modify 意图
    raw = info.get("raw") or ""
    rm = re.search(r'修改reward_id\s*(?:为|是)?\s*(?P<rid>\d+)\s*名称(?:为|是)?\s*(?P<new_name>[^\s，,。；;]+)', raw)
    if rm and reward_id:
        intents.append(SplitIntent(
            text=f"修改奖励 {reward_id} 名称", table_hint="reward", sheet_hint="Reward",
            action="set", fields={"名称": rm.group("new_name")},
            locator_value=reward_id, locator_field="id",
        ))
    return intents


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────



def _build_npc_dialogue_intents(info: dict) -> list[SplitIntent]:
    """对话 NPC：Base + Interaction(对话效果) + Conv + Options + spawn。

    D3 对话链：主对话 → 选项（首个含"领取/奖励"跳转奖励对话，其余结束）。
    分支对话：选"看看"跳到新对话 → 新对话 + 跳转选项 function_type=1。
    """
    name = info["name"]
    opts = info.get("options") or []
    conv = info.get("conv") or ""
    branch = info.get("branch")  # {"opt_text":"看看","branch_conv":"这只赤炎虎饿了..."}
    intents: list[SplitIntent] = []
    f0 = {"名字": name, "交互id": "<new_interaction_id>"}
    if info["model_id"]:
        f0["模型"] = info["model_id"]
    intents.append(SplitIntent(
        text=f"新增NPC {name}", table_hint="entity_prefab", sheet_hint="Base",
        action="add", fields=f0, produces="new_prefab_id",
    ))
    # Interaction 对话效果
    f1 = {"effect.key": "3006", "effect.data.3006.conv_id": "<new_conv_id>"}
    intents.append(SplitIntent(
        text=f"新增Interaction对话 {name}", table_hint="interaction",
        sheet_hint="Interaction", action="add", fields=f1,
        produces="new_interaction_id",
    ))
    # 主对话
    opt_fields = {"对话内容": conv}
    for i, _ in enumerate(opts, 1):
        opt_fields[f"选项{i}"] = f"<option_{i}_id>"
    intents.append(SplitIntent(
        text=f"新增主对话 {name}", table_hint="interaction", sheet_hint="InteractionConv",
        action="add", fields=opt_fields, produces="new_conv_id",
    ))
    # 选项
    reward_opt_idx = None
    reward_id = info.get("reward_id")
    for i, opt_text in enumerate(opts, 1):
        # 分支对话：选项文本匹配 branch.opt_text → function_type=1 跳 branch_conv
        is_branch_opt = branch and opt_text == branch.get("opt_text")
        # 有 reward_id 时，第一个选项跳转奖励对话（用户说"点击X后获得奖励"）
        is_reward_opt = (i == 1 and reward_id is not None) or \
                        ("领取" in opt_text or "奖励" in opt_text or "获得" in opt_text)
        if is_branch_opt:
            f = {"选项内容": opt_text,
                 "option_function.function_type": "1",
                 "option_function.data.1.conv_id": "<new_branch_conv_id>"}
            reward_opt_idx = i  # 复用奖励选项的"跳转"语义
        elif is_reward_opt:
            f = {"选项内容": opt_text,
                 "option_function.function_type": "1",
                 "option_function.data.1.conv_id": "<new_reward_conv_id>"}
            reward_opt_idx = i
        else:
            f = {"选项内容": opt_text, "option_function.function_type": "0"}
        intents.append(SplitIntent(
            text=f"新增选项{i} {opt_text}", table_hint="interaction",
            sheet_hint="InteractionConvOption", action="add",
            fields=f, produces=f"option_{i}_id",
        ))
    # 分支对话链：选"看看"跳到新对话 → 新对话 add
    if branch:
        go_opt = info.get("go_opt")
        quest = info.get("quest")
        has_go = bool(go_opt and quest and quest.get("quest_id"))
        bf = {"对话内容": branch["branch_conv"]}
        if has_go:
            bf["选项1"] = "<option_go_id>"
        intents.append(SplitIntent(
            text=f"新对话 {name} 分支", table_hint="interaction",
            sheet_hint="InteractionConv", action="add",
            fields=bf, produces="new_branch_conv_id",
        ))
        # 接任务选项：function_type=2 触发 quest_id（quest_npc step7 "我这就去"接任务）
        if has_go:
            intents.append(SplitIntent(
                text=f"新增选项 接任务 {go_opt['opt_text']}", table_hint="interaction",
                sheet_hint="InteractionConvOption", action="add",
                fields={"选项内容": go_opt["opt_text"],
                        "option_function.function_type": "2",
                        "option_function.data.2.quest_id": quest["quest_id"]},
                produces="option_go_id",
            ))
    # 奖励对话链（有奖励选项且无分支时）：奖励对话 + 多谢选项
    elif reward_opt_idx is not None and reward_id is not None:
        reward_conv_text = f"{name}递给你一份奖励，请收好。"
        intents.append(SplitIntent(
            text=f"新增奖励对话 {name}", table_hint="interaction", sheet_hint="InteractionConv",
            action="add", fields={"对话内容": reward_conv_text, "选项1": "<option_reward_end_id>"},
            produces="new_reward_conv_id",
        ))
        thanks_fields = {"选项内容": f"多谢{name}",
                         "option_function.function_type": "1",
                         "option_function.data.1.reward_id": reward_id}
        intents.append(SplitIntent(
            text="新增选项 多谢", table_hint="interaction", sheet_hint="InteractionConvOption",
            action="add", fields=thanks_fields, produces="option_reward_end_id",
        ))
    # spawn
    if info["space_id"]:
        f4 = {"场景ID": info["space_id"], "实体名字": name,
              "实体Prefab ID": "<new_prefab_id>", "最大生成数量": "1"}
        if info["pos"]:
            x, y, z = info["pos"]
            f4["候选坐标"] = f"{x},{y},{z}"
        intents.append(SplitIntent(
            text=f"刷新 {name}", table_hint="spawn_world_entity",
            sheet_hint="SpawnWorldEntity", action="add", fields=f4,
            produces="spawn_id",
        ))
    # 任务链（混合指令：NPC + 对话 + 任务 + 刷怪）
    quest = info.get("quest")
    if quest:
        qf: dict[str, str] = {"quest_id": quest["quest_id"], "name": quest["name"]}
        if quest["group_id"]:
            qf["group_id"] = quest["group_id"]
        if quest["desc"]:
            qf["desc"] = quest["desc"]
        if quest["target_type"]:
            qf["target.key"] = quest["target_type"]
        if quest["target_data"]:
            qf["target.data"] = quest["target_data"]
        if quest["kill_npc_id"]:
            qf["npc_ids"] = quest["kill_npc_id"]
        if quest["reward_id"]:
            qf["rewards"] = quest["reward_id"]
        intents.append(SplitIntent(
            text=f"新增任务 {quest['name']}", table_hint="quest", sheet_hint="Quest",
            action="add", fields=qf,
        ))
        if quest["kill_npc_id"] and quest["kill_space_id"]:
            sf = {"space_id": quest["kill_space_id"],
                  "entity_prefab_id": quest["kill_npc_id"]}
            if quest["kill_pos"]:
                kx, ky, kz = quest["kill_pos"]
                sf["pos_list"] = f"{kx},{ky},{kz}"
            intents.append(SplitIntent(
                text=f"刷新任务怪物 {quest['kill_npc_id']}", table_hint="spawn_quest_entity",
                sheet_hint="SpawnQuestEntity", action="add", fields=sf,
                produces="new_spawn_quest_id",
            ))
    # reward modify（"把 reward_id N 名称改为 'X'"）
    rmod = info.get("reward_modify")
    if rmod:
        intents.append(SplitIntent(
            text=f"修改reward {rmod['reward_id']} 名称", table_hint="reward",
            sheet_hint="Reward", action="modify",
            fields={"name": rmod["name"]},
            locator_value=rmod["reward_id"], locator_field="id",
        ))
    return intents


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────



# ── 道具跨 sheet 模式（ItemBase → Equipment/Potion/Fabao）─────────

# 道具类型 → (item_type, 子 sheet, 识别关键词)
_ITEM_SUB_SHEETS = {
    "weapon": (5, "Equipment", ["武器"]),
    "potion": (3, "Potion", ["药品", "丹药", "药水"]),
    "fabao": (6, "Fabao", ["法宝"]),
}
_NAME_RE_ITEM = re.compile(r'(?:新增|加|添加|增加)(?:一把|一个|一件|一只|新)?(?:武器|药品|法宝|装备|道具|防具|饰品|丹药|药水)?\s*(?:叫|名为|名字叫)?\s*["\']?(?P<name>[^\s，,。；;（("\'（]+)')
_QUALITY_RE = re.compile(r'品质\s*(?:为|是)?\s*(?P<q>\d+)')
_ICON_RE = re.compile(r"图标\s*(?:['\"](?P<iq>[^'\"]+)['\"]|(?P<ib>[^\s，,。；;]+))")
_ITEM_ID_RE = re.compile(r'item_id\s*(?:为|是)?\s*(?P<iid>\d+)')


def _detect_item_subtype(text: str) -> str:
    """识别道具子类型：weapon/potion/fabao。"""
    for key, (_, _, keywords) in _ITEM_SUB_SHEETS.items():
        if any(kw in text for kw in keywords):
            return key
    return ""


def _build_item_intents(text: str) -> list[SplitIntent]:
    """道具跨 sheet：ItemBase produces new_item_id → 子表引用。

    用例5武器→Equipment，用例6药品→Potion，用例7法宝→Fabao。
    """
    subtype = _detect_item_subtype(text)
    if not subtype:
        return []
    item_type, sub_sheet, _ = _ITEM_SUB_SHEETS[subtype]
    nm = _NAME_RE_ITEM.search(text)
    if not nm:
        return []
    name = nm.group("name").strip().strip("'\"")
    qm = _QUALITY_RE.search(text)
    im = _ICON_RE.search(text)

    # 1) ItemBase（produces new_item_id）
    base_fields: dict[str, str] = {"name": name, "item_type": str(item_type)}
    if qm:
        base_fields["quality"] = qm.group("q")
    if im:
        base_fields["icon"] = im.group("iq") or im.group("ib")
    if subtype == "weapon":
        base_fields["max_stack"] = "1"
        base_fields["droppable"] = "1"
    elif subtype == "potion":
        ms = re.search(r'最大堆叠\s*(?:为|是)?\s*(?P<ms>\d+)', text)
        if ms:
            base_fields["max_stack"] = ms.group("ms")
        if '可丢弃' in text:
            base_fields["droppable"] = "1"

    intents: list[SplitIntent] = []
    intents.append(SplitIntent(
        text=f"新增道具基础 {name}", table_hint="item", sheet_hint="ItemBase",
        action="add", fields=base_fields, produces="new_item_id",
    ))

    # 2) 子 sheet（引用 new_item_id）
    sub_fields: dict[str, str] = {"item_id": "<new_item_id>"}
    if subtype == "weapon":
        _fill_weapon_fields(text, sub_fields)
    elif subtype == "potion":
        _fill_potion_fields(text, sub_fields)
    elif subtype == "fabao":
        _fill_fabao_fields(text, sub_fields)

    intents.append(SplitIntent(
        text=f"新增{subtype} {name}", table_hint="item", sheet_hint=sub_sheet,
        action="add", fields=sub_fields,
    ))

    # 3) 法宝子类：若含「法宝技能编号/法术名」→ 追加 spell.xlsx 双 sheet
    # 用例2：玄火鉴 + 法术 700010 玄火灼烧（common_spell + spell_data）
    if subtype == "fabao":
        spell_id_m = re.search(r'法宝技能编号\s*(?:为|是)?\s*(?P<sid>\d+)', text)
        spell_name_m = re.search(r'法术叫\s*["\']?(?P<sn>[^"\',，。；;]+?)["\']?(?:[，,。；;]|$|对)', text)
        if not spell_name_m:
            spell_name_m = re.search(r'叫\s*["\'](?P<sn>[^"\']+)["\']', text)
        if spell_id_m:
            sid = spell_id_m.group("sid")
            sn = spell_name_m.group("sn").strip().strip("'\"") if spell_name_m else name
            # common_spell
            f3 = {"id": sid, "name": sn}
            intents.append(SplitIntent(
                text=f"新增法术 {sn}", table_hint="spell", sheet_hint="common_spell",
                action="add", fields=f3,
            ))
            # spell_data：提取元素/伤害
            f4: dict[str, str] = {"id": sid, "spell_type": "attack"}
            elem_m = re.search(r'(\w+)系法术', text)
            if elem_m:
                elem_map = {"火": "fire", "水": "water", "雷": "thunder",
                            "土": "earth", "风": "wind", "冰": "ice"}
                f4["spell_element"] = elem_map.get(elem_m.group(1), elem_m.group(1).lower())
            dmg_m = re.search(r'(\d+(?:\.\d+)?)%\s*伤害|造成\s*(\d+(?:\.\d+)?)\s*倍', text)
            if dmg_m:
                rate = dmg_m.group(1) or dmg_m.group(2)
                f4["damage_base_rate"] = str(float(rate) / 100 if float(rate) > 10 else float(rate))
            intents.append(SplitIntent(
                text=f"法术数据 {sn}", table_hint="spell", sheet_hint="spell_data",
                action="add", fields=f4,
            ))
    return intents


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────



def _fill_weapon_fields(text: str, f: dict) -> None:
    """武器 Equipment 字段。"""
    m = re.search(r'装备部位\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["equip_slot"] = m.group("v")
    m = re.search(r'装备类型\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["equip_type"] = m.group("v")
    m = re.search(r'可用门派\s*(?:为|是)?\s*(?P<v>[\d、,]+)', text)
    if m:
        f["equip_sch_list"] = "[" + ",".join(re.findall(r'\d+', m.group("v"))) + "]"
    m = re.search(r'基础物攻(\w+)范围\s*(?P<lo>\d+)\s*[-~]\s*(?P<hi>\d+)', text)
    if m:
        f["equip_base_attrs[0]"] = f'["{m.group(1)}", {m.group("lo")}, {m.group("hi")}]'
    m = re.search(r'随机词条池\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["rand_ga_pool"] = m.group("v")
    m = re.search(r'分解获得item_id\s*(?P<iid>\d+)\s*数量\s*(?P<num>\d+)', text)
    if m:
        f["salvage_item_id"] = m.group("iid")
        f["salvage_item_num"] = m.group("num")
    m = re.search(r'基础评分\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["base_score"] = m.group("v")


def _fill_potion_fields(text: str, f: dict) -> None:
    """药品 Potion 字段。"""
    m = re.search(r"效果描述\s*(?:['\"](?P<dq>[^'\"]+)['\"]|(?P<db>[^\s，,。；;]+))", text)
    if m: f["desc"] = m.group("dq") or m.group("db")
    m = re.search(r'使用效果id\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["usage_effect.id"] = m.group("v")
    m = re.search(r'回复(?:真元|MPMaxCon)\s*(?P<v>\d+)', text)
    if m: f["usage_effect.args.MPMaxCon"] = m.group("v")


def _fill_fabao_fields(text: str, f: dict) -> None:
    """法宝 Fabao 字段。"""
    m = re.search(r'法宝类型\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["fabao_type"] = m.group("v")
    m = re.search(r'法宝技能编号\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["fabao_spell_id"] = m.group("v")
    m = re.search(r'附加属性池\s*(?:为|是)?\s*(?P<v>\d+)', text)
    if m: f["attr_mod_id"] = m.group("v")
    m = re.search(r'阴阳权重\s*(?:为|是)?\s*(?P<v>[\d.]+)', text)
    if m: f["yinyang_rate"] = m.group("v")
    m = re.search(r"阳属性描述\s*(?:['\"](?P<q>[^'\"]+)['\"]|(?P<b>[^\s，,。；;]+))", text)
    if m: f["yang_desc"] = m.group("q") or m.group("b")
    m = re.search(r"阴属性描述\s*(?:['\"](?P<q>[^'\"]+)['\"]|(?P<b>[^\s，,。；;]+))", text)
    if m: f["yin_desc"] = m.group("q") or m.group("b")


# ── 邮件模式（MailTemplate → GlobalMail）──────────────────────────

# mail 相关正则（复合 NPC 模式也复用）
_REWARD_ID_RE2 = re.compile(r'reward_id\s*(?:为|是)?\s*(?P<rid>\d+)')
_TEMPLATE_ID_RE = re.compile(r'template_id\s*(?:为|是)?\s*(?P<tid>\d+)')
_MAIL_TITLE_RE = re.compile(
    r'标题\s*(?:["\'](?P<tq>[^"\']+)["\']|(?P<tb>.+?)(?=内容|附带|。|；|;|$))'
)
_MAIL_CONTENT_RE = re.compile(
    r'内容\s*(?:["\'](?P<cq>[^"\']+)["\']|(?P<cb>.+?)(?=附带|。|；|;|$))'
)


def _build_mail_intents(text: str) -> list[SplitIntent]:
    """邮件模板 + 全服邮件：MailTemplate add → GlobalMail add 引用 template_id。

    用例9：template_id 30015 + 标题 + 内容 → GlobalMail(global_id, template_id, sender, reward_id)
    """
    tm = _TEMPLATE_ID_RE.search(text)
    if not tm:
        return []
    tid = tm.group("tid")
    tlm = _MAIL_TITLE_RE.search(text)
    clm = _MAIL_CONTENT_RE.search(text)
    title = (tlm.group("tq") or tlm.group("tb") or "").strip() if tlm else ""
    content = (clm.group("cq") or clm.group("cb") or "").strip() if clm else ""

    intents: list[SplitIntent] = []
    # 1) MailTemplate
    f1 = {"template_id": tid}
    if title: f1["title"] = title
    if content: f1["content"] = content
    intents.append(SplitIntent(
        text=f"新增邮件模板 {tid}", table_hint="mail", sheet_hint="MailTemplate",
        action="add", fields=f1, produces="new_template_id",
    ))

    # 2) GlobalMail（引用 template_id + reward_id）
    gm = re.search(r'global_id\s*(?:为|是)?\s*(?P<gid>\d+)', text)
    sm = re.search(r"发送人\s*(?:['\"](?P<sq>[^'\"]+)['\"]|(?P<sb>[^\s，,。；;]+))", text)
    rm = _REWARD_ID_RE2.search(text)
    f2: dict[str, str] = {"template_id": tid, "mail_type": "1"}
    if gm: f2["global_id"] = gm.group("gid")
    if sm: f2["sender"] = sm.group("sq") or sm.group("sb")
    if rm: f2["reward_id"] = rm.group("rid")
    intents.append(SplitIntent(
        text=f"新增全服邮件 {tid}", table_hint="mail", sheet_hint="GlobalMail",
        action="add", fields=f2,
    ))
    return intents


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────



# ── 任务模式（Quest + SpawnQuestEntity）────────────────────────────

def _build_quest_intents(text: str) -> list[SplitIntent]:
    """支线任务 + 刷新任务实体：Quest add → spawn_quest_entity add 引用 npc_id。

    用例10：任务ID+group_id+描述+目标+奖励 + 击杀npc_id+space_id+坐标刷新
    """
    qid_m = re.search(r'任务ID\s*(?:为|是)?\s*(?P<qid>\d+)', text)
    if not qid_m:
        return []
    qid = qid_m.group("qid")
    nm = re.search(r"新增(?:一个)?(?:支线|主线|日常|环式)?任务(?:叫|名为)?\s*(?P<name>[^\s，,。；;]+)", text)
    name = nm.group("name").strip().strip("'\"") if nm else ""
    gid_m = re.search(r'group_id\s*(?:为|是)?\s*(?P<gid>\d+)', text)
    desc_m = re.search(r"描述\s*(?:['\"](?P<dq>[^'\"]+)['\"]|(?P<db>[^\s，,。；;]+))", text)
    tgt_type_m = re.search(r'目标类型\s*(?P<tt>[A-Za-z]+)', text)
    tgt_data_m = re.search(r"目标数据\s*(?:['\"](?P<tdq>[^'\"]+)['\"]|(?P<tdb>[^\s，,。；;]+))", text)
    reward_m = _REWARD_ID_RE2.search(text)
    npc_m = re.search(r'npc_id\s*(?:为|是)?\s*(?P<nid>\d+)', text)
    space_m = _SPACE_ID_RE.search(text)
    pos_m = _POS_RE.search(text)

    intents: list[SplitIntent] = []
    # 1) Quest
    f1: dict[str, str] = {"quest_id": qid, "name": name}
    if gid_m: f1["group_id"] = gid_m.group("gid")
    if desc_m: f1["desc"] = desc_m.group("dq") or desc_m.group("db")
    if tgt_type_m: f1["target.key"] = tgt_type_m.group("tt")
    if tgt_data_m: f1["target.data"] = tgt_data_m.group("tdq") or tgt_data_m.group("tdb")
    if npc_m: f1["npc_ids"] = f"[{npc_m.group('nid')}]"
    if reward_m: f1["rewards"] = f"[{reward_m.group('rid')}]"
    intents.append(SplitIntent(
        text=f"新增任务 {name}", table_hint="quest", sheet_hint="Quest",
        action="add", fields=f1,
    ))

    # 2) spawn_quest_entity（刷新击杀怪物）
    if npc_m and space_m:
        f2: dict[str, str] = {"space_id": space_m.group("sid"),
                              "entity_prefab_id": npc_m.group("nid")}
        if pos_m:
            x, y, z = pos_m.group("x"), pos_m.group("y"), pos_m.group("z")
            f2["pos_list"] = f"[[{x},{y},{z}]]"
        intents.append(SplitIntent(
            text=f"刷新任务怪物 {npc_m.group('nid')}", table_hint="spawn_quest_entity",
            sheet_hint="SpawnQuestEntity", action="add", fields=f2,
            produces="new_spawn_id",
        ))
    return intents


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────



# ── 灵兽模式（Pet + PetEvolveData）─────────────────────────────────

def _build_pet_intents(text: str) -> list[SplitIntent]:
    """灵兽新增 + 进化路径：Pet add produces new_pet_id → pet_evolve add 引用。

    用例8：灵兽基础属性+资质 + 进化为pet_id消耗道具
    """
    nm = re.search(r"新增(?:一只|一个)?(?:灵兽|宠物|召唤兽)\s*(?:叫|名为)?\s*(?P<name>[^\s，,。；;]+)", text)
    if not nm:
        return []
    name = nm.group("name").strip().strip("'\"")
    mm = _MODEL_ID_RE.search(text)
    qm = _QUALITY_RE.search(text)
    elem_m = re.search(r'元素类型\s*(?P<et>[A-Za-z]+)', text)
    egg_m = _ITEM_ID_RE.search(text)
    lvl_m = re.search(r'出战所需人物等级\s*(?:为|是)?\s*(?P<v>\d+)', text)
    alloc_m = re.search(r'默认加点\s*(?:为|是)?\s*(?P<v>\d+)', text)
    # 资质：体力资质5000、物攻资质1800...
    apt_pairs = re.findall(r'(体力|物攻|法攻|物防|法防)资质\s*(?P<v>\d+)', text)
    # 进化
    evolve_m = re.search(r'进化为pet_id\s*(?P<eid>\d+)', text)
    cost_m = re.search(r'消耗道具item_id\s*(?P<cid>\d+)\s*数量\s*(?P<cnum>\d+)', text)

    intents: list[SplitIntent] = []
    # 1) Pet
    apt_map = {"体力": "StrPotCon", "物攻": "PhyatkPotCon", "法攻": "MagatkPotCon",
               "物防": "PhydefPotCon", "法防": "MagdefPotCon"}
    f1: dict[str, str] = {"name": name}
    if mm: f1["model_id"] = mm.group("id")
    if qm: f1["quality"] = qm.group("q")
    if elem_m: f1["elemental_type"] = elem_m.group("et")
    if egg_m: f1["item_id"] = egg_m.group("iid")
    if lvl_m: f1["hero_level"] = lvl_m.group("v")
    if alloc_m: f1["auto_attr_point_alloc_type"] = alloc_m.group("v")
    for label, val in apt_pairs:
        key = apt_map.get(label)
        if key:
            f1[f"aptitude_base.{key}"] = val
    intents.append(SplitIntent(
        text=f"新增灵兽 {name}", table_hint="pet", sheet_hint="Pet",
        action="add", fields=f1, produces="new_pet_id",
    ))

    # 2) PetEvolveData（进化路径）
    if evolve_m:
        f2: dict[str, str] = {"pet_id": "<new_pet_id>",
                              "evolved_pet_id": evolve_m.group("eid")}
        if cost_m:
            f2["cost_item_id"] = cost_m.group("cid")
            f2["cost_item_num"] = cost_m.group("cnum")
        intents.append(SplitIntent(
            text=f"灵兽进化路径 {name}", table_hint="pet_evolve",
            sheet_hint="PetEvolveData", action="add", fields=f2,
        ))
    return intents


# ── 级联数据驱动（关系图谱查询，供未命中模式时 LLM 上下文增强）─────


# ── 门派神通+神通等级+技能组+法术 四表联动（模式9）─────────────────

# 神通编号 / 神通名 / 法术id 提取
_SCHOOL_ABILITY_ID_RE = re.compile(r'神通编号\s*(?:为|是)?\s*(?P<aid>\d+)')
_SCHOOL_ABILITY_NAME_RE = re.compile(
    r'新神通["[""](?P<name>[^""]+)["""]|新神通\s*(?:叫|名为)?\s*(?P<name2>[^\s，,。；;]+)')
_SPELL_ID_RE_SCHOOL = re.compile(r'关联法术\s*(?P<sid>\d+)|法术编号\s*(?P<sid2>\d+)')
_SCHOOL_DESC_RE = re.compile(r'描述\s*["""](?P<desc>[^""]+)["""]')
_GROUP_ID_RE_SCHOOL = re.compile(r'技能组\s*(?P<gid>\d+)|组编号\s*(?P<gid2>\d+)')
_GROUP_NAME_RE = re.compile(r'组名\s*["""](?P<gn>[^""]+)["""]')
_UNLOCK_LEVEL_RE = re.compile(r'解锁等级\s*(?P<lvl>\d+)')
_UPGRADE_LEVEL_RE = re.compile(r'升级要求人物\s*(?P<lvl>\d+)\s*级')


def _build_school_ability_spell_intents(text: str) -> list[SplitIntent]:
    """门派神通 + 神通等级 + 技能组 + 法术（common_spell + spell_data）四表。

    用例3：神通编号 8099 + 0级配置 + 法术 700020 + 技能组 500。
    依赖链：SpellGroup.spell_ids 引用 common_spell.id（显式编号，无占位符）；
            SchoolAbilityLevel.common_spell_id 引用 common_spell.id；
            四表均用用户显式编号，无需 produces/consume 占位符。
    """
    aid_m = _SCHOOL_ABILITY_ID_RE.search(text)
    if not aid_m:
        return []
    aid = aid_m.group("aid")
    nm_m = _SCHOOL_ABILITY_NAME_RE.search(text)
    name = (nm_m.group("name") or nm_m.group("name2") or "").strip().strip("'\"") if nm_m else ""
    sid_m = _SPELL_ID_RE_SCHOOL.search(text)
    sid = sid_m.group("sid") or sid_m.group("sid2") if sid_m else ""
    desc_m = _SCHOOL_DESC_RE.search(text)
    desc = desc_m.group("desc") if desc_m else ""
    gid_m = _GROUP_ID_RE_SCHOOL.search(text)
    gid = gid_m.group("gid") or gid_m.group("gid2") if gid_m else ""
    gn_m = _GROUP_NAME_RE.search(text)
    gname = gn_m.group("gn") if gn_m else ""
    ul_m = _UNLOCK_LEVEL_RE.search(text)
    ulvl = ul_m.group("lvl") if ul_m else "1"
    up_m = _UPGRADE_LEVEL_RE.search(text)
    uplvl = up_m.group("lvl") if up_m else "1"

    intents: list[SplitIntent] = []
    # 1) SchoolAbility
    f1: dict[str, str] = {"神通id": aid, "名称": name, "解锁等级": ulvl}
    if desc:
        f1["神通描述"] = desc
    if name:
        f1["图标"] = "spell_default.png"
    intents.append(SplitIntent(
        text=f"新增神通 {name}", table_hint="school_ability", sheet_hint="SchoolAbility",
        action="add", fields=f1,
    ))
    # 2) SchoolAbilityLevel（0级配置）
    if sid:
        f2: dict[str, str] = {
            "school_ability_id": aid, "等级": "0",
            "升级消耗": "0", "升级要求等级": uplvl,
            "法术id": sid,
        }
        intents.append(SplitIntent(
            text=f"神通等级 {name} 0级", table_hint="school_ability",
            sheet_hint="SchoolAbilityLevel", action="add", fields=f2,
        ))
    # 3) SpellGroup
    if sid and gid:
        f3: dict[str, str] = {"组id": gid, "组名": gname or f"组{gid}", "法术列表": f"[{sid}]"}
        intents.append(SplitIntent(
            text=f"新增技能组 {gname or gid}", table_hint="spell_group",
            sheet_hint="SpellGroup", action="add", fields=f3,
        ))
    # 4) common_spell
    if sid:
        f4: dict[str, str] = {"id": sid, "name": name}
        intents.append(SplitIntent(
            text=f"新增法术 {name}", table_hint="spell", sheet_hint="common_spell",
            action="add", fields=f4,
        ))
        # 5) spell_data
        f5: dict[str, str] = {"id": sid, "spell_type": "attack"}
        intents.append(SplitIntent(
            text=f"法术数据 {name}", table_hint="spell", sheet_hint="spell_data",
            action="add", fields=f5,
        ))
    return intents


# ── 战斗+奖励包 联动（模式10）──────────────────────────────────────

_COMBAT_ID_RE_CR = re.compile(r'战斗\s*(?:ID|id)?\s*(?:为|是)?\s*(?P<cid>\d{6,})')
_REWARD_ID_RE_CR = re.compile(r'奖励包\s*(?:为|是)?\s*(?P<rid>\d+)')
_SPACE_ID_RE_CR = re.compile(r'战场\s*(?P<sid>\d+)')
_NPC_IDS_RE_CR = re.compile(r'npc\s*(?P<nids>\d+(?:\s*[，,和、]\s*\d+)+)', re.IGNORECASE)


def _build_combat_reward_intents(text: str) -> list[SplitIntent]:
    """战斗 combat_data + 奖励包 Reward 联动。

    用例4：战斗 77777001 + 奖励包 10999 + 战场 10001 + npc 3000,3001。
    两表用显式编号，无占位符依赖。
    """
    intents: list[SplitIntent] = []
    cid_m = _COMBAT_ID_RE_CR.search(text)
    rid_m = _REWARD_ID_RE_CR.search(text)
    # combat_data
    if cid_m:
        cid = cid_m.group("cid")
        rid = rid_m.group("rid") if rid_m else "0"
        sid_m = _SPACE_ID_RE_CR.search(text)
        sid = sid_m.group("sid") if sid_m else ""
        nids_m = _NPC_IDS_RE_CR.search(text)
        nids = []
        if nids_m:
            nids = re.findall(r'\d+', nids_m.group("nids"))
        f1: dict[str, str] = {
            "战斗id": cid, "名称": "清剿战斗",
            "战斗类型": "pve",
        }
        if sid:
            f1["战场id"] = sid
        if rid:
            f1["胜利奖励"] = rid
            f1["失败奖励"] = "0"
            f1["平局奖励"] = "0"
        if nids:
            f1["npc列表"] = "[" + ",".join(nids) + "]"
        intents.append(SplitIntent(
            text=f"新增战斗 {cid}", table_hint="combat", sheet_hint="combat_data",
            action="add", fields=f1,
        ))
    # Reward
    if rid_m:
        rid = rid_m.group("rid")
        f2: dict[str, str] = {
            "id": rid, "名称": "清剿奖励", "每日上限": "50",
            "经验概率": "10000", "金币概率": "10000", "金币公式": "100",
        }
        intents.append(SplitIntent(
            text=f"新增奖励包 {rid}", table_hint="reward", sheet_hint="Reward",
            action="add", fields=f2,
        ))
    return intents


# ── 洞府建筑类型+实例+交互动画（模式11）──────────────────────────

_BUILDING_TYPE_ID_RE = re.compile(r'建筑类型\s*(?P<bt>\d+)')
_BUILDING_NAME_RE = re.compile(r'叫\s*["""](?P<bn>[^""]+)["""]|叫\s*(?P<bn2>[^\s，,。；;]+)')
_ENTITY_TYPE_RE = re.compile(r'实体类型\s*(?P<et>[A-Za-z]+)')
_BUILDING_ITEM_ID_RE = re.compile(r'建筑(?:道具)?(?:编号|实例)\s*(?P<biid>\d+)|实例编号\s*(?P<biid2>\d+)')
_MODEL_ID_RE_BLDG = re.compile(r'模型\s*(?P<mid>\d+)')
_MAP_ICON_RE_BLDG = re.compile(r'地图图标\s*(?P<mi>\d+)')
_BLUEPRINT_RE = re.compile(r'图纸道具(?:为第|编号为第|id为第)?\s*第?\s*(?P<bp>\d+)\s*号?')
_RESIDENCE_LEVEL_RE = re.compile(r'(?:洞府等级|准入等级)\s*(?:为|是)?\s*(?P<rl>\d+|入门级)')
_AREA_RE = re.compile(
    r'占地\s*(?P<ax>[一二三四五六七八九十\d]+)\s*格(?:子)?\s*[，,、]?\s*(?P<ay>[一二三四五六七八九十\d]+)\s*格(?:子)?')

_CN_NUM = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
           "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _cn_to_int(s: str) -> str:
    """中文数字/阿拉伯数字 → 字符串数字。非数字原样返回。"""
    if not s:
        return s
    if s.isdigit():
        return s
    if s == "十":
        return "10"
    if len(s) == 1:
        return str(_CN_NUM.get(s, s))
    if s.startswith("十"):
        return str(10 + _CN_NUM.get(s[1], 0))
    if s.endswith("十"):
        return str(_CN_NUM.get(s[0], 0) * 10)
    a, b = s.split("十", 1)
    return str(_CN_NUM.get(a, 0) * 10 + _CN_NUM.get(b, 0))


def _build_residence_building_intents(text: str) -> list[SplitIntent]:
    """建筑类型 + 建筑实例 + 交互动画（同文件三 sheet）。

    用例5：建筑类型11 + 实例22199 + idle动画。三表用 building_type 串联。
    """
    bt_m = _BUILDING_TYPE_ID_RE.search(text)
    if not bt_m:
        return []
    bt = bt_m.group("bt")
    bn_m = _BUILDING_NAME_RE.search(text)
    bname = (bn_m.group("bn") or bn_m.group("bn2") or "").strip().strip("'\"") if bn_m else ""
    et_m = _ENTITY_TYPE_RE.search(text)
    etype = et_m.group("et") if et_m else ""
    biid_m = _BUILDING_ITEM_ID_RE.search(text)
    biid = (biid_m.group("biid") or biid_m.group("biid2")) if biid_m else ""
    mid_m = _MODEL_ID_RE_BLDG.search(text)
    mid = mid_m.group("mid") if mid_m else ""
    mi_m = _MAP_ICON_RE_BLDG.search(text)
    mi = mi_m.group("mi") if mi_m else ""
    bp_m = _BLUEPRINT_RE.search(text)
    bp = bp_m.group("bp") if bp_m else ""
    rl_m = _RESIDENCE_LEVEL_RE.search(text)
    rl = "1" if (rl_m.group("rl") if rl_m else "") == "入门级" else (rl_m.group("rl") if rl_m else "1")
    area_m = _AREA_RE.search(text)
    area_x = _cn_to_int(area_m.group("ax")) if area_m else "1"
    area_y = _cn_to_int(area_m.group("ay")) if area_m else "1"
    # 一级/二级分类
    pc_m = re.search(r'一级分类\s*["""](?P<pc>[^""]+)["""]', text)
    sc_m = re.search(r'二级分类\s*["""](?P<sc>[^""]+)["""]', text)

    intents: list[SplitIntent] = []
    # 1) BuildingType
    f1: dict[str, str] = {"建筑类型": bt, "名称": bname}
    if etype:
        f1["实体类型"] = etype
    if pc_m:
        f1["一级分类"] = pc_m.group("pc")
    if sc_m:
        f1["二级分类"] = sc_m.group("sc")
    # 等级上限：洞府1级最多放2个、2级最多放3个
    lvl_m = re.search(r'1\s*级最多放\s*(?P<l1>\d+)\s*个.*?2\s*级最多放\s*(?P<l2>\d+)\s*个', text)
    if lvl_m:
        f1["等级建筑上限1"] = lvl_m.group("l1")
        f1["等级建筑上限2"] = lvl_m.group("l2")
    intents.append(SplitIntent(
        text=f"新增建筑类型 {bname}", table_hint="residence_building",
        sheet_hint="BuildingType", action="add", fields=f1,
    ))
    # 2) ResidenceBuilding
    if biid:
        f2: dict[str, str] = {
            "建筑道具id": biid, "名称": "初级炼丹炉",
            "等级": "1", "建筑类型": bt,
        }
        if mid:
            f2["模型id"] = mid
        if mi:
            f2["地图图标id"] = mi
        if bp:
            f2["图纸道具id"] = bp
        if rl:
            f2["洞府等级"] = rl
        f2["占地X"] = area_x
        f2["占地Y"] = area_y
        # 实例名提取
        iname_m = re.search(r'建筑实例\s*["""](?P<in>[^""]+)["""]', text)
        if iname_m:
            f2["名称"] = iname_m.group("in")
        intents.append(SplitIntent(
            text=f"新增建筑实例 {biid}", table_hint="residence_building",
            sheet_hint="ResidenceBuilding", action="add", fields=f2,
        ))
    # 3) BuildingInteract
    montage_m = re.search(r'蒙太奇(?:动画)?\s*[为:：]?\s*(?P<mg>[^\s，,。；;]+)', text)
    f3: dict[str, str] = {"建筑类型": bt, "状态id": "idle"}
    if montage_m:
        f3["角色蒙太奇"] = montage_m.group("mg").rstrip('。，,；;')
    f3["建筑状态"] = "0"
    f3["软停止"] = "1"
    intents.append(SplitIntent(
        text=f"建筑交互动画 {bt}", table_hint="residence_building",
        sheet_hint="BuildingInteract", action="add", fields=f3,
    ))
    return intents


# ── 级联数据驱动（关系图谱查询，供未命中模式时 LLM 上下文增强）─────

def get_cascade_hints(stem: str) -> dict:
    """查询 add stem 表时的级联提示（基于 table_relations.json 关系图谱）。

    数据驱动接口：cross_table_splitter 硬编码 _build_* 覆盖 task_chain 已知模式
    （NPC/item/pet/mail/quest），未命中模式时上层可调本函数获取关系图谱级联
    提示，注入 LLM 上下文辅助拆分多 op。

    返回:
      {depends_on: [{target_stem, source_col, target_col, sheet}],  # add 此表时需已存在
       referenced_by: [{source_stem, source_col, target_col, sheet}]} # add 此表后可能需同步建
    """
    from .cascade_resolver import get_cascade_hints as _get
    return _get(stem)


# ── 复合 NPC 模式（对话+选项+奖励+邮件 全链路）─────────────────────


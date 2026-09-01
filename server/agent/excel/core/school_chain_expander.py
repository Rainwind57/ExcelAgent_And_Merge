"""门派全链确定性展开器（school full-chain deterministic expander）。

针对"新建门派"这一高频且高复杂策划意图，用规则确定性拆出完整跨表链，
不依赖 LLM（避免 serve 超时/漏意图）。产出 SplitIntent 列表，字段名用
row2 英文名（school/school_ability/school_spirit/school_talent/mail）。

链型：
  school.School (produces new_school_id, 引用 N 个 <new_ability{i}_id>)
  school_ability.SchoolAbility ×N (produces new_ability{i}_id)
  school_ability.SchoolAbilityLevel ×N (consumes <new_ability{i}_id>)
  school_spirit.SchoolSpirit ×N (灵根映射, consumes <new_ability{i}_id> + <new_school_id>)
  school_talent.SchoolTalent ×M (consumes <new_school_id>)
  school_talent.SchoolTalentLevel ×K (consumes <new_talent{k}_id>)
  mail.MailTemplate (produces new_template_id) + mail.GlobalMail (consumes <new_template_id>)

设计原则：只在高置信度命中"新建门派 + 门派编号/类型 + 神通"时触发，
避免误伤 LLM 能做好的简单意图（如纯邮件、字段级 modify）。
"""

from __future__ import annotations

import re
from typing import Optional

from .cross_table_splitter import SplitIntent


# ── 元素 → spirit_id 映射（金1 木2 水3 火4 土5）──
_ELEM_SPIRIT_ID = {"金": 1, "木": 2, "水": 3, "火": 4, "土": 5}

# 中文序数
_ORD = "一二三四五六七八九十"

_Q = r"['\"‘’“”]"  # 引号（含全角）


def _q(name: str) -> str:
    """构造引号包裹字段的正则片段。"""
    return rf"{_Q}(?P<{name}>[^'\"‘’“”]+){_Q}"


def is_school_new_chain(text: str) -> bool:
    """判定是否为"新建门派全链"意图。

    需同时满足：动作词+门派 + (门派编号|门派类型) + 至少提到神通。
    modify/delete 单点操作不命中（无门派编号/类型这类整体建表信号）。
    """
    if not text:
        return False
    t = text
    has_create = bool(re.search(r"(新增|新建|新开|开设|开)[^。；;]{0,12}门派", t)) \
        or "开宗立派" in t or bool(re.search(r"门派[^。；;]{0,6}叫", t))
    has_id = bool(re.search(r"门派编号|门派类型", t))
    has_ability = "神通" in t
    return has_create and has_id and has_ability


def _find(pattern: str, text: str, group: int = 1) -> Optional[str]:
    m = re.search(pattern, text)
    if not m:
        return None
    try:
        return m.group(group).strip()
    except Exception:
        return None


def _split_ability_blocks(text: str) -> list[str]:
    """按序数标记（第一个/第二个…神通|叫）切出每个神通的描述块。"""
    # 找到所有 "第X个" 起点
    marker = re.compile(rf"第[{_ORD}\d]+个(?:神通)?")
    starts = [m.start() for m in marker.finditer(text)]
    if not starts:
        return []
    # 每块从本 marker 到下一 marker（最后一块到灵根/天赋/邮件段或文本末）
    blocks: list[str] = []
    # 限制尾部：神通枚举通常在"这N个神通每级消耗"/"再把神通和灵根绑定"/"灵根映射"之前
    tail_m = re.search(r"这[几\d一二三四五六七八九十]+个神通|再把神通|灵根映射|灵根绑定|门派天赋", text)
    hard_end = tail_m.start() if tail_m else len(text)
    starts = [s for s in starts if s < hard_end]
    for i, s in enumerate(starts):
        e = starts[i + 1] if i + 1 < len(starts) else hard_end
        blocks.append(text[s:e])
    return blocks


def _parse_ability(block: str) -> Optional[dict]:
    """从单个神通块提取 name/desc + 两套心法 + 可选 per-ability 技能编号。"""
    # 名称：第X个神通'NAME' 或 第X个叫'NAME'
    nm = re.search(rf"(?:神通|叫)\s*{_q('name')}", block)
    if not nm:
        nm = re.search(_q("name"), block)
    if not nm:
        return None
    name = nm.group("name").strip()
    # 描述：第一个"描述'...'"（在心法之前）
    desc = None
    dm = re.search(rf"描述\s*{_q('d')}", block)
    if dm:
        desc = dm.group("d").strip()
    # 心法一/二
    m1n = None
    m1d = None
    m2n = None
    m2d = None
    m1 = re.search(rf"心法一\s*(?:叫)?\s*{_q('n')}\s*[，,]?\s*描述\s*{_q('d')}", block)
    if m1:
        m1n, m1d = m1.group("n").strip(), m1.group("d").strip()
    m2 = re.search(rf"心法二\s*(?:叫)?\s*{_q('n')}\s*[，,]?\s*描述\s*{_q('d')}", block)
    if m2:
        m2n, m2d = m2.group("n").strip(), m2.group("d").strip()
    # per-ability 技能编号
    spell = _find(r"技能编号\s*(\d+)", block)
    return {
        "name": name, "desc": desc,
        "m1n": m1n, "m1d": m1d, "m2n": m2n, "m2d": m2d,
        "spell": spell,
    }


def _parse_spell_ids(text: str, n: int) -> list[Optional[str]]:
    """解析全局"技能编号按顺序 901、902、903、904"，返回 n 个（不足补 None）。"""
    m = re.search(r"技能编号[^0-9]{0,8}((?:\d+\s*[、,，]?\s*){2,})", text)
    ids: list[str] = []
    if m:
        ids = re.findall(r"\d+", m.group(1))
    out: list[Optional[str]] = []
    for i in range(n):
        out.append(ids[i] if i < len(ids) else None)
    return out


def _parse_spirits(text: str) -> list[dict]:
    """解析灵根映射：'太虚剑意绑金灵根给天赋 600001'。"""
    out: list[dict] = []
    for m in re.finditer(
            rf"(?P<an>[^\s，,、；;。]+?)绑(?P<elem>[金木水火土])灵根(?:给天赋|天赋|给)\s*(?P<buff>\d+)",
            text):
        out.append({
            "ability_name": m.group("an").strip(),
            "elem": m.group("elem"),
            "buff": m.group("buff"),
        })
    return out


def _parse_talents(text: str) -> list[dict]:
    """解析门派天赋：主键/下层天赋 + 名字/描述/图标/层级/所在列/父列。"""
    # 缩到天赋段
    seg_m = re.search(r"门派天赋[^。]*", text)
    seg = text
    tm_start = re.search(r"门派天赋", text)
    if tm_start:
        # 天赋段到"再补"/"天赋等级"/"最后"/"邮件"之前
        end_m = re.search(r"再补|天赋等级|最后|邮件模板|发一封", text[tm_start.start():])
        end = tm_start.start() + end_m.start() if end_m else len(text)
        seg = text[tm_start.start():end]
    out: list[dict] = []
    for m in re.finditer(
            rf"(?:天赋主键|下层天赋|天赋)\s*(?P<tid>\d+)\s*[，,]?\s*名[字称]\s*{_q('tn')}"
            rf"\s*[，,]?\s*(?:功法描述|描述)\s*{_q('td')}"
            rf"(?:\s*[，,]?\s*图标\s*{_q('ic')})?",
            seg):
        chunk = seg[m.start():m.start() + 200]
        layer = _find(r"层级\s*(\d+)", chunk)
        column = _find(r"所在列\s*(\d+)", chunk)
        parent = _find(rf"上一层功法所在列填\s*{_Q}?(\d+){_Q}?", chunk)
        out.append({
            "tid": m.group("tid"),
            "name": m.group("tn").strip(),
            "desc": m.group("td").strip(),
            "icon": (m.group("ic") or "").strip() if m.groupdict().get("ic") else None,
            "layer": layer,
            "column": column,
            "parent": parent,
        })
    return out


def _parse_talent_levels(text: str) -> list[dict]:
    """解析天赋等级：'破军 1 级描述'伤害提升20%'被动 600003'。"""
    out: list[dict] = []
    for m in re.finditer(
            rf"(?P<tn>[^\s，,、；;。]+?)\s*(?P<lvl>\d+)\s*级\s*描述\s*{_q('d')}\s*"
            rf"(?:被动|buff|Buff)\s*(?P<buff>\d+)",
            text):
        out.append({
            "talent_name": m.group("tn").strip(),
            "level": m.group("lvl"),
            "desc": m.group("d").strip(),
            "buff": m.group("buff"),
        })
    return out


def _parse_mail(text: str) -> Optional[dict]:
    """解析门派开宗邮件（MailTemplate + GlobalMail）。"""
    if "邮件" not in text:
        return None
    title = _find(rf"标题\s*{_q('v')}", text, "v")
    content = _find(rf"内容\s*{_q('v')}", text, "v")
    if not title and not content:
        return None
    gid = _find(r"global_id\s*(\d+)", text) or _find(r"全服邮件[^0-9]{0,6}(\d+)", text)
    mail_type = _find(r"邮件类型\s*(\d+)", text) or "1"
    sender = _find(rf"发送人\s*{_q('v')}", text, "v") or _find(r"发送人\s*(\S+?)[，,。]", text)
    send_time = _find(r"发送时间\s*(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})", text)
    if "不带奖励" in text or "不附带奖励" in text:
        reward = "0"
    else:
        reward = _find(r"(?:附[带上]?奖励|奖励(?:包)?|reward_id)\D{0,4}(\d+)", text) or "0"
    return {
        "title": title, "content": content, "global_id": gid,
        "mail_type": mail_type, "sender": sender or "系统",
        "send_time": send_time, "reward": reward,
    }


def build_school_new_chain_intents(text: str) -> list[SplitIntent]:
    """新建门派全链 → SplitIntent[]。非命中或解析不足返回 []。"""
    if not is_school_new_chain(text):
        return []

    school_name = _find(rf"门派\s*(?:叫|名[字称]?为?|叫做)?\s*{_q('v')}", text, "v")
    if not school_name:
        school_name = _find(rf"叫\s*{_q('v')}", text, "v")
    school_id = _find(r"门派编号\s*(\d+)", text)
    school_type = _find(r"门派类型\s*(\d+)", text)
    model_id = _find(r"大世界模型\s*(\d+)", text)
    combat_model_id = _find(r"战斗模型\s*(\d+)", text)

    # 神通块
    blocks = _split_ability_blocks(text)
    abilities: list[dict] = []
    for b in blocks:
        a = _parse_ability(b)
        if a and a.get("name"):
            abilities.append(a)
    if not abilities:
        return []

    # 全局技能编号（若单块未带）
    global_spells = _parse_spell_ids(text, len(abilities))
    # 全局等级参数
    g_cost = _find(r"每级消耗灵石\s*(\d+)", text)
    g_upgrade = _find(r"一级升一级要人物\s*(\d+)\s*级", text) \
        or _find(r"人物\s*(\d+)\s*级", text)

    intents: list[SplitIntent] = []

    # 1) School（引用 N 个 ability produces）
    ability_labels = [f"new_ability{i+1}_id" for i in range(len(abilities))]
    school_fields: dict = {}
    # School 主键走自增产出（produces new_school_id）：门派编号可能与既有行冲突
    # （PK 冲突测例），显式 literal 会级联污染 talent/spirit 的 school_id 引用。
    # 用户显式编号保留在 extras 供 PK-冲突解析参考，不作为写盘 literal。
    school_fields["name"] = school_name or ""
    if school_type:
        school_fields["school_type"] = school_type
    if model_id:
        school_fields["model_id"] = model_id
    if combat_model_id:
        school_fields["combat_model_id"] = combat_model_id
    for i, lbl in enumerate(ability_labels):
        school_fields[f"school_ability_id[{i}]"] = f"<{lbl}>"
    intents.append(SplitIntent(
        text=f"新增门派 {school_name}", table_hint="school", sheet_hint="School",
        action="add", fields=school_fields, produces="new_school_id",
    ))

    # 2) 每个神通 SchoolAbility + SchoolAbilityLevel
    ability_name_to_label: dict[str, str] = {}
    for i, a in enumerate(abilities):
        lbl = ability_labels[i]
        ability_name_to_label[a["name"]] = lbl
        af: dict = {"name": a["name"]}
        if a.get("desc"):
            af["desc"] = a["desc"]
        af["unlock_level"] = "1"
        if a.get("m1n"):
            af["mental_1_name"] = a["m1n"]
        if a.get("m1d"):
            af["mental_1_desc"] = a["m1d"]
        if a.get("m2n"):
            af["mental_2_name"] = a["m2n"]
        if a.get("m2d"):
            af["mental_2_desc"] = a["m2d"]
        intents.append(SplitIntent(
            text=f"新增神通 {a['name']}", table_hint="school_ability",
            sheet_hint="SchoolAbility", action="add", fields=af, produces=lbl,
        ))
        # AbilityLevel（0 级）
        spell = a.get("spell") or global_spells[i]
        lf: dict = {
            "school_ability_id": f"<{lbl}>",
            "level": "0",
            "cost_num": (g_cost if g_cost is not None else "0"),
            "upgrade_level": (g_upgrade if g_upgrade is not None else "1"),
        }
        if spell:
            lf["common_spell_id"] = spell
        intents.append(SplitIntent(
            text=f"神通等级 {a['name']} 0级", table_hint="school_ability",
            sheet_hint="SchoolAbilityLevel", action="add", fields=lf,
        ))

    # 3) 灵根映射 SchoolSpirit
    for sp in _parse_spirits(text):
        lbl = ability_name_to_label.get(sp["ability_name"])
        if lbl is None:
            # 模糊匹配（截断/别名）
            for an, al in ability_name_to_label.items():
                if sp["ability_name"] in an or an in sp["ability_name"]:
                    lbl = al
                    break
        sf: dict = {
            "school_id": "<new_school_id>",
            "spirit_id": str(_ELEM_SPIRIT_ID.get(sp["elem"], 1)),
            "spirit_buffs[0]": sp["buff"],
        }
        if lbl:
            sf["school_ability_id"] = f"<{lbl}>"
        intents.append(SplitIntent(
            text=f"灵根映射 {sp['ability_name']}", table_hint="school_spirit",
            sheet_hint="SchoolSpirit", action="add", fields=sf,
        ))

    # 4) 门派天赋 SchoolTalent
    talents = _parse_talents(text)
    talent_id_to_label: dict[str, str] = {}
    talent_name_to_label: dict[str, str] = {}
    for k, t in enumerate(talents):
        lbl = f"new_talent{k+1}_id"
        talent_id_to_label[t["tid"]] = lbl
        talent_name_to_label[t["name"]] = lbl
        tf: dict = {
            "id": t["tid"],
            "name": t["name"],
            "desc": t["desc"],
            "school_id": "<new_school_id>",
        }
        if t.get("icon"):
            tf["icon"] = t["icon"]
        if t.get("layer"):
            tf["layer"] = t["layer"]
        if t.get("column"):
            tf["column"] = t["column"]
        if t.get("parent"):
            tf["parent_columns"] = f"[{t['parent']}]"
        intents.append(SplitIntent(
            text=f"门派天赋 {t['name']}", table_hint="school_talent",
            sheet_hint="SchoolTalent", action="add", fields=tf, produces=lbl,
        ))

    # 5) 天赋等级 SchoolTalentLevel
    for tl in _parse_talent_levels(text):
        lbl = talent_name_to_label.get(tl["talent_name"])
        if lbl is None:
            for tn, al in talent_name_to_label.items():
                if tl["talent_name"] in tn or tn in tl["talent_name"]:
                    lbl = al
                    break
        tlf: dict = {
            "desc": tl["desc"],
            "level": tl["level"],
            "buff_id": tl["buff"],
        }
        if lbl:
            tlf["talent_id"] = f"<{lbl}>"
        intents.append(SplitIntent(
            text=f"天赋等级 {tl['talent_name']} {tl['level']}级",
            table_hint="school_talent", sheet_hint="SchoolTalentLevel",
            action="add", fields=tlf,
        ))

    # 6) 邮件 MailTemplate + GlobalMail
    mail = _parse_mail(text)
    if mail:
        mtf: dict = {}
        if mail.get("title"):
            mtf["title"] = mail["title"]
        if mail.get("content"):
            mtf["content"] = mail["content"]
        intents.append(SplitIntent(
            text="新增邮件模板", table_hint="mail", sheet_hint="MailTemplate",
            action="add", fields=mtf, produces="new_template_id",
        ))
        gmf: dict = {
            "template_id": "<new_template_id>",
            "mail_type": mail.get("mail_type") or "1",
            "sender": mail.get("sender") or "系统",
            "reward_id": mail.get("reward") or "0",
        }
        if mail.get("global_id"):
            gmf["global_id"] = mail["global_id"]
        if mail.get("send_time"):
            gmf["send_time"] = mail["send_time"]
        intents.append(SplitIntent(
            text="新增全服邮件", table_hint="mail", sheet_hint="GlobalMail",
            action="add", fields=gmf,
        ))

    return intents

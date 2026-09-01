# -*- coding: utf-8 -*-
"""school_chain_expander 确定性展开器单测。

验证"新建门派全链"的规则拆分：命中判据、意图条数、跨表 produces/consumes 连线、
关键字段值（元素→spirit_id、天赋、邮件），以及对非门派意图的零误命中。
不依赖 LLM/serve，快且确定。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.excel.core.school_chain_expander import (
    build_school_new_chain_intents, is_school_new_chain, _ELEM_SPIRIT_ID)


CASE0 = (
    "新增一个门派叫'太虚剑宗'，门派编号 9，门派类型 1，大世界模型 1027，战斗模型 1074，"
    "先配四个神通：第一个神通'太虚剑意'，描述'以虚御实'，"
    "心法一叫'剑意·蓄'，描述'开局即得剑气'，心法二叫'剑意·绝'，描述'击杀获剑气'；"
    "第二个神通'裂空斩'，描述'撕开空间'，心法一叫'裂空·碎'，描述'暴击提升'，"
    "心法二叫'裂空·连'，描述'不进入休息'；"
    "第三个神通'万剑归宗'，描述'剑气齐射'，心法一叫'归宗·雨'，描述'范围扩大'，"
    "心法二叫'归宗·化劲'，描述'稳定伤害'；"
    "第四个神通'无相护体'，描述'无相罡气'，心法一叫'无相·觉'，描述'格挡提升'，"
    "心法二叫'无相·传'，描述'人宠护体'。"
    "这四个神通每级消耗灵石 0，一级升一级要人物 1 级，技能编号按顺序 901、902、903、904。"
    "再把神通和灵根绑定：太虚剑意绑金灵根给天赋 600001，裂空斩绑木灵根给天赋 600002，"
    "万剑归宗绑水灵根给天赋 600002，无相护体绑火灵根给天赋 600001。"
    "门派天赋盘配一个：天赋主键 9011，名字'破军'，功法描述'伤害提升20%'，"
    "图标'Icon_120_shentong_1.png'，门派 9，层级 1，所在列 1；"
    "它的下层天赋 9021，名字'贪狼'，功法描述'伤害提升30%'，图标'Icon_120_shentong_2.png'，"
    "门派 9，层级 2，所在列 1，上一层功法所在列填'1'。"
    "再补两条天赋等级：破军 1 级描述'伤害提升20%'被动 600003，"
    "破军 2 级描述'伤害提升40%'被动 600003。"
    "最后给新门派发一封开宗立派的全服邮件：邮件模板标题'太虚剑宗开宗立派'，"
    "内容'太虚剑宗今日开宗'，全服邮件 global_id 20，邮件类型 1，发送人'系统'，"
    "发送时间 2026-09-01 00:00:00，不带奖励。"
)


def _by_sheet(intents):
    d = {}
    for it in intents:
        d.setdefault((it.table_hint, it.sheet_hint), []).append(it)
    return d


def test_element_spirit_id_map():
    assert _ELEM_SPIRIT_ID["金"] == 1
    assert _ELEM_SPIRIT_ID["木"] == 2
    assert _ELEM_SPIRIT_ID["水"] == 3
    assert _ELEM_SPIRIT_ID["火"] == 4


def test_detection_positive_and_negative():
    assert is_school_new_chain(CASE0) is True
    # 纯字段级 modify 不命中
    assert is_school_new_chain("剑修的战斗模型换成 1075") is False
    # 纯邮件不命中
    assert is_school_new_chain("发一封全服邮件，标题'X'，global_id 21") is False
    # 删除神通不命中
    assert is_school_new_chain("把测试神通'TEST'下架掉") is False


def test_case0_full_chain_structure():
    its = build_school_new_chain_intents(CASE0)
    grp = _by_sheet(its)
    # 表/sheet 计数
    assert len(grp[("school", "School")]) == 1
    assert len(grp[("school_ability", "SchoolAbility")]) == 4
    assert len(grp[("school_ability", "SchoolAbilityLevel")]) == 4
    assert len(grp[("school_spirit", "SchoolSpirit")]) == 4
    assert len(grp[("school_talent", "SchoolTalent")]) == 2
    assert len(grp[("school_talent", "SchoolTalentLevel")]) == 2
    assert len(grp[("mail", "MailTemplate")]) == 1
    assert len(grp[("mail", "GlobalMail")]) == 1
    assert len(its) == 19


def test_case0_school_produces_and_ability_refs():
    its = build_school_new_chain_intents(CASE0)
    grp = _by_sheet(its)
    school = grp[("school", "School")][0]
    assert school.produces == "new_school_id"
    # School 自增主键（不写字面 school 值，避免 PK 冲突级联）
    assert "school" not in school.fields
    # 引用四个 ability produces 占位
    for i in range(4):
        assert school.fields[f"school_ability_id[{i}]"] == f"<new_ability{i+1}_id>"


def test_case0_ability_levels_consume_and_spells():
    its = build_school_new_chain_intents(CASE0)
    grp = _by_sheet(its)
    levels = grp[("school_ability", "SchoolAbilityLevel")]
    spells = sorted(lv.fields.get("common_spell_id") for lv in levels)
    assert spells == ["901", "902", "903", "904"]
    for i, lv in enumerate(levels):
        assert lv.fields["school_ability_id"] == f"<new_ability{i+1}_id>"
        assert lv.fields["level"] == "0"
        assert lv.fields["cost_num"] == "0"
        assert lv.fields["upgrade_level"] == "1"
        # 等级不再挂 produces（避免 _level 子串误判 producer cycle）
        assert lv.produces is None


def test_case0_spirit_element_mapping():
    its = build_school_new_chain_intents(CASE0)
    grp = _by_sheet(its)
    spirits = grp[("school_spirit", "SchoolSpirit")]
    # 金1木2水3火4，buff 600001/600002/600002/600001
    got = [(s.fields["spirit_id"], s.fields["spirit_buffs[0]"],
            s.fields["school_ability_id"]) for s in spirits]
    assert got == [
        ("1", "600001", "<new_ability1_id>"),
        ("2", "600002", "<new_ability2_id>"),
        ("3", "600002", "<new_ability3_id>"),
        ("4", "600001", "<new_ability4_id>"),
    ]
    for s in spirits:
        assert s.fields["school_id"] == "<new_school_id>"


def test_case0_talents_and_levels():
    its = build_school_new_chain_intents(CASE0)
    grp = _by_sheet(its)
    talents = grp[("school_talent", "SchoolTalent")]
    assert [t.fields["id"] for t in talents] == ["9011", "9021"]
    assert talents[0].fields["name"] == "破军"
    assert talents[1].fields["parent_columns"] == "[1]"
    for t in talents:
        assert t.fields["school_id"] == "<new_school_id>"
    levels = grp[("school_talent", "SchoolTalentLevel")]
    # 两条天赋等级都挂在 破军(new_talent1_id)
    for lv in levels:
        assert lv.fields["talent_id"] == "<new_talent1_id>"
    assert sorted(lv.fields["level"] for lv in levels) == ["1", "2"]


def test_case0_mail_chain():
    its = build_school_new_chain_intents(CASE0)
    grp = _by_sheet(its)
    tmpl = grp[("mail", "MailTemplate")][0]
    gm = grp[("mail", "GlobalMail")][0]
    assert tmpl.produces == "new_template_id"
    assert tmpl.fields["title"] == "太虚剑宗开宗立派"
    assert gm.fields["template_id"] == "<new_template_id>"
    assert gm.fields["global_id"] == "20"
    assert gm.fields["reward_id"] == "0"  # 不带奖励
    assert gm.fields["sender"] == "系统"


def test_non_school_returns_empty():
    assert build_school_new_chain_intents("把'驭风'这条神通的描述改成'狂风'") == []
    assert build_school_new_chain_intents("开一个限时活动叫'九霄论剑'") == []

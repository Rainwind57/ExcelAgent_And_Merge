from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.cross_table_splitter import SplitIntent
from agent.excel.parse_agent import ParseAgent
from agent.excel.subagent.locator_agent import FKEdge


class _TinyCli:
    def list_tables(self):
        class P:
            def __init__(self, stem):
                self.stem = stem
        return [P("mail"), P("school_ability"), P("school_talent"), P("school_spirit")]

    def read_header(self, _path, sheet):
        if sheet == "SchoolAbility":
            return ["ability id", "name", "desc"]
        if sheet == "SchoolAbilityLevel":
            return ["id", "ability id", "level", "spell"]
        if sheet == "SchoolTalentLevel":
            return ["id", "desc", "talent id", "level", "buff"]
        if sheet == "SchoolSpirit":
            return ["ability id", "school id", "spirit id", "buff"]
        if sheet == "GlobalMail":
            return ["全服邮件ID", "模板ID", "奖励"]
        if sheet == "MailTemplate":
            return ["模板ID", "标题", "内容"]
        return []

    def read_type_row(self, _path, sheet):
        if sheet == "SchoolAbility":
            return ["school_ability_id:int", "name:string", "desc:string"]
        if sheet == "SchoolAbilityLevel":
            return ["id:int", "school_ability_id:int", "level:int", "common_spell_id:int"]
        if sheet == "SchoolTalentLevel":
            return ["id:int", "desc:string", "talent_id:int", "level:int", "buff_id:int"]
        if sheet == "SchoolSpirit":
            return ["school_ability_id:int", "school_id:int", "spirit_id:int", "spirit_buffs[0]:int"]
        if sheet == "GlobalMail":
            return ["global_id:int", "template_id:int", "reward_id:int"]
        if sheet == "MailTemplate":
            return ["template_id:int", "title:string", "content:string"]
        return []


def test_split_to_nl_preserves_consumes_labels_from_placeholders():
    split = SplitIntent(
        text="add child row",
        table_hint="child",
        sheet_hint="Child",
        action="add",
        fields={"parent_id": "<new_parent_id>", "name": "child"},
    )

    out = ParseAgent().parse_baseline("add parent and child", [split])

    assert len(out) == 1
    assert out[0].consumes_labels == ["new_parent_id"]


def test_split_to_nl_copies_fields_to_avoid_aliasing():
    split = SplitIntent(
        text="add row",
        table_hint="demo",
        sheet_hint="Demo",
        action="add",
        fields={"name": "before"},
    )

    out = ParseAgent().parse_baseline("add row", [split])
    split.fields["name"] = "after"

    assert out[0].extras["fields"] == {"name": "before"}


def test_parse_baseline_dedupes_identical_intents():
    split = SplitIntent(
        text="set row",
        table_hint="entity_prefab",
        sheet_hint="Base",
        action="set",
        fields={"名字": "青龙堂主"},
        locator_field="prefab_id",
        locator_value="8004",
    )

    out = ParseAgent().parse_baseline("set row", [split, split])

    assert len(out) == 1


def test_parse_baseline_dedupes_semantic_retry_duplicates():
    a = SplitIntent(
        text="add activity",
        table_hint="activity",
        sheet_hint="Activity",
        action="add",
        fields={"活动id": 3060, "活动名称": "九霄论剑", "开始时间": "2026-11-01 00:00:00"},
    )
    b = SplitIntent(
        text="add activity",
        table_hint="activity",
        sheet_hint="Activity",
        action="add",
        fields={"活动编号": 3060, "活动名称": "九霄论剑", "开始时间": "2026-11-01 00:00:00"},
        produces="new_activity_id",
    )

    out = ParseAgent().parse_baseline("add activity", [a, b])

    assert len(out) == 1
    assert out[0].produces_label is None


def test_parse_baseline_keeps_distinct_batch_rows():
    splits = [
        SplitIntent(
            text="tips 1",
            table_hint="tips",
            sheet_hint="tips",
            action="add",
            fields={"value": "背包已满", "key": "BAG_FULL", "type": "tips"},
        ),
        SplitIntent(
            text="tips 2",
            table_hint="tips",
            sheet_hint="tips",
            action="add",
            fields={"value": "金币不足", "key": "GOLD_LACK", "type": "tips"},
        ),
    ]

    out = ParseAgent().parse_baseline("add tips", splits)

    assert len(out) == 2


def test_parse_baseline_drops_empty_add_shadow_when_non_empty_sibling_exists():
    empty = SplitIntent(
        text="tips empty",
        table_hint="tips",
        sheet_hint="tips",
        action="add",
        fields={"value": "", "key": "", "type": ""},
    )
    filled = SplitIntent(
        text="tips filled",
        table_hint="tips",
        sheet_hint="tips",
        action="add",
        fields={"value": "背包已满", "key": "BAG_FULL", "type": "tips"},
    )

    out = ParseAgent().parse_baseline("add tips", [empty, filled])

    assert len(out) == 1
    assert out[0].extras["fields"]["key"] == "BAG_FULL"


def test_parse_baseline_keeps_single_empty_add_intent():
    empty = SplitIntent(
        text="unknown add",
        table_hint="demo",
        sheet_hint="Demo",
        action="add",
        fields={"name": ""},
    )
    out = ParseAgent().parse_baseline("add unknown", [empty])

    assert len(out) == 1


def test_parse_baseline_keeps_producer_placeholder_row_not_dropped_as_shadow():
    """已挂 produces 的 producer 占位行豁免 shadow 去重。

    sparse 有 produces=new_global_mail_id（producer 占位行，主键自增、字段含
    FK 占位符），complete 无 produces（纯数据行）。新语义 produces 非空豁免
    shadow 删 → 两行都保留（原测试期望 sparse 被删删剩 1，改为保留 2）。
    """
    sparse = SplitIntent(
        text="global mail sparse",
        table_hint="mail",
        sheet_hint="GlobalMail",
        action="add",
        fields={"global_id": "<new_global_mail_id>", "mail_type": 1, "sender": ""},
        produces="new_global_mail_id",
    )
    complete = SplitIntent(
        text="global mail complete",
        table_hint="mail",
        sheet_hint="GlobalMail",
        action="add",
        fields={"global_id": 21, "mail_type": 1, "sender": "系统", "reward_id": 10001},
    )

    out = ParseAgent().parse_baseline("add global mail", [sparse, complete])

    assert len(out) == 2  # producer 占位行豁免 shadow 去重，两行都保留
    labels = [it.extras.get("produces") for it in out]
    assert "new_global_mail_id" in labels  # producer 保留


def test_backfill_missing_fk_fields_when_single_producer_exists():
    parent = ParseAgent().parse_baseline("add template", [
        SplitIntent(
            text="template",
            table_hint="mail",
            sheet_hint="MailTemplate",
            action="add",
            fields={"template_id": "<new_template_id>", "title": "A"},
            produces="new_template_id",
        )
    ])[0]
    child = ParseAgent().parse_baseline("add global", [
        SplitIntent(
            text="global",
            table_hint="mail",
            sheet_hint="GlobalMail",
            action="add",
            fields={"global_id": 21, "mail_type": 1},
        )
    ])[0]
    edge = FKEdge("mail", "GlobalMail", "template_id",
                  "mail", "MailTemplate", "template_id")

    n = ParseAgent._backfill_missing_fk_fields([parent, child], [edge])

    assert n == 1
    assert child.extras["fields"]["template_id"] == "<new_template_id>"
    assert child.consumes_labels == ["new_template_id"]


def test_resolve_same_batch_name_fk_to_placeholder():
    pa = ParseAgent(cli=_TinyCli())
    ability = pa.parse_baseline("add ability", [
        SplitIntent(
            text="ability",
            table_hint="school_ability",
            sheet_hint="SchoolAbility",
            action="add",
            fields={"school_ability_id": "<new_ability1_id>", "name": "Taixu Sword"},
            produces="new_ability1_id",
        )
    ])[0]
    spirit = pa.parse_baseline("bind spirit", [
        SplitIntent(
            text="spirit",
            table_hint="school_spirit",
            sheet_hint="SchoolSpirit",
            action="add",
            fields={"school_ability_id": "Taixu Sword", "spirit_id": "Gold Root"},
        )
    ])[0]

    class LR:
        fk_edges = [
            FKEdge("school_spirit", "SchoolSpirit", "school_ability_id",
                   "school_ability", "SchoolAbility", "school_ability_id")
        ]

    n = pa._resolve_same_batch_name_refs([ability, spirit], LR())

    assert n == 1
    assert spirit.extras["fields"]["school_ability_id"] == "<new_ability1_id>"
    assert spirit.extras["fields"]["spirit_id"] == "Gold Root"
    assert spirit.consumes_labels == ["new_ability1_id"]


def test_prune_and_remap_fields_against_selected_sheet_schema():
    pa = ParseAgent(cli=_TinyCli())
    ability_level = pa.parse_baseline("level", [
        SplitIntent(
            text="level",
            table_hint="school_ability",
            sheet_hint="SchoolAbilityLevel",
            action="add",
            fields={
                "name": "Taixu Sword",
                "desc": "long text",
                "school_ability_id": "<new_ability1_id>",
                "level": 0,
            },
        )
    ])[0]
    talent_level = pa.parse_baseline("talent", [
        SplitIntent(
            text="talent",
            table_hint="school_talent",
            sheet_hint="SchoolTalentLevel",
            action="add",
            fields={"school_talent_id": "<new_talent_id>", "desc": "damage up", "level": 1},
        )
    ])[0]

    n = pa._prune_fields_not_in_schema([ability_level, talent_level])

    assert n == 0
    assert "name" not in ability_level.extras["fields"]
    assert "desc" not in ability_level.extras["fields"]
    assert ability_level.extras["fields"]["school_ability_id"] == "<new_ability1_id>"
    assert "school_talent_id" not in talent_level.extras["fields"]
    assert talent_level.extras["fields"]["talent_id"] == "<new_talent_id>"
    assert talent_level.extras["fields"]["desc"] == "damage up"


def test_prune_keeps_fields_when_nothing_matches_schema():
    pa = ParseAgent(cli=_TinyCli())
    intent = pa.parse_baseline("school", [
        SplitIntent(
            text="school",
            table_hint="school_ability",
            sheet_hint="SchoolAbilityLevel",
            action="add",
            fields={"unknown_name": "Taixu Sword", "unknown_desc": "long text"},
        )
    ])[0]

    assert intent.extras["fields"] == {
        "unknown_name": "Taixu Sword",
        "unknown_desc": "long text",
    }


def test_resolve_ordinal_placeholder_by_fk_target_order():
    pa = ParseAgent(cli=_TinyCli())
    ability1 = pa.parse_baseline("a1", [
        SplitIntent(
            text="a1",
            table_hint="school_ability",
            sheet_hint="SchoolAbility",
            action="add",
            fields={"school_ability_id": "<new_ability1_id>", "name": "A1"},
            produces="new_ability1_id",
        )
    ])[0]
    ability2 = pa.parse_baseline("a2", [
        SplitIntent(
            text="a2",
            table_hint="school_ability",
            sheet_hint="SchoolAbility",
            action="add",
            fields={"school_ability_id": "<new_ability2_id>", "name": "A2"},
            produces="new_ability2_id",
        )
    ])[0]
    spirit = pa.parse_baseline("bind", [
        SplitIntent(
            text="bind",
            table_hint="school_spirit",
            sheet_hint="SchoolSpirit",
            action="add",
            fields={"school_ability_id": "<new_school_ability_id_2>", "spirit_id": 2},
        )
    ])[0]
    edge = FKEdge("school_spirit", "SchoolSpirit", "school_ability_id",
                  "school_ability", "SchoolAbility", "school_ability_id")

    n = pa._resolve_ordinal_placeholders([ability1, ability2, spirit], [edge])

    assert n == 1
    assert spirit.extras["fields"]["school_ability_id"] == "<new_ability2_id>"
    assert spirit.consumes_labels == ["new_ability2_id"]


def test_backfill_same_workbook_placeholder_fields_by_shared_header():
    pa = ParseAgent(cli=_TinyCli())
    parent = pa.parse_baseline("add template", [
        SplitIntent(
            text="template",
            table_hint="mail",
            sheet_hint="MailTemplate",
            action="add",
            fields={"模板ID": "<new_template_id>", "标题": "A"},
            produces="new_template_id",
        )
    ])[0]
    child = pa.parse_baseline("add global", [
        SplitIntent(
            text="global",
            table_hint="mail",
            sheet_hint="GlobalMail",
            action="add",
            fields={"全服邮件ID": 21, "奖励": 10001},
        )
    ])[0]

    n = pa._backfill_same_workbook_placeholder_fields([parent, child])

    assert n == 1
    assert child.extras["fields"]["模板ID"] == "<new_template_id>"
    assert child.consumes_labels == ["new_template_id"]


def test_sparse_single_value_adds_collapse_as_semantic_duplicate():
    """同表 add 只有 1 个具体值(其余占位符)且值完全相同 → 判重，保留信息更全的一条。

    铁匠老张例：LLM 对同一对话节点重复 add 3 次 InteractionConv，每条只填
    prompt_text 一个具体值(其余是占位符/未填)——原判重要求双边≥2个具体值才
    比较值重叠，单值 add 全数逃过 → 3 条矛盾候选全部存活。放宽后：双边具体
    值集合完全相同(且非空)也判重。
    """
    thin = SplitIntent(
        text="conv thin",
        table_hint="interaction",
        sheet_hint="InteractionConv",
        action="add",
        fields={"conv_id": "<new_interaction_conv_id_1>",
                "prompt_text": "要打造装备吗？"},
    )
    richer = SplitIntent(
        text="conv richer",
        table_hint="interaction",
        sheet_hint="InteractionConv",
        action="add",
        fields={"conv_id": "<new_interaction_conv_id_1>",
                "prompt_text": "要打造装备吗？",
                "options[0]": "<new_interaction_conv_option_id_1>"},
        produces="new_interaction_conv_id_1",
    )

    out = ParseAgent().parse_baseline("add conv", [thin, richer])

    assert len(out) == 1  # 两条判重收敛为 1
    assert out[0].extras["fields"].get("options[0]") == "<new_interaction_conv_option_id_1>"


def test_sparse_dedup_does_not_collapse_different_single_values():
    """单值 add 但值不同(不同实体) → 仍不判重，不误杀真正不同的行。"""
    a = SplitIntent(
        text="conv a",
        table_hint="interaction",
        sheet_hint="InteractionConv",
        action="add",
        fields={"conv_id": "<new_interaction_conv_id_1>", "prompt_text": "要打造装备吗？"},
    )
    b = SplitIntent(
        text="conv b",
        table_hint="interaction",
        sheet_hint="InteractionConv",
        action="add",
        fields={"conv_id": "<new_interaction_conv_id_2>", "prompt_text": "焚天谷在山门正东"},
    )

    out = ParseAgent().parse_baseline("add conv", [a, b])

    assert len(out) == 2  # 值不同，不判重


def test_repair_self_ref_placeholder_typo_underscore_shift():
    """自引用占位符下划线错位 typo（con_voption vs conv_option）应被修正。

    场景：intent 的 produces="new_interaction_conv_option_id_1"，同一行内
    option_id 字段自引用却打成 "<new_interaction_con_voption_id_1>"（下划线
    从 conv|option 边界挪到 con|voption）。去下划线规范形相同("newinteraction
    convoptionid")，本层识别为 typo 并改写为正确拼写。
    """
    pa = ParseAgent()
    from agent.excel.parser.nl_parser import NLIntent
    it = NLIntent(
        action="add", table_hint="interaction", sheet_hint="InteractionConvOption",
        raw="x",
        extras={"fields": {
                "option_id": "<new_interaction_con_voption_id_1>",
                "option_text": "是的",
            },
            "produces": "new_interaction_conv_option_id_1"},
    )
    it.produces_label = "new_interaction_conv_option_id_1"

    n = pa._repair_self_ref_placeholder_typos([it])

    assert n == 1
    assert it.extras["fields"]["option_id"] == "<new_interaction_conv_option_id_1>"


def test_repair_self_ref_placeholder_typo_skips_ambiguous():
    """两个 produces label 去下划线规范形撞车时不猜，保留原引用。"""
    pa = ParseAgent()
    from agent.excel.parser.nl_parser import NLIntent
    a = NLIntent(
        action="add", table_hint="t", sheet_hint="A", raw="x",
        extras={"fields": {}, "produces": "new_ab_id_1"},
    )
    a.produces_label = "new_ab_id_1"
    b = NLIntent(
        action="add", table_hint="t", sheet_hint="B", raw="x",
        extras={"fields": {}, "produces": "new_a_bid_1"},
    )
    b.produces_label = "new_a_bid_1"
    c = NLIntent(
        action="add", table_hint="t", sheet_hint="C", raw="x",
        extras={"fields": {"ref": "<new_a_b_id_1>"}},
    )

    n = pa._repair_self_ref_placeholder_typos([a, b, c])

    assert n == 0
    assert c.extras["fields"]["ref"] == "<new_a_b_id_1>"  # 歧义，不改写


def test_drop_islands_disconnected_from_anchor_removes_hallucinated_tables():
    """"新增npc叫做TEST"样例：LLM 幻觉展开出与锚点表无本批连通证据的孤岛表，

    应被丢弃，只保留 spawn_hint 锚点(entity_prefab)本身。
    """
    pa = ParseAgent()
    from agent.excel.parser.nl_parser import NLIntent
    anchor = NLIntent(
        action="add", table_hint="entity_prefab", sheet_hint="Base", raw="x",
        extras={"fields": {"entity_name": "TEST"}, "produces": "new_entity_prefab_id_1"},
    )
    anchor.produces_label = "new_entity_prefab_id_1"
    quest = NLIntent(
        action="add", table_hint="quest", sheet_hint="Quest", raw="x",
        extras={"fields": {"name": "TEST", "npc_ids": "<new_quest_id_1>"},
                "produces": "new_quest_id_1"},
    )
    quest.produces_label = "new_quest_id_1"
    model = NLIntent(
        action="add", table_hint="model_prefab", sheet_hint="MetaData", raw="x",
        extras={"fields": {"name": "TEST"}, "produces": "new_model_prefab_id_1"},
    )
    model.produces_label = "new_model_prefab_id_1"
    combat = NLIntent(
        action="add", table_hint="combat", sheet_hint="combat_data", raw="x",
        extras={"fields": {"name": "TEST", "npc_ids[0]": "<new_npc_id_1>"},
                "produces": "new_combat_id_1"},
    )
    combat.consumes_labels = ["new_npc_id_1"]

    out = pa._drop_islands_disconnected_from_anchor(
        [anchor, quest, model, combat], fk_edges=[], spawn_hint="entity_prefab")

    assert len(out) == 1
    assert out[0] is anchor


def test_drop_islands_keeps_fk_connected_secondary_table():
    """锚点与其他表之间有真实 FK 边(即便不同 sheet)时，不能被误删。"""
    pa = ParseAgent()
    from agent.excel.parser.nl_parser import NLIntent
    anchor = NLIntent(
        action="add", table_hint="entity_prefab", sheet_hint="Base", raw="x",
        extras={"fields": {"entity_name": "TEST", "交互id": "<new_interaction_id_1>"},
                "produces": "new_entity_prefab_id_1"},
    )
    anchor.produces_label = "new_entity_prefab_id_1"
    anchor.consumes_labels = ["new_interaction_id_1"]
    conv = NLIntent(
        action="add", table_hint="interaction", sheet_hint="Interaction", raw="x",
        extras={"fields": {"编号": "<new_interaction_id_1>"},
                "produces": "new_interaction_id_1"},
    )
    conv.produces_label = "new_interaction_id_1"
    edge = FKEdge("entity_prefab", "Base", "交互id",
                  "interaction", "Interaction", "编号")

    out = pa._drop_islands_disconnected_from_anchor(
        [anchor, conv], fk_edges=[edge], spawn_hint="entity_prefab")

    assert len(out) == 2


def test_drop_islands_noop_without_spawn_hint():
    """无 spawn_hint 锚点信号时保守跳过，不删任何表（防误伤）。"""
    pa = ParseAgent()
    from agent.excel.parser.nl_parser import NLIntent
    a = NLIntent(action="add", table_hint="entity_prefab", sheet_hint="Base", raw="x",
                 extras={"fields": {"entity_name": "TEST"}})
    b = NLIntent(action="add", table_hint="quest", sheet_hint="Quest", raw="x",
                 extras={"fields": {"name": "TEST"}})

    out = pa._drop_islands_disconnected_from_anchor([a, b], fk_edges=[], spawn_hint=None)

    assert len(out) == 2

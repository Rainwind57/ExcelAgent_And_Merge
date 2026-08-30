from __future__ import annotations

import os
import sys
from pathlib import Path
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.cli_interface import StubCodeMakerCLI
from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.locator_agent import CandidateTable, FKEdge


RESOURCES = Path(__file__).resolve().parents[2] / "resources"


class _KvtCli:
    def __init__(self):
        class _P:
            def __init__(self, stem):
                self.stem = stem
        self._paths = [_P("tips"), _P("guild")]
        self._schemas = {
            ("tips", "tips"): (["value", "key", "type"],
                              ["value:string", "key:string", "type:string"]),
            ("guild", "Const"): (["value", "key", "type"],
                                 ["value:string", "key:string", "type:string"]),
        }

    def list_tables(self):
        return self._paths

    def get_sheets(self, path):
        return ["tips"] if path.stem == "tips" else ["Const"]

    def read_header(self, path, sheet):
        return self._schemas[(path.stem, sheet)][0]

    def read_type_row(self, path, sheet):
        return self._schemas[(path.stem, sheet)][1]


def test_key_value_type_baseline_routes_by_unquoted_intent_text():
    text = (
        "帮我把几条游戏内提示文案配一下。"
        "第一条：'帮派资金不足，无法升级该建筑'，key 用 TID_TIPS_GUILD_FUNDS_LACK，类型 tips；"
        "第二条：'当前网络波动，请稍后重试'，key 用 TID_TIPS_NETWORK_RETRY，类型 tips。"
    )
    candidates = [
        CandidateTable(stem="guild", sheet="Const", confidence=0.95,
                       level="alias", matched_term="帮派"),
        CandidateTable(stem="tips", sheet="tips", confidence=0.8,
                       level="alias", matched_term="提示文案"),
    ]

    intents = DecomposeAgent(parser=object(), cli=_KvtCli())._splitter_baseline(
        text, candidates, [])

    assert len(intents) == 2
    assert {it.table_hint for it in intents} == {"tips"}
    assert [it.fields["key"] for it in intents] == [
        "TID_TIPS_GUILD_FUNDS_LACK",
        "TID_TIPS_NETWORK_RETRY",
    ]
    assert intents[0].fields["value"] == "帮派资金不足，无法升级该建筑"
    assert intents[1].fields["type"] == "tips"


def test_splitter_baseline_does_not_append_empty_candidate_shells_after_template():
    text = (
        "新增一个NPC叫铁匠老张，model_id为1015，放在space_id 10008的场景坐标(60,0,30)，"
        "玩家点击后弹出对话，对话内容为'欢迎来到铁匠铺，我可以帮你锻造装备。'，"
        "选项为'好的，我要锻造'和'离开'"
    )
    candidates = [
        CandidateTable(stem="entity_prefab", sheet="Base", confidence=1.0),
        CandidateTable(stem="interaction", sheet="Interaction", confidence=1.0),
        CandidateTable(stem="spawn_world_entity", sheet="SpawnWorldEntity", confidence=1.0),
        CandidateTable(stem="item", sheet="", confidence=0.4),
        CandidateTable(stem="space", sheet="", confidence=0.4),
        CandidateTable(stem="reward", sheet="", confidence=0.4),
        CandidateTable(stem="spell", sheet="", confidence=0.4),
    ]
    fk_edges = [
        FKEdge("spawn_world_entity", "SpawnWorldEntity", "entity_prefab_id",
               "entity_prefab", "Base", "prefab_id"),
        FKEdge("interaction", "Interaction", "effect.data.3006.conv_id",
               "interaction", "InteractionConv", "conv_id"),
    ]

    intents = DecomposeAgent(parser=object())._splitter_baseline(
        text, candidates, fk_edges)

    actual = {(it.table_hint, it.sheet_hint) for it in intents}
    assert ("entity_prefab", "Base") in actual
    assert ("interaction", "Interaction") in actual
    assert ("spawn_world_entity", "SpawnWorldEntity") in actual
    assert not any(it.table_hint in {"item", "space", "reward", "spell"}
                   and not it.fields for it in intents)
    prefab = next(it for it in intents if it.table_hint == "entity_prefab")
    spawn = next(it for it in intents if it.table_hint == "spawn_world_entity")
    conv = next(it for it in intents
                if it.table_hint == "interaction" and it.sheet_hint == "InteractionConv")
    assert prefab.fields["实体类型"] == "WorldNonPlayer"
    assert prefab.fields["model_prefab"] == "1015"
    assert conv.fields["对话内容"] == "欢迎来到铁匠铺，我可以帮你锻造装备。"
    assert spawn.fields["刷新ID"] == "<new_spawn_id>"
    assert spawn.produces == "new_spawn_id"
    assert spawn.fields["候选坐标"] == [[60, 0, 30]]


def test_splitter_baseline_preserves_set_action_and_locator():
    text = "把entity_prefab中prefab_id为8004的NPC名字从'青龙'改成'青龙堂主'"
    candidates = [
        CandidateTable(stem="entity_prefab", sheet="Base", confidence=1.0),
        CandidateTable(stem="interaction", sheet="Interaction", confidence=0.4),
    ]

    intents = DecomposeAgent(parser=object())._splitter_baseline(text, candidates, [])

    assert len(intents) == 1
    assert intents[0].table_hint == "entity_prefab"
    assert intents[0].action == "set"
    assert intents[0].locator_field == "prefab_id"
    assert intents[0].locator_value == "8004"
    assert intents[0].fields == {"名字": "青龙堂主"}


def test_splitter_baseline_preserves_delete_action_for_mentioned_related_tables():
    text = "删除prefab_id为8005的NPC白虎的所有相关配置，包括entity_prefab表和spawn_world_entity表中的记录"
    candidates = [
        CandidateTable(stem="entity_prefab", sheet="Base", confidence=1.0),
        CandidateTable(stem="spawn_world_entity", sheet="SpawnWorldEntity", confidence=1.0),
        CandidateTable(stem="interaction", sheet="Interaction", confidence=0.4),
    ]
    fk_edges = [
        FKEdge("spawn_world_entity", "SpawnWorldEntity", "entity_prefab_id",
               "entity_prefab", "Base", "prefab_id"),
    ]

    intents = DecomposeAgent(parser=object())._splitter_baseline(
        text, candidates, fk_edges)

    by_table = {it.table_hint: it for it in intents}
    assert set(by_table) == {"entity_prefab", "spawn_world_entity"}
    assert by_table["entity_prefab"].action == "delete"
    assert by_table["entity_prefab"].locator_field == "prefab_id"
    assert by_table["entity_prefab"].locator_value == "8005"
    assert by_table["spawn_world_entity"].action == "delete"
    assert by_table["spawn_world_entity"].locator_field == "entity_prefab_id"
    assert by_table["spawn_world_entity"].locator_value == "8005"


def test_splitter_baseline_adds_explicit_table_name_missed_by_locator():
    text = "删除prefab_id为8005的NPC白虎的所有相关配置，包括entity_prefab表和spawn_world_entity表中的记录"
    candidates = [
        CandidateTable(stem="entity_prefab", sheet="Base", confidence=1.0),
        CandidateTable(stem="interaction", sheet="Interaction", confidence=0.4),
    ]
    fk_edges = [
        FKEdge("spawn_world_entity", "SpawnWorldEntity", "entity_prefab_id",
               "entity_prefab", "Base", "prefab_id"),
    ]

    intents = DecomposeAgent(
        parser=object(),
        cli=StubCodeMakerCLI(workspace=RESOURCES),
    )._splitter_baseline(text, candidates, fk_edges)

    by_table = {it.table_hint: it for it in intents}
    assert "spawn_world_entity" in by_table
    assert by_table["spawn_world_entity"].action == "delete"
    assert by_table["spawn_world_entity"].sheet_hint == "SpawnWorldEntity"


def test_splitter_baseline_reward_dialogue_uses_stable_option_3_chain():
    text = (
        "新增一个道具商人叫'云游商人'，model_id 1021，放在space_id 10001坐标(30,0,40)，"
        "玩家点击后弹出对话'欢迎光临，要不要看看我的货物？'，选项'好的，看看'和'下次再来'，"
        "点击'好的，看看'后获得reward_id 10066的奖励包"
    )
    candidates = [
        CandidateTable(stem="entity_prefab", sheet="Base", confidence=1.0),
        CandidateTable(stem="interaction", sheet="Interaction", confidence=1.0),
        CandidateTable(stem="spawn_world_entity", sheet="SpawnWorldEntity", confidence=1.0),
    ]

    intents = DecomposeAgent(parser=object())._splitter_baseline(text, candidates, [])

    reward_conv = next(
        it for it in intents
        if it.table_hint == "interaction"
        and it.sheet_hint == "InteractionConv"
        and it.produces == "new_reward_conv_id"
    )
    option_3 = next(it for it in intents if it.produces == "option_3_id")
    first_option = next(it for it in intents if it.produces == "option_1_id")

    assert first_option.fields["option_function.data.1.conv_id"] == "<new_reward_conv_id>"
    assert reward_conv.fields["编号"] == "<new_reward_conv_id>"
    assert reward_conv.fields["选项1"] == "<option_3_id>"
    assert option_3.fields["编号"] == "<option_3_id>"
    assert option_3.fields["选项内容"] == "多谢"
    assert option_3.fields["option_function.data.1.reward_id"] == "10066"


def test_decompose_prompt_injects_domain_few_shots_without_parser_hook():
    da = DecomposeAgent(parser=object())
    schema = "\n".join([
        "- school/School: school | name | school_ability_id[0]",
        "- school_ability/SchoolAbility: school_ability_id | name | desc",
        "- school_spirit/SchoolSpirit: school_id | school_ability_id | spirit_buffs[0]",
        "- mail/MailTemplate: template_id | title | content",
        "- mail/GlobalMail: global_id | template_id | reward_id",
        "- tips/tips: value | key | type",
        "- activity/Activity: id | activity_type | name | desc | start_time | end_time",
    ])

    prompt = da._build_prompt(
        "新建门派并配神通、灵根映射、全服邮件；再配 tips 和限时活动。",
        schema,
        "school_spirit.SchoolSpirit.school_id -> school.School.school",
    )

    assert "few-shot: two-table forward reference" in prompt
    assert "few-shot: homogeneous batch rows" in prompt
    assert "few-shot: single activity row" in prompt
    assert "few-shot: school parent-child chain" in prompt
    assert "真实表头列名" in prompt
    assert "Pattern notes" in prompt


def test_parse_json_array_accepts_common_object_wrappers():
    da = DecomposeAgent(parser=object())
    raw = json.dumps({
        "intents": [
            {
                "table": "tips",
                "sheet": "tips",
                "action": "add",
                "fields": {"key": "A", "value": "B", "type": "tips"},
            }
        ]
    }, ensure_ascii=False)

    arr = da._parse_json_array(raw)

    assert len(arr) == 1
    assert arr[0]["table"] == "tips"

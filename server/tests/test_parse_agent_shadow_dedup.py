"""Step1 同 sheet 稀疏影子去重（月华邮件类 4 条→2 条）回归测试。

覆盖 ParseAgent._dedupe_same_sheet_shadows + _field_canon_map 中英桥：
- MailTemplate/GlobalMail 稀疏影子 vs canonical 版 → 去影子留 canonical
- 同 sheet 不同值（tips 两条）→ 不误杀
- 同 sheet 不同字段（BuildingInteract idle/collect）→ 不误杀
- 跨 sheet 同字段 → 绝不互删
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parse_agent import ParseAgent
from agent.excel.parser.nl_parser import NLIntent
from agent.excel.subagent.locator_agent import FKEdge


def _it(fields, action="add", stem="mail", sheet="MailTemplate"):
    return NLIntent(action=action, table_hint=stem, sheet_hint=sheet,
                    raw="x", extras={"fields": fields})


class _MailCli:
    """最小 cli：只供 mail 两张 sheet 的表头（row1 中文 + row2 英文）。"""

    def __init__(self):
        self._sheets = {
            "MailTemplate": (
                ["模板ID", "标题", "内容"],
                ["template_id:int", "title:string", "content:string"],
            ),
            "GlobalMail": (
                ["全服邮件ID", "邮件类型", "模板ID", "发送人"],
                ["global_id:int", "mail_type:int", "template_id:int", "sender:string"],
            ),
        }

    def list_tables(self):
        class _P:
            stem = "mail"
        return [_P()]

    def read_header(self, _path, sheet):
        return self._sheets[sheet][0]

    def read_type_row(self, _path, sheet):
        return self._sheets[sheet][1]


class TestDedupeSameSheetShadows:
    def test_mail_template_sparse_shadow_dropped(self):
        pa = ParseAgent(cli=_MailCli())
        sparse = _it({"模板ID": "<new_template_id>", "全服邮件ID": "<new_global_mail_id>"})
        canon = _it({"template_id": "<new_template_id>", "title": "月华庆典开启",
                     "content": "月华照耀九州", "全服邮件ID": "<new_global_mail_id>"})
        out = pa._dedupe_same_sheet_shadows([sparse, canon])
        assert len(out) == 1
        kept_fields = out[0].extras["fields"]
        assert "title" in kept_fields or "标题" in kept_fields

    def test_global_mail_sparse_shadow_dropped(self):
        pa = ParseAgent(cli=_MailCli())
        sparse = _it({"全服邮件ID": "<new_global_mail_id>", "模板ID": "<new_template_id>"},
                     sheet="GlobalMail")
        canon = _it({"全服邮件ID": 21, "邮件类型": 1, "发送人": "系统",
                     "模板ID": "<new_template_id>"}, sheet="GlobalMail")
        out = pa._dedupe_same_sheet_shadows([sparse, canon])
        assert len(out) == 1
        fields = out[0].extras["fields"]
        assert fields.get("全服邮件ID") == 21 or fields.get("global_id") == 21

    def test_four_to_two_mail_chain(self):
        pa = ParseAgent(cli=_MailCli())
        intents = [
            _it({"模板ID": "<new_template_id>", "全服邮件ID": "<new_global_mail_id>"}),
            _it({"全服邮件ID": "<new_global_mail_id>", "模板ID": "<new_template_id>"},
                sheet="GlobalMail"),
            _it({"template_id": "<new_template_id>", "title": "月华庆典开启",
                 "content": "月华照耀九州", "全服邮件ID": "<new_global_mail_id>"}),
            _it({"全服邮件ID": 21, "邮件类型": 1, "发送人": "系统",
                 "模板ID": "<new_template_id>"}, sheet="GlobalMail"),
        ]
        out = pa._dedupe_same_sheet_shadows(intents)
        assert len(out) == 2
        sheets = {o.sheet_hint for o in out}
        assert sheets == {"MailTemplate", "GlobalMail"}

    def test_same_keys_less_informative_shadow_dropped(self):
        pa = ParseAgent(cli=_MailCli())
        sparse = _it({
            "template_id": "<new_template_id>",
            "title": "月华庆典",
            "content": "",
        })
        sparse.produces_label = "new_template_id"
        canon = _it({
            "template_id": "<new_template_id>",
            "title": "月华庆典开启",
            "content": "月华照耀九州",
        })
        canon.produces_label = "new_template_id"

        out = pa._dedupe_same_sheet_shadows([sparse, canon])

        assert len(out) == 1
        assert out[0].extras["fields"]["content"] == "月华照耀九州"
        assert out[0].extras["fields"]["template_id"] == "<new_template_id>"

    def test_second_pass_drops_placeholder_shadow_after_backfill_shape(self):
        pa = ParseAgent(cli=_MailCli())
        sparse = _it({
            "global_id": "<new_global_mail_id>",
            "template_id": "<new_template_id>",
            "reward_id": "",
        }, sheet="GlobalMail")
        sparse.produces_label = "new_global_mail_id"
        canon = _it({
            "global_id": 21,
            "mail_type": 1,
            "sender": "系统",
            "send_time": "2026-10-01 00:00:00",
            "reward_id": 10001,
            "template_id": "<new_template_id>",
        }, sheet="GlobalMail")

        out = pa._dedupe_same_sheet_shadows([sparse, canon])

        assert len(out) == 1
        assert out[0].extras["fields"]["global_id"] == 21

    def test_clean_self_consumes_removes_fake_dependency(self):
        it = _it({"template_id": "<new_template_id>"})
        it.produces_label = "new_template_id"
        it.consumes_labels = ["new_template_id", "new_global_mail_id"]

        changed = ParseAgent._clean_self_consumes([it])

        assert changed == 1
        assert it.consumes_labels == ["new_global_mail_id"]

    def test_clean_dangling_consumes_keeps_only_referenced_labels(self):
        it = _it({"template_id": "<new_template_id>", "title": "T"})
        it.consumes_labels = ["new_template_id", "new_global_mail_id"]

        changed = ParseAgent._clean_dangling_consumes([it])

        assert changed == 1
        assert it.consumes_labels == ["new_template_id"]

    def test_expand_repeated_child_configs_by_fk_and_ordered_sequence(self):
        pa = ParseAgent(cli=_MailCli())
        parents = []
        for i in range(4):
            it = _it({"id": f"<new_parent_{i + 1}_id>", "name": f"P{i + 1}"},
                     stem="parent", sheet="Parent")
            it.produces_label = f"new_parent_{i + 1}_id"
            it.extras["produces"] = it.produces_label
            parents.append(it)
        child = _it({
            "parent_id": "<new_parent_id>",
            "level": 0,
            "common_spell_id": 901,
        }, stem="child", sheet="ChildLevel")
        child.consumes_labels = ["new_parent_1_id"]
        intents = parents + [child]
        edges = [FKEdge(
            "child", "ChildLevel", "parent_id",
            "parent", "Parent", "id")]

        changed = pa._expand_repeated_child_configs(
            intents, "技能编号按顺序 901、902、903、904", edges)

        child_rows = [it for it in intents if it.table_hint == "child"]
        spell_ids = [
            (it.extras["fields"]["parent_id"], it.extras["fields"]["common_spell_id"])
            for it in child_rows
        ]
        assert changed == 3
        assert spell_ids == [
            ("<new_parent_1_id>", 901),
            ("<new_parent_2_id>", 902),
            ("<new_parent_3_id>", 903),
            ("<new_parent_4_id>", 904),
        ]

    def test_expand_repeated_child_configs_by_same_workbook_pk(self):
        pa = ParseAgent()
        pa._primary_field_names = lambda stem, sheet: {"parent_id"} if str(sheet).lower() == "parent" else {"id"}
        parents = []
        for i in range(3):
            it = _it({"parent_id": f"<new_p{i + 1}_id>"},
                     stem="bundle", sheet="Parent")
            it.produces_label = f"new_p{i + 1}_id"
            it.extras["produces"] = it.produces_label
            parents.append(it)
        child = _it({"parent_id": "<new_bundle_id>", "score": 10},
                    stem="bundle", sheet="Child")
        intents = parents + [child]

        changed = pa._expand_repeated_child_configs(
            intents, "分数按顺序 10、20、30", [])

        assert changed == 2
        assert [it.extras["fields"]["score"] for it in intents if it.table_hint == "bundle"
                and it.sheet_hint == "Child"] == [10, 20, 30]

    def test_expand_rewrites_ordinal_fk_placeholder_to_parent_label(self):
        pa = ParseAgent()
        parent = _it({"id": "<new_ability3_id>"},
                     stem="parent", sheet="Parent")
        parent.produces_label = "new_ability3_id"
        parent.extras["produces"] = parent.produces_label
        child = _it({"parent_id": "<new_parent_id_1>"},
                    stem="child", sheet="Child")
        child.consumes_labels = ["new_parent_id_1"]
        intents = [
            _it({"id": "<new_ability1_id>"}, stem="parent", sheet="Parent"),
            _it({"id": "<new_ability2_id>"}, stem="parent", sheet="Parent"),
            parent,
            child,
        ]
        for i, it in enumerate(intents[:2], start=1):
            it.produces_label = f"new_ability{i}_id"
            it.extras["produces"] = it.produces_label
        edges = [FKEdge("child", "Child", "parent_id", "parent", "Parent", "id")]

        pa._expand_repeated_child_configs(intents, "", edges)

        assert child.extras["fields"]["parent_id"] == "<new_ability1_id>"
        assert child.consumes_labels == ["new_ability1_id"]

    def test_resolve_ordinal_placeholder_by_schema_pk_without_fk_edge(self):
        pa = ParseAgent()
        pa._primary_field_names = (
            lambda stem, sheet: {"parent_id"} if stem == "parent" else {"id"}
        )
        intents = []
        for i in range(2):
            parent = _it({"name": f"P{i + 1}"}, stem="parent", sheet="Parent")
            parent.produces_label = f"new_actual{i + 1}_id"
            parent.extras["produces"] = parent.produces_label
            intents.append(parent)
        child = _it({"parent_id": "<new_parent_id_2>"}, stem="child", sheet="Child")
        child.consumes_labels = ["new_parent_id_2"]
        intents.append(child)

        changed = pa._resolve_ordinal_placeholders(intents, [])

        assert changed == 1
        assert child.extras["fields"]["parent_id"] == "<new_actual2_id>"
        assert child.consumes_labels == ["new_actual2_id"]

    def test_align_placeholder_family_to_row_produces(self):
        it = _it({"id": "<new_parent_id>", "name": "P1"},
                 stem="parent", sheet="Parent")
        it.produces_label = "new_parent_1_id"
        it.extras["produces"] = it.produces_label
        it.consumes_labels = ["new_parent_id", "other_id"]

        changed = ParseAgent()._align_placeholder_families([it])

        assert changed == 2
        assert it.extras["fields"]["id"] == "<new_parent_1_id>"
        assert it.consumes_labels == ["other_id"]

    def test_resolve_placeholder_to_explicit_parent_pk(self):
        pa = ParseAgent()
        pa._primary_field_names = lambda stem, sheet: {"id"}
        parent = _it({"id": 9, "name": "P"}, stem="parent", sheet="Parent")
        child = _it({"parent_id": "<new_parent_id>", "value": 1},
                    stem="child", sheet="Child")
        child.consumes_labels = ["new_parent_id"]

        changed = pa._resolve_placeholders_to_explicit_pks([parent, child])

        assert changed == 2
        assert child.extras["fields"]["parent_id"] == 9
        assert child.consumes_labels == []

    def test_resolve_produced_placeholder_to_explicit_parent_pk(self):
        pa = ParseAgent()
        pa._primary_field_names = lambda stem, sheet: {"id"}
        parent = _it({"id": 9, "name": "P"}, stem="parent", sheet="Parent")
        parent.produces_label = "new_parent_id"
        parent.extras["produces"] = "new_parent_id"
        child = _it({"parent_id": "<new_parent_id>", "value": 1},
                    stem="child", sheet="Child")
        child.consumes_labels = ["new_parent_id"]

        changed = pa._resolve_placeholders_to_explicit_pks([parent, child])

        assert changed == 2
        assert child.extras["fields"]["parent_id"] == 9
        assert child.consumes_labels == []

    def test_fill_empty_fk_from_unique_explicit_parent_pk(self):
        pa = ParseAgent()
        pa._primary_field_names = lambda stem, sheet: {"id"}
        parent = _it({"id": 9, "name": "P"}, stem="parent", sheet="Parent")
        child = _it({"parent_id": "", "value": 1}, stem="child", sheet="Child")
        edges = [FKEdge("child", "Child", "parent_id", "parent", "Parent", "id")]

        changed = pa._fill_empty_fks_from_explicit_pks([parent, child], edges)

        assert changed == 1
        assert child.extras["fields"]["parent_id"] == 9

    def test_distinct_batch_rows_kept(self):
        pa = ParseAgent(cli=_MailCli())
        t1 = _it({"value": "背包已满", "key": "BAG_FULL", "type": "tips"},
                 stem="tips", sheet="tips")
        t2 = _it({"value": "金币不足", "key": "GOLD_LACK", "type": "tips"},
                 stem="tips", sheet="tips")
        out = pa._dedupe_same_sheet_shadows([t1, t2])
        assert len(out) == 2

    def test_building_interact_states_kept(self):
        pa = ParseAgent(cli=_MailCli())
        b1 = _it({"state": "idle", "效果": "待机"}, stem="building",
                 sheet="BuildingInteract")
        b2 = _it({"state": "collect", "效果": "采集"}, stem="building",
                 sheet="BuildingInteract")
        out = pa._dedupe_same_sheet_shadows([b1, b2])
        assert len(out) == 2

    def test_cross_sheet_never_dedup(self):
        pa = ParseAgent(cli=_MailCli())
        x1 = _it({"模板ID": "<p>"}, sheet="MailTemplate")
        x2 = _it({"模板ID": "<p>"}, sheet="GlobalMail")
        out = pa._dedupe_same_sheet_shadows([x1, x2])
        assert len(out) == 2

    def test_single_intent_kept(self):
        pa = ParseAgent(cli=_MailCli())
        out = pa._dedupe_same_sheet_shadows([_it({"title": "A"})])
        assert len(out) == 1

    def test_empty_intents_noop(self):
        pa = ParseAgent(cli=_MailCli())
        assert pa._dedupe_same_sheet_shadows([]) == []


class TestFieldCanonMap:
    def test_cn_en_bridge(self):
        pa = ParseAgent(cli=_MailCli())
        cm = pa._field_canon_map("mail", "MailTemplate")
        n_cn = ParseAgent._field_name_norm("模板ID")
        n_en = ParseAgent._field_name_norm("template_id")
        assert cm.get(n_cn) == cm.get(n_en) == "templateid"

    def test_no_cli_returns_empty(self):
        pa = ParseAgent(cli=None)
        assert pa._field_canon_map("mail", "MailTemplate") == {}

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
            stem = "mail"
        return [P()]

    def read_header(self, _path, sheet):
        if sheet == "GlobalMail":
            return ["全服邮件ID", "模板ID", "奖励"]
        if sheet == "MailTemplate":
            return ["模板ID", "标题", "内容"]
        return []

    def read_type_row(self, _path, sheet):
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


def test_parse_baseline_drops_sparse_add_shadow():
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

    assert len(out) == 1
    assert out[0].extras["fields"]["global_id"] == 21


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

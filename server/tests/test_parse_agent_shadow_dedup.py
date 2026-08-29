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
        assert "title" in out[0].extras["fields"]

    def test_global_mail_sparse_shadow_dropped(self):
        pa = ParseAgent(cli=_MailCli())
        sparse = _it({"全服邮件ID": "<new_global_mail_id>", "模板ID": "<new_template_id>"},
                     sheet="GlobalMail")
        canon = _it({"全服邮件ID": 21, "邮件类型": 1, "发送人": "系统",
                     "模板ID": "<new_template_id>"}, sheet="GlobalMail")
        out = pa._dedupe_same_sheet_shadows([sparse, canon])
        assert len(out) == 1
        assert out[0].extras["fields"]["全服邮件ID"] == 21

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

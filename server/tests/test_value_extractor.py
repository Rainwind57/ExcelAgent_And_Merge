"""Phase 2-a 确定性字段值抽取（value_extractor）单测。全部 0 LLM。"""
from __future__ import annotations

from agent.excel.core.pipeline.value_extractor import extract_fields_from_text


def test_header_anchored_chinese_colon():
    r = extract_fields_from_text(
        "名称：饕餮，等级：99，描述：上古凶兽",
        headers=["名称", "等级", "描述"],
    )
    assert r["fields"] == {"名称": "饕餮", "等级": "99", "描述": "上古凶兽"}
    assert r["unresolved"] == []


def test_header_anchored_english_and_equals():
    r = extract_fields_from_text(
        "quest_id=1001; title = 拯救村庄",
        headers=["quest_id", "title"],
    )
    assert r["fields"]["quest_id"] == "1001"
    assert r["fields"]["title"] == "拯救村庄"


def test_wei_shi_separators():
    r = extract_fields_from_text(
        "类型为活动，名称是春节庆典",
        headers=["类型", "名称"],
    )
    assert r["fields"]["类型"] == "活动"
    assert r["fields"]["名称"] == "春节庆典"


def test_unresolved_fields_reported():
    r = extract_fields_from_text(
        "名称：青龙",
        headers=["名称", "攻击力", "防御力"],
    )
    assert r["fields"] == {"名称": "青龙"}
    assert set(r["unresolved"]) == {"攻击力", "防御力"}


def test_existing_fields_not_overwritten():
    r = extract_fields_from_text(
        "名称：白虎",
        headers=["名称", "等级"],
        existing={"名称": "已存在"},
    )
    assert "名称" not in r["fields"]           # 不覆盖已有
    assert r["unresolved"] == ["等级"]


def test_longer_header_wins_over_substring():
    r = extract_fields_from_text(
        "灵兽名称：麒麟",
        headers=["名称", "灵兽名称"],
    )
    # 长表头优先，避免"名称"先吃掉子串
    assert r["fields"].get("灵兽名称") == "麒麟"
    assert "名称" not in r["fields"]


def test_residual_quoted_and_numbers():
    r = extract_fields_from_text(
        "新增一条记录「玄武」，掉落 5006",
        headers=["名称"],
    )
    # 无 header 锚定 → 名称未解，但残余候选被收集供 LLM
    assert "名称" in r["unresolved"]
    assert "玄武" in r["residual_quoted"]
    assert "5006" in r["residual_numbers"]


def test_consumed_value_not_double_counted_as_residual():
    r = extract_fields_from_text(
        "编号：1001，名称：「朱雀」",
        headers=["编号", "名称"],
    )
    assert r["fields"]["编号"] == "1001"
    assert r["fields"]["名称"] == "朱雀"       # 引号被清理
    # 已消费的值不应再出现在残余候选里
    assert "1001" not in r["residual_numbers"]
    assert "朱雀" not in r["residual_quoted"]


def test_empty_inputs():
    r = extract_fields_from_text("", headers=["a"])
    assert r["fields"] == {}
    assert r["unresolved"] == ["a"]
    r2 = extract_fields_from_text("anything", headers=[])
    assert r2["fields"] == {}
    assert r2["unresolved"] == []

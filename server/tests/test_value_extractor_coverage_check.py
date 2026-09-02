"""has_uncovered_literal_values 纯函数确定性单测（0 LLM）。

用于 _llm_complete_fields 调用前的低成本预判：原文字面值（引号名/裸数字）是否
已被现有字段值覆盖。全部覆盖 → 大概率无需调 LLM；有未覆盖 → 值得一查。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.pipeline.value_extractor import has_uncovered_literal_values


def test_all_literal_values_covered_returns_false():
    text = "新增一件装备，名称：屠龙刀，品质：5，图标：Icon_daolong"
    values = ["屠龙刀", "5", "Icon_daolong"]
    assert has_uncovered_literal_values(text, values) is False


def test_uncovered_quoted_value_returns_true():
    text = "新增NPC，名字叫'老张'，对话内容是'欢迎光临'"
    values = ["老张"]  # "欢迎光临" 未被任何字段覆盖
    assert has_uncovered_literal_values(text, values) is True


def test_uncovered_number_returns_true():
    # _NUMBER_RE 只认 2+ 位数字候选（单位数如"5"不算候选，避免噪声）
    text = "新增奖励包，reward_id 10066，数量 88"
    values = ["10066"]  # "88" 未被覆盖
    assert has_uncovered_literal_values(text, values) is True


def test_no_field_values_but_text_has_literals_returns_true():
    text = "新增奖励包 reward_id 10066"
    assert has_uncovered_literal_values(text, []) is True


def test_no_field_values_and_no_literals_returns_false():
    text = "把这个删掉"
    assert has_uncovered_literal_values(text, []) is False


def test_empty_text_returns_false():
    assert has_uncovered_literal_values("", ["10066"]) is False


def test_value_substring_match_counts_as_covered():
    # "10066" 出现在字段值 "reward_10066" 里也算覆盖（保守偏宽松，不误报"值得查"）
    text = "新增奖励包 10066"
    values = ["reward_10066"]
    assert has_uncovered_literal_values(text, values) is False

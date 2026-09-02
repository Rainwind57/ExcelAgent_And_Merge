"""TableCard 轻量表卡单测（纯函数，0 LLM，确定性）。"""
from agent.excel.core.pipeline.table_card import build_table_card, render_card_text


def test_build_card_basic_pk_and_fk():
    card = build_table_card(
        "interaction", "InteractionConv",
        headers=["conv_id", "prompt_text", "option_1", "reward_id"],
        type_row=["conv_id:int", "prompt_text:string", "option_1:int", "reward_id:int"],
        fk_map={"reward_id": "reward.Reward.reward_id"},
        aliases=["对话", "对话节点"],
        purpose="对话节点",
    )
    assert card["stem"] == "interaction"
    assert card["primary_key"] == ["conv_id"]
    # reward_id 来自 fk_map；option_1 由 _id 形态启发也算 FK
    fk_cols = {f["column"] for f in card["fk_columns"]}
    assert "reward_id" in fk_cols
    assert card["fk_columns"][[f["column"] for f in card["fk_columns"]].index("reward_id")]["target"] \
        == "reward.Reward.reward_id"
    assert "对话" in card["aliases"]
    assert "conv_id" in card["required_columns"]


def test_pk_fallback_to_first_id_column_when_no_pk_cols():
    card = build_table_card(
        "reward", "Reward",
        headers=["名称", "reward_id", "数量"],
        type_row=["string", "int", "int"],
    )
    # 无 pk_cols → 取首个含 id 的列
    assert card["primary_key"] == ["reward_id"]


def test_pk_fallback_to_first_col_when_no_id():
    card = build_table_card("t", "S", headers=["名称", "描述"], type_row=["string", "string"])
    assert card["primary_key"] == ["名称"]


def test_explicit_pk_cols_win():
    card = build_table_card(
        "fabao", "FabaoLevel",
        headers=["法宝id", "法宝等级", "攻击力"],
        pk_cols=["法宝id", "法宝等级"],
    )
    assert card["primary_key"] == ["法宝id", "法宝等级"]


def test_card_text_is_compact_and_contains_key_fields():
    card = build_table_card(
        "interaction", "InteractionConv",
        headers=["conv_id", "reward_id"],
        fk_map={"reward_id": "reward.Reward.reward_id"},
        aliases=["对话"], purpose="对话节点",
        hit_columns=["conv_id"],
    )
    txt = render_card_text(card)
    assert "interaction/InteractionConv" in txt
    assert "对话节点" in txt
    assert "conv_id" in txt
    assert "reward_id→reward.Reward.reward_id" in txt
    # 紧凑：6 行以内
    assert len(txt.splitlines()) <= 6


def test_headers_with_type_suffix_are_cleaned():
    card = build_table_card("t", "S", headers=["conv_id:int", "text:string"])
    assert card["columns"][0]["name"] == "conv_id"
    assert card["columns"][0]["type"] == "int"

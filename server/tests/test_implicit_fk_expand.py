"""缺口4 隐式 FK 扩展单测（case2 school_spirit.spirit_id→pet 未声明）。

不依赖 serve LLM。mock cli + 反向列名索引，验证 _expand_by_implicit_fk
能把含 _id 列的表的对端表（pet）补进 adj 邻接表。
"""
from pathlib import Path
from unittest.mock import MagicMock

from agent.excel.subagent.locator_agent import LocatorAgent, CandidateTable


def test_implicit_fk_expands_adj(monkeypatch):
    """候选 school_spirit（含 spirit_id 列）→ 反查含 灵根id 的 pet 补进 adj。"""
    # mock cli：school_spirit 表 sheet1 header 含 spirit_id 列
    cli = MagicMock()
    sw_path = Path("/fake/school_spirit.xlsx")
    pet_path = Path("/fake/pet.xlsx")
    cli.list_tables.return_value = [sw_path, pet_path]
    cli.get_sheets.return_value = ["Spirit"]
    # school_spirit header: 含 spirit_id（row2 规范名括号内）
    cli.read_header.return_value = ["灵根id（spirit_id:int）", "名称（name:str）"]

    # mock 反向列名索引：灵根id → pet，spirit_id → pet
    import agent.excel.subagent.locator_agent as loc_mod
    fake_rev = {
        "灵根id": [("pet", "Pet")],
        "spirit_id": [("pet", "Pet")],
    }
    monkeypatch.setattr(loc_mod, "get_column_reverse_index",
                        lambda: fake_rev, raising=False)
    # 真实 import 在函数内，patch 模块级 table_index
    import agent.excel.locator.table_index as ti_mod
    monkeypatch.setattr(ti_mod, "get_column_reverse_index",
                        lambda: fake_rev)

    loc = LocatorAgent(cli=cli)
    candidates = [CandidateTable(stem="school_spirit", confidence=0.9, level="rule")]
    adj: dict[str, set[str]] = {"school_spirit": set()}
    loc._expand_by_implicit_fk(candidates, adj)

    assert "pet" in adj.get("school_spirit", set()), \
        f"隐式 FK 应把 pet 补进 school_spirit 邻接, 实际 adj={adj}"
    assert "school_spirit" in adj.get("pet", set()), \
        f"双向邻接, 实际 adj={adj}"
    print(f"PASS implicit_fk: adj={adj}")


def test_infer_fk_edges_keeps_same_workbook_cross_sheet_refs():
    cli = MagicMock()
    path = Path("/fake/fabao.xlsx")
    cli.list_tables.return_value = [path]
    cli.get_sheets.return_value = ["Fabao", "FabaoLevel"]
    cli.read_header.side_effect = lambda _p, sh: {
        "Fabao": ["法宝id", "名称"],
        "FabaoLevel": ["法宝id", "法宝等级"],
    }[sh]
    cli.read_type_row.side_effect = lambda _p, sh: {
        "Fabao": ["id:int", "name:string"],
        "FabaoLevel": ["fabao_id:int", "level:int"],
    }[sh]

    loc = LocatorAgent(cli=cli)
    edges = loc._infer_fk_edges([CandidateTable(stem="fabao", confidence=1.0, level="rule")])

    assert any(
        e.from_stem == "fabao"
        and e.from_sheet == "FabaoLevel"
        and e.from_column == "fabao_id"
        and e.to_stem == "fabao"
        and e.to_sheet == "Fabao"
        and e.to_column == "id"
        for e in edges
    ), edges


def test_infer_fk_edges_matches_same_workbook_pk_column_family():
    cli = MagicMock()
    path = Path("/fake/interaction.xlsx")
    cli.list_tables.return_value = [path]
    cli.get_sheets.return_value = ["InteractionConv", "InteractionConvOption"]
    cli.read_header.side_effect = lambda _p, sh: {
        "InteractionConv": ["编号", "对话内容"],
        "InteractionConvOption": ["编号", "选项内容", "1:新对话ID"],
    }[sh]
    cli.read_type_row.side_effect = lambda _p, sh: {
        "InteractionConv": ["conv_id:int", "prompt_text:string"],
        "InteractionConvOption": [
            "option_id:int",
            "option_text:string",
            "option_function.data.1.conv_id:int",
        ],
    }[sh]

    loc = LocatorAgent(cli=cli)
    edges = loc._infer_fk_edges([CandidateTable(stem="interaction", confidence=1.0, level="rule")])

    assert any(
        e.from_stem == "interaction"
        and e.from_sheet == "InteractionConvOption"
        and e.from_column == "option_function.data.1.conv_id"
        and e.to_stem == "interaction"
        and e.to_sheet == "InteractionConv"
        and e.to_column == "conv_id"
        for e in edges
    ), edges


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

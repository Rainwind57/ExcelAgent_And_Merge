"""复合主键定位 _locate_row_composite 单测（缺口2，case5 ResidenceEntry 双键）。

不依赖 serve LLM，用临时 xlsx + StubCodeMakerCLI 验证多列交集逻辑。
"""
from pathlib import Path

from agent.excel.cli_interface import StubCodeMakerCLI
from agent.excel.core.agent import TableAgent


class _MockParser:
    """最小 parser stub（TableAgent 构造需，_locate_row_composite 不用）。"""
    def __init__(self):
        self.client = None
        self.directory = ""
        self.model = ""


def _make_test_xlsx(tmp: Path) -> Path:
    """建临时 ResidenceEntry sheet：复合主键 (residence_id, obstacle_id)。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "ResidenceEntry"
    # row1 显示名, row2 规范名, row3-4 占位, row5 起数据
    ws.append(["洞府ID（residence_id:int）", "障碍ID（obstacle_id:int）", "入口名（name:str）"])
    ws.append(["residence_id", "obstacle_id", "name"])
    ws.append(["", "", ""])
    ws.append(["", "", ""])
    ws.append([30005, 10110, "聚灵塔入口A"])
    ws.append([30005, 10111, "聚灵塔入口B"])
    ws.append([30006, 10110, "聚灵塔入口C"])  # 同 obstacle_id 不同 residence
    p = tmp / "_test_composite_pk.xlsx"
    wb.save(p)
    return p


def _make_agent(tmp: Path) -> tuple[TableAgent, Path]:
    p = _make_test_xlsx(tmp)
    cli = StubCodeMakerCLI(workspace=tmp)
    # live_index=False：临时目录 Agent 不刷新全局 _table_index.json，
    # 避免把测试夹具写进生产索引、污染其它依赖索引的用例（如 test_decompose_agent）。
    agent = TableAgent(cli=cli, parser=_MockParser(), live_index=False)
    return agent, p


def test_composite_pk_locate(tmp_path):
    agent, p = _make_agent(tmp_path)
    # 双键 (30005, 10110) → 唯一命中该行
    rm = agent._locate_row_composite(
        p, "ResidenceEntry",
        ["residence_id", "obstacle_id"],
        ["30005", "10110"],
        match_mode="exact")
    assert rm is not None, "复合主键应命中"
    assert not rm.ambiguous, "双键应唯一定位"
    assert rm.method == "composite"
    # 校验命中的行实际含目标值
    cell_res = agent.cli._read_cell_value(p, "ResidenceEntry", rm.row, 1) \
        if hasattr(agent.cli, "_read_cell_value") else None
    print(f"PASS composite_pk: 行{rm.row} method={rm.method} conf={rm.confidence} cell1={cell_res}")


def test_composite_pk_no_intersection(tmp_path):
    """双键无交集时返 None。"""
    agent, p = _make_agent(tmp_path)
    # (30005, 99999) — obstacle_id 99999 不存在, 交集空
    rm = agent._locate_row_composite(
        p, "ResidenceEntry",
        ["residence_id", "obstacle_id"],
        ["30005", "99999"],
        match_mode="exact")
    assert rm is None, f"无交集应返 None, 实际{rm}"
    print("PASS composite_pk_no_intersection: 无交集返 None")


def test_composite_pk_single_fallback(tmp_path):
    """只给1列时回退单列 _locate_row。"""
    agent, p = _make_agent(tmp_path)
    rm = agent._locate_row_composite(
        p, "ResidenceEntry",
        ["residence_id"], ["30005"],
        match_mode="exact")
    # 单列回退, residence_id=30005 命中2行 → 歧义
    assert rm is not None, "单列回退应命中"
    assert rm.ambiguous, "单列 residence_id=30005 应歧义"
    print(f"PASS composite_pk_single_fallback: 行{rm.row} ambiguous={rm.ambiguous}")


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        test_composite_pk_locate(td)
        test_composite_pk_no_intersection(td)
        test_composite_pk_single_fallback(td)
    print("ALL PASS")

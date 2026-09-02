"""现象3：value_extractor 接入 _splitter_baseline 主路径的确定性回归（0 LLM）。

弱 baseline 只抓"列名紧邻数字"，漏 str 列的"列名：值"。接入 value_extractor 后，
header 锚定的确定性字段（名称/品质/图标等）应被补齐。默认开，可用
CODEMAKER_BASELINE_VALUE_EXTRACTOR=0 回退到旧弱 baseline。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.locator_agent import CandidateTable


class _EquipCli:
    class _P:
        def __init__(self, stem):
            self.stem = stem

    def __init__(self):
        self._paths = [self._P("equip")]
        self._hdrs = ["装备id", "名称", "品质", "图标"]
        self._trow = ["装备id:int", "名称:string", "品质:int", "图标:string"]

    def list_tables(self):
        return self._paths

    def get_sheets(self, path):
        return ["Equip"]

    def read_header(self, path, sheet):
        return list(self._hdrs)

    def read_type_row(self, path, sheet):
        return list(self._trow)


def _run(text, flag):
    if flag is None:
        os.environ.pop("CODEMAKER_BASELINE_VALUE_EXTRACTOR", None)
    else:
        os.environ["CODEMAKER_BASELINE_VALUE_EXTRACTOR"] = flag
    cand = [CandidateTable(stem="equip", sheet="Equip", confidence=0.95,
                           level="alias", matched_term="装备")]
    da = DecomposeAgent(parser=object(), cli=_EquipCli())
    return da._splitter_baseline(text, cand, [])


def test_value_extractor_fills_header_anchored_str_fields():
    os.environ.pop("CODEMAKER_BASELINE_VALUE_EXTRACTOR", None)
    try:
        text = "新增一件装备，名称：屠龙刀，品质：5，图标：Icon_daolong"
        intents = _run(text, None)
        equip = next(it for it in intents if it.table_hint == "equip")
        assert equip.fields.get("名称") == "屠龙刀"
        assert equip.fields.get("图标") == "Icon_daolong"
        # 数字列也应被 header 锚定抽出（弱 baseline 需 column_signal 才能命中）
        assert str(equip.fields.get("品质")) == "5"
    finally:
        os.environ.pop("CODEMAKER_BASELINE_VALUE_EXTRACTOR", None)


def test_value_extractor_can_be_disabled_for_rollback():
    os.environ["CODEMAKER_BASELINE_VALUE_EXTRACTOR"] = "0"
    try:
        text = "新增一件装备，名称：屠龙刀，品质：5，图标：Icon_daolong"
        intents = _run(text, "0")
        # 关开关后无 column_signal → 弱 baseline 抓不到"列名：值"，equip 无该类字段
        equips = [it for it in intents if it.table_hint == "equip"]
        for it in equips:
            assert it.fields.get("名称") is None
            assert it.fields.get("图标") is None
    finally:
        os.environ.pop("CODEMAKER_BASELINE_VALUE_EXTRACTOR", None)

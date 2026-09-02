"""提速：_build_schema_block 接入 schema_budget（超预算时 dependency 转摘要）。

验证主路径 prompt 裁剪的接线：required 表保留完整列、dependency 表压成摘要（PK+
命中+id 列）、超预算才触发。小 prompt（未超预算）保持完整不变。0 LLM、确定性。
"""
import os
import sys
import types

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.locator_agent import CandidateTable


class _WideCli:
    """两表，每表一 sheet 多列（用于撑过 char budget）。"""
    class _P:
        def __init__(self, stem):
            self.stem = stem

    def __init__(self):
        self._paths = [self._P("aaa"), self._P("bbb")]
        cols = ["aaa_id", "名称"] + [f"属性列_{i:02d}" for i in range(14)]
        cols_b = ["bbb_id", "名称"] + [f"字段列_{i:02d}" for i in range(14)]
        self._hdr = {"aaa": cols, "bbb": cols_b}

    def list_tables(self):
        return self._paths

    def get_sheets(self, path):
        return ["S"]

    def read_header(self, path, sheet):
        return list(self._hdr[path.stem])

    def read_type_row(self, path, sheet):
        return ["int"] + ["string"] * (len(self._hdr[path.stem]) - 1)


def _cands():
    return [
        CandidateTable(stem="aaa", sheet="S", confidence=1.0, level="alias"),
        # fk_expanded → dependency 层（超预算时压摘要）
        CandidateTable(stem="bbb", sheet="S", confidence=0.6, level="fk_expanded"),
    ]


def _build(env_budget, cols_env="20"):
    if env_budget is None:
        os.environ.pop("CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET", None)
    else:
        os.environ["CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET"] = env_budget
    os.environ["CODEMAKER_DECOMPOSE_SCHEMA_COLS"] = cols_env
    os.environ["CODEMAKER_DECOMPOSE_SCHEMA_SHEETS"] = "4"
    try:
        da = DecomposeAgent(parser=object(), cli=_WideCli())
        return da._build_schema_block(_cands(), text="")
    finally:
        os.environ.pop("CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET", None)
        os.environ.pop("CODEMAKER_DECOMPOSE_SCHEMA_COLS", None)
        os.environ.pop("CODEMAKER_DECOMPOSE_SCHEMA_SHEETS", None)


def test_over_budget_summarizes_dependency_table():
    block = _build(env_budget="200")  # 小预算强制触发
    lines = {l.split(":")[0]: l for l in block.splitlines()}
    aaa = next(l for k, l in lines.items() if "aaa/S" in k)
    bbb = next(l for k, l in lines.items() if "bbb/S" in k)
    # required(aaa) 完整列多；dependency(bbb) 摘要列少
    assert aaa.count("|") > bbb.count("|")
    assert "bbb_id" in bbb  # 摘要保留主键


def test_disabled_budget_keeps_full_schema():
    block = _build(env_budget="0")  # 显式关闭
    lines = block.splitlines()
    aaa = next(l for l in lines if "aaa/S" in l)
    bbb = next(l for l in lines if "bbb/S" in l)
    # 关闭时两表都完整（列数相当）
    assert aaa.count("|") == bbb.count("|")
    assert aaa.count("|") >= 10


def test_small_prompt_under_budget_unchanged():
    # 默认 6000 预算下，小 schema（本 stub ~几百字符）不触发裁剪 → 与关闭时一致
    default_block = _build(env_budget=None)
    off_block = _build(env_budget="0")
    assert default_block == off_block

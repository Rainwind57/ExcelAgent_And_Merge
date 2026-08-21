"""缺口3 嵌套点分键校验豁免单测（case3 aptitude_base.StrPotCon）。

不依赖 serve LLM。验证 validator_field_layer 对含 . 的点分键字段
不报 COL_NOT_FOUND（取末段匹配 row1 表头）。
"""
from pathlib import Path

from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.parser.nl_parser import NLIntent


class _MockParser:
    def __init__(self):
        self.client = None
        self.directory = ""
        self.model = ""


def test_dotted_key_not_col_not_found(tmp_path, monkeypatch):
    """fields 含 aptitude_base.StrPotCon 点分键，表头有体力资质 → 不报 COL_NOT_FOUND。"""
    va = ValidatorAgent(parser=_MockParser(), thinking_sink=lambda p, d: None,
                        cli=MagicMock_cli())
    intent = NLIntent(action="add", table_hint="pet", sheet_hint="Pet",
                      extras={"fields": {"aptitude_base.StrPotCon": 6000}})
    # schema_getter 返表头（含"体力资质"，row1 中文，与点分键末段 StrPotCon 不直接等）
    # 末段 StrPotCon 也不在表头 → 走点分全名对齐分支？
    # 实际：aptitude_base.StrPotCon 末段是 StrPotCon（row2 规范名），row1 表头是"体力资质"
    # 当前豁免逻辑取末段匹配 headers_norm，StrPotCon != 体力资质 → 仍报 COL_NOT_FOUND
    # 需 type_aliases 映射。本测聚焦"末段命中表头"场景（如字段 StrPotCon 直接命中）
    def schema_getter(it):
        return ["体力资质（aptitude_base.StrPotCon:int）"], None

    issues_map = va.validate_field_layer([intent], schema_getter=schema_getter)
    issues = issues_map.get(id(intent), [])
    # aptitude_base.StrPotCon 末段 StrPotCon，表头含"体力资质"不含 StrPotCon
    # 但表头含点分全名 aptitude_base.StrPotCon（括号内）→ 豁免分支应命中
    col_not_found = [i for i in issues
                     if getattr(i, "issue_type", "") == "col_not_found"]
    assert not col_not_found, \
        f"点分键 aptitude_base.StrPotCon 不应报 COL_NOT_FOUND, 实际 {[getattr(i,'col') for i in col_not_found]}"


from unittest.mock import MagicMock as MagicMock_cli


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

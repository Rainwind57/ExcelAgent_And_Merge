"""建议5 结构化严重度判定单测（纯函数，0 LLM，确定性）。"""
from agent.excel.core.pipeline.issue_severity import (
    classify_severity, is_hard, HARD_ISSUE_TYPES,
)


def test_hard_types_are_hard():
    for t in ("unique_violation", "type_mismatch", "col_not_found"):
        assert classify_severity(issue_type=t) == "hard"
        assert is_hard(error_type=t)


def test_missing_required_soft_unless_pk():
    assert classify_severity(issue_type="missing_required") == "soft"
    assert classify_severity(issue_type="missing_required", is_pk_missing=True) == "hard"


def test_explicit_severity_wins():
    # 显式 severity 覆盖类型判定
    assert classify_severity(issue_type="type_mismatch", severity="soft") == "soft"
    assert classify_severity(issue_type="missing_required", severity="hard") == "hard"


def test_unknown_type_is_soft():
    assert classify_severity(issue_type="some_soft_tip") == "soft"
    assert classify_severity(error_type="", issue_type="") == "soft"


def test_no_root_cause_string_parsing():
    # 不解析 root_cause 文案：即便"业务必填列"这类词只出现在别处，也不影响判定
    assert classify_severity(error_type="validate_issue",
                             issue_type="missing_required") == "soft"


def test_case_insensitive_and_whitespace():
    assert classify_severity(issue_type="  TYPE_MISMATCH  ") == "hard"


def test_hard_issue_types_constant():
    assert HARD_ISSUE_TYPES == {"unique_violation", "type_mismatch", "col_not_found"}

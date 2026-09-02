"""建议5：Step2 结构化校验严重度判定（纯函数，0 LLM）。

背景（docs §P2 "不要靠 root_cause 字符串判 hard/soft；按 IssueType + severity"）：
现状 _is_hard_validation_issue 用 root_cause 子串（如 "业务必填列"/"指令明确"）判
hard——脆弱、随文案漂移。本模块改为**结构化判定**：

  1. 显式 severity（issue 自带 "hard"/"soft"）优先——来源方（validator）已判定。
  2. 否则按 IssueType/error_type 命中硬集合（unique_violation / type_mismatch /
     col_not_found）判 hard；missing_required 仅在"主键缺失"时 hard。
  3. 绝不解析 root_cause 自然语言串。

纯函数、确定性。配合"单 intent 跳过"（validator 标 intent.validation.skipped，
Step3 跳过该条不写、其余照跑），避免一条坏任务卡死整批。
"""
from __future__ import annotations

from typing import Optional

__all__ = ["HARD_ISSUE_TYPES", "classify_severity", "is_hard"]

# 结构化硬类型（对齐 validator_agent IssueType 的 value）
HARD_ISSUE_TYPES = {
    "unique_violation",
    "type_mismatch",
    "col_not_found",
}
_MISSING = "missing_required"


def _norm(v: object) -> str:
    return str(v if v is not None else "").strip().lower()


def classify_severity(
    error_type: object = "",
    issue_type: object = "",
    *,
    severity: Optional[str] = None,
    is_pk_missing: bool = False,
) -> str:
    """返回 "hard" | "soft"。纯结构化判定，不看 root_cause 文案。

    Args:
        error_type / issue_type: 结构化类型串（IssueType.value 或 error_type）。
        severity: 来源方显式给的严重度（"hard"/"soft"）——最高优先。
        is_pk_missing: missing_required 是否为主键缺失（仅此情况 hard）。
    """
    sev = _norm(severity)
    if sev in ("hard", "soft"):
        return sev
    vals = {_norm(error_type), _norm(issue_type)}
    if vals & HARD_ISSUE_TYPES:
        return "hard"
    if _MISSING in vals and is_pk_missing:
        return "hard"
    return "soft"


def is_hard(error_type: object = "", issue_type: object = "", *,
            severity: Optional[str] = None, is_pk_missing: bool = False) -> bool:
    return classify_severity(error_type, issue_type, severity=severity,
                             is_pk_missing=is_pk_missing) == "hard"

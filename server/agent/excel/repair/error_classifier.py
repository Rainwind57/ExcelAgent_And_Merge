"""错误类型分类器：把写操作失败信号归类为 ErrorType，供 repair playbook 定向路由。

设计动机：
    原 `_collect_error_feedback`（agent.py）用正则泛化提取失败列/值后堆上下文喂 LLM 重试，
    无错误类型概念，所有错误走同一修复路径。本模块把分类逻辑抽离为规则化、可测、零 LLM
    的分类器，分类信号来自三源：
      1. 异常对象（agent 写操作多数不抛类型化异常，主要为兜底）
      2. AgentResult.steps[].detail / message / needs_confirm / needs_user_fill
         （错误经 res.add(name, False, detail) 数据流传递，detail 含 "列[X]...值'Y'"、
           "类型为 int，值'Z'无法转为整数" 等模式）
      3. verify 门控输出 VerifyResult（ref_integrity dangling / id_scope / type / anti_pattern）

分类优先级（高置信度信号优先）：
    verify 门控信号 > step.detail 类型模式 > message 文本模式 > 兜底 unknown

分类结果 ClassifiedError 携带 error_type + 置信度 + 失败列/值/步骤 + verify 信号，
供 RepairPlaybook 选策略、供 Level 2 LLM 诊断消费、供 skill_updater 学习。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class ErrorType(str, Enum):
    """写操作失败类型。str 枚举便于序列化与 anti_pattern 信号记录。"""

    COLUMN_NOT_FOUND = "column_not_found"
    ROW_NOT_FOUND = "row_not_found"
    TYPE_MISMATCH = "type_mismatch"
    ID_CONFLICT = "id_conflict"
    PK_MISPLACED = "pk_misplaced"
    CROSS_REF_BROKEN = "cross_ref_broken"
    FORMULA_ERROR = "formula_error"
    SEMANTIC_OUTLIER = "semantic_outlier"
    UNKNOWN = "unknown"


# 失败 step.detail / message 的正则模式（与 agent._collect_error_feedback 的提取模式对齐）
_COL_RE = re.compile(r"列\[([^\]]+)\]")
_VAL_QUOTED_RE = re.compile(r"值'([^']+)'")
_VAL_UNQUOTED_RE = re.compile(r"值\s*([^\s，,]+)")
_TYPE_MISMATCH_RE = re.compile(r"类型为\s*(\w+)\s*[，,]?.*?(?:无法转为|不能转|不是)\s*(\w+)?", re.IGNORECASE)
_COL_NOT_FOUND_RE = re.compile(r"(?:列\s*[【\[]?([^\]】]+?)[】\]]?\s*(?:不存在|未找到|找不到|未匹配|不在表头))|(?:未找到列\s*[【\[]?([^\]】]+?)[】\]]?)|(?:无法匹配目标列[【\[]?([^\]】]+?)[】\]]?)", re.IGNORECASE)
_ROW_NOT_FOUND_RE = re.compile(r"(?:行|记录|数据)\s*(?:未找到|找不到|零命中|未命中|不存在)", re.IGNORECASE)
_FORMULA_RE = re.compile(r"(?:公式|FORMULA|#REF!|#NAME\?|引用断裂|引用错)", re.IGNORECASE)
_ID_CONFLICT_RE = re.compile(r"(?:主键|PK|ID).{0,20}?(?:重复|冲突|已存在|越界|占用|被占|占号|已占)", re.IGNORECASE)
# 合并单元格/只读结构错误：openpyxl 写 MergedCell 报 attribute read-only；
# ReadOnlyWorksheet/Workbook 等只读模式写操作也会报类似 attribute 错。
# 这类失败说明目标 sheet 结构不宜写（列举 sheet/合并表头/只读），需人工判断 → SEMANTIC_OUTLIER。
_MERGED_CELL_RE = re.compile(r"MergedCell|ReadOnlyWorksheet|ReadOnlyWorkbook|attribute.*read-only|read-only", re.IGNORECASE)


@dataclass
class VerifyResult:
    """verify 门控输出（agent._verify_write 产出）。零 LLM、纯规则内存校验。

    failed_kind 为 None 表示校验通过；否则为优先级最高的失败类别，与 ErrorType 对齐。
    """

    passed: bool = True
    failed_kind: Optional[ErrorType] = None
    dangling_refs: list[dict] = field(default_factory=list)  # ref_integrity dangling 项
    id_issues: list[dict] = field(default_factory=list)       # id_scope / 主键冲突
    type_issues: list[dict] = field(default_factory=list)     # 类型约束违反
    anti_pattern_hits: list[dict] = field(default_factory=list)  # anti_pattern 命中
    semantic_issues: list[dict] = field(default_factory=list)    # 值语义离群（Level 0 门）
    checked: int = 0
    raw: dict = field(default_factory=dict)  # 原始校验输出供诊断


@dataclass
class ClassifiedError:
    """分类后的错误对象。供 repair 策略路由与 LLM 诊断消费。"""

    error_type: ErrorType = ErrorType.UNKNOWN
    confidence: float = 0.0  # 0~1，分类置信度
    failed_col: Optional[str] = None
    failed_val: Optional[str] = None
    failed_step: Optional[str] = None
    detail: str = ""
    verify_signals: dict = field(default_factory=dict)
    root_cause: str = ""

    def as_feedback(self) -> dict:
        """结构化反馈字典，供 Level 2 LLM prompt 注入替代泛化文本堆栈。"""
        return {
            "error_type": self.error_type.value,
            "confidence": round(self.confidence, 3),
            "failed_col": self.failed_col,
            "failed_val": self.failed_val,
            "failed_step": self.failed_step,
            "root_cause": self.root_cause or self.detail,
            "verify_signals": self.verify_signals,
        }


def _extract_failed_step(result: Any) -> tuple[Optional[str], str]:
    """从 AgentResult 找首个失败 step，返回 (step_name, detail)。"""
    steps = getattr(result, "steps", None) or []
    for s in steps:
        if getattr(s, "ok", True) is False:
            return getattr(s, "name", None), getattr(s, "detail", "") or ""
    msg = getattr(result, "message", "") or ""
    return None, msg


def _parse_col_val(detail: str) -> tuple[Optional[str], Optional[str]]:
    """从 detail 正则提取失败列与值（与 agent._collect_error_feedback 对齐）。

    优先匹配带引号值 `值'Y'`，回退到无引号 `值 Y`（兼容 "值'abc'无法转为整数" 等尾随文本）。
    """
    cm = _COL_RE.search(detail)
    col = cm.group(1).strip() if cm else None
    vq = _VAL_QUOTED_RE.search(detail)
    if vq:
        return col, vq.group(1).strip()
    if col:
        vu = _VAL_UNQUOTED_RE.search(detail)
        if vu:
            return col, vu.group(1).strip()
    return col, None


def classify(
    error: Optional[BaseException],
    result: Any,
    verify_output: Optional[VerifyResult] = None,
    context: Optional[dict] = None,
) -> ClassifiedError:
    """把写操作失败归类为 ClassifiedError。纯规则，不调 LLM。

    优先级：verify 门控信号 > step.detail 类型模式 > message 文本 > unknown。
    context 可含 table_stem/sheet/path/intent，仅用于丰富 root_cause，不影响分类。
    """
    ctx = context or {}
    verify_output = verify_output or VerifyResult()

    # 1. verify 门控信号优先（高置信度，校验器已明确判定）
    if not verify_output.passed and verify_output.failed_kind is not None:
        signals = {
            "dangling_refs": verify_output.dangling_refs,
            "id_issues": verify_output.id_issues,
            "type_issues": verify_output.type_issues,
            "anti_pattern_hits": verify_output.anti_pattern_hits,
            "semantic_issues": verify_output.semantic_issues,
            "checked": verify_output.checked,
        }
        col = None
        val = None
        if verify_output.semantic_issues:
            si = verify_output.semantic_issues[0]
            col = si.get("column")
            val = si.get("value")
        elif verify_output.type_issues:
            ti = verify_output.type_issues[0]
            col = ti.get("column")
            val = ti.get("value")
        elif verify_output.dangling_refs:
            dr = verify_output.dangling_refs[0]
            col = dr.get("col_header")
            val = dr.get("value")
        elif verify_output.id_issues:
            ii = verify_output.id_issues[0]
            val = ii.get("value")
        return ClassifiedError(
            error_type=verify_output.failed_kind,
            confidence=0.9,
            failed_col=col,
            failed_val=val,
            verify_signals=signals,
            root_cause=f"verify 门控失败：{verify_output.failed_kind.value}",
        )

    # 2. step.detail / message 类型模式
    step_name, detail = _extract_failed_step(result)
    failed_col, failed_val = _parse_col_val(detail)

    # O20c：优先从 res.failures 结构化取 failed_col（agent._run_set 写入带 failed_col，
    # 比 detail regex 提取更可靠）。detail 可能是聚合 message 不含单列名。
    try:
        _rfailures = getattr(result, "failures", None) or []
        for _rf in _rfailures:
            if isinstance(_rf, dict) and _rf.get("failed_col"):
                failed_col = failed_col or _rf["failed_col"]
                if _rf.get("failed_val") is not None:
                    failed_val = failed_val or _rf["failed_val"]
                # kind=column_not_found 直接定类，免 regex 漏
                if _rf.get("kind") == "column_not_found":
                    return ClassifiedError(
                        error_type=ErrorType.COLUMN_NOT_FOUND,
                        confidence=0.85,
                        failed_col=failed_col,
                        failed_val=failed_val,
                        failed_step=step_name,
                        detail=detail,
                        root_cause=f"列名不存在：{failed_col or '未知列'}",
                    )
                break
    except Exception:
        pass

    # 2a. 异常对象文本（兜底，多数写操作不抛异常）
    err_text = str(error) if error else ""
    text = f"{detail}\n{err_text}".strip()
    msg = getattr(result, "message", "") or ""
    full = f"{text}\n{msg}"

    # type_mismatch（_coerce_value 的 "类型为 int，值'X'无法转为整数" 模式）
    if _TYPE_MISMATCH_RE.search(full):
        tm = _TYPE_MISMATCH_RE.search(full)
        return ClassifiedError(
            error_type=ErrorType.TYPE_MISMATCH,
            confidence=0.85,
            failed_col=failed_col,
            failed_val=failed_val,
            failed_step=step_name,
            detail=detail,
            root_cause=f"类型不符：期望 {tm.group(1)}，值 '{failed_val}' 无法转换",
        )

    # column_not_found
    # O21 真错误：headers 比对前去 `:` 后缀（如 "类型:int" 取 "类型"），避免 LLM 产
    # 的带后缀列名 not in headers 误判为列不存在（实为列名+类型标注，列存在）。
    _hdr_plain = [(h.split(":")[0] if h else h) for h in (ctx.get("headers") or [])]
    if _COL_NOT_FOUND_RE.search(full) or (failed_col and _hdr_plain and failed_col not in _hdr_plain):
        return ClassifiedError(
            error_type=ErrorType.COLUMN_NOT_FOUND,
            confidence=0.8,
            failed_col=failed_col,
            failed_val=failed_val,
            failed_step=step_name,
            detail=detail,
            root_cause=f"列名不存在：{failed_col or '未知列'}",
        )

    # row_not_found
    if _ROW_NOT_FOUND_RE.search(full):
        return ClassifiedError(
            error_type=ErrorType.ROW_NOT_FOUND,
            confidence=0.75,
            failed_col=failed_col,
            failed_val=failed_val,
            failed_step=step_name,
            detail=detail,
            root_cause="行定位零命中或未找到",
        )

    # id_conflict
    if _ID_CONFLICT_RE.search(full):
        return ClassifiedError(
            error_type=ErrorType.ID_CONFLICT,
            confidence=0.8,
            failed_col=failed_col,
            failed_val=failed_val,
            failed_step=step_name,
            detail=detail,
            root_cause="主键重复或 ID 冲突",
        )

    # formula_error
    if _FORMULA_RE.search(full):
        return ClassifiedError(
            error_type=ErrorType.FORMULA_ERROR,
            confidence=0.75,
            failed_col=failed_col,
            failed_val=failed_val,
            failed_step=step_name,
            detail=detail,
            root_cause="公式引用断裂或语法错误",
        )

    # pk_misplaced：依赖 context 中的 is_misplaced_pk 信号（agent 在 add 前判定）
    if ctx.get("pk_misplaced"):
        return ClassifiedError(
            error_type=ErrorType.PK_MISPLACED,
            confidence=0.8,
            failed_val=ctx.get("pk_value"),
            failed_step=step_name,
            detail=detail,
            root_cause="主键值误塞（重复/非数字/疑似效果码）",
        )

    # needs_confirm + anti_pattern block（如 anti_pattern_block step）
    if step_name and "anti_pattern" in step_name.lower():
        return ClassifiedError(
            error_type=ErrorType.UNKNOWN,
            confidence=0.5,
            failed_step=step_name,
            detail=detail,
            verify_signals={"anti_pattern_block": True},
            root_cause=f"反模式拦截：{detail}",
        )

    # 合并单元格/只读结构错误：sheet 结构不宜写（列举目录/合并表头/只读模式）。
    # 归 SEMANTIC_OUTLIER → 触发 ask 用户路径（不浪费自动修复轮次）。
    if _MERGED_CELL_RE.search(full):
        return ClassifiedError(
            error_type=ErrorType.SEMANTIC_OUTLIER,
            confidence=0.8,
            failed_col=failed_col,
            failed_val=failed_val,
            failed_step=step_name,
            detail=detail,
            root_cause=("目标 sheet 含合并单元格或为只读结构，不宜直接写入"
                       "（多为列举目录/合并表头 sheet），需人工确认落表位置"),
        )

    # 3. 兜底
    return ClassifiedError(
        error_type=ErrorType.UNKNOWN,
        confidence=0.3,
        failed_col=failed_col,
        failed_val=failed_val,
        failed_step=step_name,
        detail=detail or msg or err_text,
        root_cause="未知错误类型，走通用 error_feedback retry 兜底",
    )


def build_user_friendly(error_type: str, root_cause: str = "",
                        table: str = "", sheet: str = "",
                        failed_col: str = "", failed_val: str = "",
                        suggested_id=None, issue_type: str = "",
                        dangling_lines: list = None) -> dict:
    """要求 B：把技术错误转策划能懂的大白话 reason + action 建议。

    覆盖三类冲突（PK / placeholder / dangling FK）+ verify_repair 兜底。
    返回 {reason, action} 供 ask payload 挂 user_friendly 字段。
    前端优先渲染 user_friendly.reason 而非 root_cause 技术词。
    """
    _et = (error_type or "").lower()
    _it = (issue_type or "").lower()
    # PK 冲突（id_conflict / unique_violation / pk_misplaced）
    if (_et in ("id_conflict", "pk_misplaced") or _it == "unique_violation"):
        _val = failed_val or ""
        _col = failed_col or "编号"
        if suggested_id is not None:
            return {
                "reason": f"你填的「{_col}」值「{_val}」已经被别的数据用了，换个编号吧。",
                "action": f"建议改成「{suggested_id}」（系统自动找的下一个可用编号），点「接受」即可；也可手动填别的编号。",
            }
        return {
            "reason": f"你填的「{_col}」值「{_val}」已经被别的数据用了，换个编号吧。",
            "action": "请在下方输入一个新的编号（数字），或点「跳过」放弃此项。",
        }
    # placeholder 未解（forward_ref_broken）
    if _it == "forward_ref_broken" or _et == "cross_ref_broken":
        _col = failed_col or ""
        return {
            "reason": f"这项「{_col}」需要先建好依赖的东西才能填，但那个依赖现在还没建出来。",
            "action": "你可以：①先去建依赖项 ②手动填一个已有的编号 ③点「跳过」放弃此项。",
        }
    # dangling FK（写后深度校验）
    if dangling_lines:
        _first = dangling_lines[0] if len(dangling_lines) == 1 else None
        if _first:
            return {
                "reason": f"你引用的内容在对应表里找不到（{_first}）。",
                "action": "请先去建被引用的那行数据，或换一个已经存在的编号；也可点「跳过」放弃。",
            }
        return {
            "reason": "你引用的一些内容在对应表里找不到，功能没接通。",
            "action": "请先去建被引用的那些数据，或换已经存在的编号；也可点「跳过」放弃。",
        }
    # 列不存在
    if _et == "column_not_found":
        return {
            "reason": f"「{failed_col or '某列'}」这列在表里不存在，可能是写错了列名或表选错了。",
            "action": "请在下方填正确的列名，或换一张表；也可点「跳过」放弃此项。",
        }
    # 类型不符
    if _et == "type_mismatch":
        return {
            "reason": f"「{failed_col or '某列'}」的值「{failed_val or ''}」格式不对，需要填数字但你填了别的。",
            "action": "请在下方填一个正确的数字（或枚举编号）；也可点「跳过」放弃此项。",
        }
    # verify_repair 兜底
    return {
        "reason": root_cause or "这项数据写入多次失败，可能是内容上的问题。",
        "action": "请在下方表格按失败列填入正确值，或点「跳过」放弃此项继续后续任务。",
    }


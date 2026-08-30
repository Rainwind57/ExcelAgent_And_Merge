"""自然语言意图数据结构。

NLIntent 是 codemaker LLM 解析器（CodemakerNLParser）输出的结构化意图，
由 TableAgent 消费执行。

4-Step Loop 架构（§二数据契约 / §2.9 路线 A）扩展：
  在原 NLIntent 基础上加 produces_label/consumes_labels/source/ai_check_skipped/
  validation/execution 字段，使其承载 SubTask 的"已 schema 化、已 plan 化"超集语义。
  旧字段全部保留，#22/#25 splitter 保护语义不变。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Literal, Optional


class IssueType(str, Enum):
    """字段层 + FK 层校验 issue 类型枚举（§3.2 §4.4）。

    值为字符串（str 继承），可直接 JSON 序列化供前端 ask-card 渲染。
    """
    MISSING_REQUIRED = "missing_required"        # 必填列缺失
    TYPE_MISMATCH = "type_mismatch"              # 类型 coerce 失败
    UNIQUE_VIOLATION = "unique_violation"        # 唯一列值重复
    ENUM_INVALID = "enum_invalid"                 # 枚举白名单外
    FORWARD_REF_BROKEN = "forward_ref_broken"    # 前向引用未在本批 produces
    RANGE_OUTLIER = "range_outlier"               # 范围/分布离群（modify）
    COL_NOT_FOUND = "col_not_found"              # 列不存在（LLM 幻觉列）
    SCHEMA_MISSING = "schema_missing"            # 拉不到 sheet schema
    ID_OUT_OF_SCOPE = "id_out_of_scope"          # ID 列值越界 id_mgr 预留段（O4）


@dataclass
class Issue:
    """单条校验 issue（§4.4 tips 序列化单元）。

    col:          出问题的列名
    issue_type:   IssueType 值（str）
    expected:     期望（如 "int"/"枚举:1,2,3"/"必填"）
    suggestion:   修正建议
    value:        实际值（供前端展示）
    suggested_combo: 复合主键冲突时的可一键采纳组合值，形如 "列A=1,列B=2"
    """
    col: str = ""
    issue_type: str = ""
    expected: str = ""
    suggestion: str = ""
    value: Any = None
    suggested_combo: str = ""

    def to_dict(self) -> dict:
        return {
            "col": self.col, "issue_type": self.issue_type,
            "expected": self.expected, "suggestion": self.suggestion,
            "value": self.value, "suggested_combo": self.suggested_combo,
        }


def assemble_tips(issues_map: dict) -> list[dict]:
    """把 {subtask_id: list[Issue|dict]} 序列化为前端 ask-card 用的 tips 列表（§4.4）。

    返回 [{subtask_id, col, issue_type, expected, suggestion}]，
    供 _ask_callback 发 ask SSE 事件 → 前端 AgentChatView ask-card 渲染。
    """
    tips: list[dict] = []
    for sid, issues in (issues_map or {}).items():
        if not issues:
            continue
        for iss in issues:
            if isinstance(iss, Issue):
                d = iss.to_dict()
            elif isinstance(iss, dict):
                d = dict(iss)
            else:
                continue
            d["subtask_id"] = sid
            tips.append(d)
    return tips


@dataclass
class ValidationResult:
    """字段层 + FK 层校验结果（Step2 ValidateAgent 填充）。

    issues: [{col, issue_type, expected, suggestion}]；
            issue_type 枚举 missing_required / type_mismatch / unique_violation /
            enum_invalid / forward_ref_broken / range_outlier（§3.2 §4.4）。
    ok: 是否通过（无 issue 或全部 skipped 后置真）。
    skipped: 用户显式跳过（下游 ExecuteAgent 跳写盘）。
    """
    issues: list[dict] = field(default_factory=list)
    ok: bool = False
    skipped: bool = False
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """P27 checkpoint 序列化。"""
        return {"issues": list(self.issues), "ok": self.ok,
                "skipped": self.skipped, "raw": dict(self.raw)}

    @classmethod
    def from_dict(cls, d: dict) -> "ValidationResult":
        return cls(issues=list(d.get("issues") or []),
                   ok=bool(d.get("ok", False)),
                   skipped=bool(d.get("skipped", False)),
                   raw=dict(d.get("raw") or {}))


@dataclass
class ExecutionResult:
    """执行结果（Step3 ExecuteAgent 填充，§3.3）。

    ok: 是否写盘成功。
    row: 目标行号（modify/delete/单 cell set）。
    written_fields: 实际写盘的列名列表（用于汇总 success_list）。
    new_row_pk: add 写盘产出的新行 PK（供 produces_label 解析为字面值）。
    failure: #40 结构化失败 dict（type/table/sheet/col/root_cause/attempted/suggestion）。
    """
    ok: bool = False
    row: Optional[int] = None
    written_fields: list[str] = field(default_factory=list)
    new_row_pk: Any = None
    failure: Optional[dict] = None
    raw: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """P27 checkpoint 序列化。"""
        return {"ok": self.ok, "row": self.row,
                "written_fields": list(self.written_fields),
                "new_row_pk": self.new_row_pk, "failure": self.failure,
                "raw": dict(self.raw)}

    @classmethod
    def from_dict(cls, d: dict) -> "ExecutionResult":
        return cls(ok=bool(d.get("ok", False)),
                   row=d.get("row"),
                   written_fields=list(d.get("written_fields") or []),
                   new_row_pk=d.get("new_row_pk"),
                   failure=d.get("failure"),
                   raw=dict(d.get("raw") or {}))


@dataclass
class NLIntent:
    """自然语言解析后的意图结构。

    字段说明：
      action:         动作类型，取值 set | add | delete | get | col
      table_hint:     表名提示（供上层 TableResolver 做模糊匹配）
      sheet_hint:     sheet 名提示
      locator_field:  定位字段名（如 "名称"、"ID"）
      locator_value:  定位值（如对象名、ID 值等，用于找到目标行）
      target_field:   目标字段名（set 时被修改的字段）
      value:          目标值（set 时写入的值）
      raw_target:     分隔动词右侧的原始文本（供上层进一步解析）
      raw:            原始输入文本
      row_override:   用户显式指定的行号（如"用行6""第6行"），非 None 时跳过行定位直接读该行
      extras:         扩展字典，存放解析过程中产生的额外信息（如 fields、col_name）

    === 4-Step Loop 扩展（§2.9 路线 A，承载 SubTask 超集语义）===
      produces_label:   本子任务产出的新 ID 标签（被其他子任务 consumes）
      consumes_labels:  本子任务引用的 produces 标签列表
      source:           来源标记，决定下游走哪条路径：
                         nl(默认现状) | llm_decompose(schema-driven) | splitter_baseline(模板兜底)
      ai_check_skipped: 锁定字段不进 AI 重映射（#22/#25 splitter 保护）
      validation:       Step2 填充的校验结果
      execution:        Step3 填充的执行结果
    """
    action: str = "set"
    table_hint: Optional[str] = None
    sheet_hint: Optional[str] = None
    locator_field: Optional[str] = None
    locator_value: Optional[str] = None
    # 复合主键定位（如 (residence_id, obstacle_id)）。非空时优先于单值用于行定位。
    # LLM 对复合主键表产 locator_fields/locator_values 列表；单主键场景留空走单值。
    locator_fields: list[str] = field(default_factory=list)
    locator_values: list[str] = field(default_factory=list)
    target_field: Optional[str] = None
    value: Optional[str] = None
    raw_target: Optional[str] = None
    raw: str = ""
    row_override: Optional[int] = None
    extras: dict = field(default_factory=dict)

    # === 4-Step Loop 扩展（§2.9）===
    produces_label: Optional[str] = None
    consumes_labels: list[str] = field(default_factory=list)
    source: Literal["nl", "llm_decompose", "splitter_baseline"] = "nl"
    ai_check_skipped: bool = False
    validation: Optional[ValidationResult] = None
    execution: Optional[ExecutionResult] = None
    # P23：pre-validate 遗留 tips 软失败清单。O3 后 validate_two_layer 非阻断
    # （ok=True 恒），tips 供 thinking 展示但不上报 → CI/非交互带病照样落盘。
    # attach_tips_as_soft_failures 把遗留 tips 转 #40 形状软失败 dict 追加到
    # 此字段，partition 创建时 transfer 到 res.failures，保 D6 上报不静默吞。
    failures: list[dict] = field(default_factory=list)
    # P9：用户显式多 producer 同 sheet 标记。DecomposeAgent 标注「这是用户
    # 显式要的多行 op，非 LLM 过产」时设 True；_suppress_over_produce 跳过
    # 此类 op（不抑制），保留多 producer。默认 False = 旧行为（一表一 op
    # 契约，同 sheet 第二个 produces op 被当 LLM 过产抑制）。
    multi_op_same_sheet: bool = False

    def to_checkpoint_dict(self) -> dict:
        """P27：序列化为 JSON-able dict（4-step NL 路径 checkpoint）。

        拍 parse/validate 后中间态，stall 可从 checkpoint 续跑，免 Step1 重
        LLM decompose。validation/execution 嵌套 dataclass 经 to_dict 展开。
        extras/failures 等 dict/list 原样保留（需 JSON-able）。
        """
        return {
            "action": self.action, "table_hint": self.table_hint,
            "sheet_hint": self.sheet_hint, "locator_field": self.locator_field,
            "locator_value": self.locator_value,
            "locator_fields": list(self.locator_fields),
            "locator_values": list(self.locator_values),
            "target_field": self.target_field,
            "value": self.value, "raw_target": self.raw_target, "raw": self.raw,
            "row_override": self.row_override,
            "extras": dict(self.extras),
            "produces_label": self.produces_label,
            "consumes_labels": list(self.consumes_labels),
            "source": self.source, "ai_check_skipped": self.ai_check_skipped,
            "validation": self.validation.to_dict() if self.validation else None,
            "execution": self.execution.to_dict() if self.execution else None,
            "failures": list(self.failures),
            "multi_op_same_sheet": self.multi_op_same_sheet,
        }

    @classmethod
    def from_checkpoint_dict(cls, d: dict) -> "NLIntent":
        """P27：反序列化（to_checkpoint_dict 的逆）。重建嵌套 validation/execution。"""
        v = d.get("validation")
        e = d.get("execution")
        return cls(
            action=d.get("action", "set"),
            table_hint=d.get("table_hint"),
            sheet_hint=d.get("sheet_hint"),
            locator_field=d.get("locator_field"),
            locator_value=d.get("locator_value"),
            locator_fields=list(d.get("locator_fields") or []),
            locator_values=list(d.get("locator_values") or []),
            target_field=d.get("target_field"),
            value=d.get("value"),
            raw_target=d.get("raw_target"),
            raw=d.get("raw", ""),
            row_override=d.get("row_override"),
            extras=dict(d.get("extras") or {}),
            produces_label=d.get("produces_label"),
            consumes_labels=list(d.get("consumes_labels") or []),
            source=d.get("source", "nl"),
            ai_check_skipped=bool(d.get("ai_check_skipped", False)),
            validation=ValidationResult.from_dict(v) if v else None,
            execution=ExecutionResult.from_dict(e) if e else None,
            failures=list(d.get("failures") or []),
            multi_op_same_sheet=bool(d.get("multi_op_same_sheet", False)),
        )

"""修复上下文 scratchpad：单次 run 内累积修复历史，供迭代环决策避免重复策略。

设计动机：
    verify→repair→execute 迭代环需要记录「试过什么、为什么失败、产出了什么修正」：
      - 避免重复同一策略无效循环（is_repeat_strategy）
      - 跨表 DAG 后续步骤引用 repair 产出的实际 ID（resolved_placeholders）
      - Level 2 LLM 诊断可读取 error_type_history 判断错误是否在升级
    完整结构化 WorkingMemory（B1）与 checkpointer 持久化（A3）是 P1/P3，本模块仅
    提供 in-run 状态，run 结束丢弃，不跨 run 持久化。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .error_classifier import ErrorType


@dataclass
class FailedOpRecord:
    """单次失败操作记录。"""

    task_id: Optional[str] = None
    error_type: ErrorType = ErrorType.UNKNOWN
    strategy_name: str = ""
    attempt: int = 0  # 第几轮 repair（1-based）
    failed_col: Optional[str] = None
    failed_val: Optional[str] = None
    detail: str = ""
    resolved: bool = False  # 后续轮是否修复成功


@dataclass
class RepairContext:
    """单次 run 的修复上下文。in-run，run 结束丢弃。"""

    attempts: int = 0
    failed_ops: list[FailedOpRecord] = field(default_factory=list)
    resolved_placeholders: dict[str, int] = field(default_factory=dict)
    error_type_history: list[ErrorType] = field(default_factory=list)
    # 已用 (error_type, strategy_name) 对，按轮次顺序
    used_strategies: list[tuple[ErrorType, str]] = field(default_factory=list)
    # repair 产出的结构化失败（达上限仍失败时上报）
    final_failure: Optional[dict] = None

    def record_attempt(
        self,
        error_type: ErrorType,
        strategy_name: str,
        task_id: Optional[str] = None,
        failed_col: Optional[str] = None,
        failed_val: Optional[str] = None,
        detail: str = "",
    ) -> None:
        """记录一轮 repair 尝试。"""
        self.attempts += 1
        self.error_type_history.append(error_type)
        self.used_strategies.append((error_type, strategy_name))
        self.failed_ops.append(
            FailedOpRecord(
                task_id=task_id,
                error_type=error_type,
                strategy_name=strategy_name,
                attempt=self.attempts,
                failed_col=failed_col,
                failed_val=failed_val,
                detail=detail,
            )
        )

    def is_repeat_strategy(self, error_type: ErrorType, strategy_name: str) -> bool:
        """检测本轮选定的 (error_type, strategy) 是否与历史某轮完全相同。

        相同 error_type + 相同策略名视为重复——无进展，应跳过升级或中止。
        """
        return (error_type, strategy_name) in self.used_strategies

    def last_error_type(self) -> Optional[ErrorType]:
        return self.error_type_history[-1] if self.error_type_history else None

    def resolve_placeholder(self, placeholder: str, actual_id: int) -> None:
        """记录 repair 产出的实际 ID，供后续跨表 DAG 步骤引用。"""
        self.resolved_placeholders[placeholder] = actual_id

    def set_final_failure(self, failure: dict) -> None:
        """达上限仍失败时设置结构化失败，供 Step6 汇总上报。"""
        self.final_failure = failure

    def summarized_strategies(self) -> list[str]:
        """已尝试策略清单，供上报与 LLM 诊断。"""
        return [f"轮{r.attempt}:{r.error_type.value}/{r.strategy_name}" for r in self.failed_ops]

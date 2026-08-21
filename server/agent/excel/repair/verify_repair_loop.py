"""Verify-Repair Loop 模块（§3.2 解耦抽文件）。

现状：`_run_verify_repair_loop` 循环主体（agent.py:6109-6372, 464 行）+ 8 helper
散落 agent.py。本模块逐步抽离 verify-repair 相关纯函数 + 循环主体。

本批（最小版）：
  - `check_type_constraint` 纯函数（从 `agent._check_type_constraint` 抽离,零 self 依赖）
  - `agent._check_type_constraint` 薄转发到本模块

待后续（大重构,§3.2 完整版）：
  - `_run_verify_repair_loop` 循环主体抽到本模块（`run_verify_repair_loop` 函数）
  - `_verify_write` 抽离（需传 cli/semantic_gate 引用）
  - 其余 helper（`_apply_repair_fix`/`_llm_call`/`_run_react_repair`/`_llm_diagnose_only`/
    `_record_repair_signal`/`_safe_redispatch`/`_rollback_write`）经 agent 回调传入
  - 接口：`run_verify_repair_loop(agent, intent, path, sheet, res, out, backup_file, is_write)`
    或收 agent 回调 dict：`{apply_fix, safe_redispatch, rollback_write, run_react, ...}`

风险：464 行循环 + 8 helper 依赖,改主路径 verify-repair。建议保留 `agent._verify_write`
薄转发避免改 `_phase_execute` 多处调用点。

注意：`execute_no_llm=1`（§3.1）已跳 verify-repair LLM,§3.2 抽文件为代码组织优化,
无功能影响。`enable_verify_repair_loop` 默认开（现状）。
"""
from __future__ import annotations

from typing import Any


def check_type_constraint(col_type: str, value: Any) -> tuple[bool, str]:
    """轻量类型校验（int/float/bool），复用 repair_playbook._coerce_value 语义。

    §3.2 从 `agent._check_type_constraint`（agent.py:5680）抽离。
    纯函数,零 self 依赖,可独立单测。

    Args:
        col_type: 列类型字符串（int/float/bool 等,不区分大小写）
        value: 待校验值

    Returns:
        (ok, err_msg)。ok=True 通过；ok=False 时 err_msg 描述类型不符。
    """
    t = (col_type or "").strip().lower()
    if t in ("int", "integer", "long"):
        try:
            int(float(str(value)))
            return True, ""
        except (ValueError, TypeError):
            return False, f"int 类型不符：{value!r}"
    if t in ("float", "double", "number"):
        try:
            float(str(value))
            return True, ""
        except (ValueError, TypeError):
            return False, f"float 类型不符：{value!r}"
    if t in ("bool", "boolean"):
        if isinstance(value, bool) or str(value).strip().lower() in (
                "0", "1", "true", "false", "是", "否"):
            return True, ""
        return False, f"bool 类型不符：{value!r}"
    return True, ""


__all__ = ["check_type_constraint"]

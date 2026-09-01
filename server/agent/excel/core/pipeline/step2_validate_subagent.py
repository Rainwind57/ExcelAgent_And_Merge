"""4-Step V2 Step2 Validate SubAgent（§设计 S3）。

职责（严格限定）：
  - 字段层 + FK 层校验
  - PK 冲突检测 → ask 用户 → accept → 修正 intent
  - tips 序列化 + 交互反问

严禁：
  - 输入分析/拆分/定位（属 Step1）
  - 执行/写入（属 Step3）
  - 汇总/反模式归纳（属 Step4）

复用现有 _step2_validate_intents（validate_two_layer + ask + 修正），
包装为统一 StepResult。错误归属固定 step2_validate。
"""
from __future__ import annotations

import logging
import copy
import time
from typing import Any, Optional

from .contracts import STEP2_VALIDATE, StepContext, StepError, StepResult

logger = logging.getLogger(__name__)


class Step2ValidateSubAgent:
    """Step2：初形成指令的校验、冲突处理。"""

    def __init__(self, services: Any = None):
        # 注入 ExcelAgentServices（替代原 agent=self 散播私态）。
        # services 收口 _wire_sinks / validate_intents 接口。
        self._services = services

    @staticmethod
    def _structural_errors(intents: list) -> list[StepError]:
        """Cheap Step2 guard that does not need the legacy TableAgent service."""
        errors: list[StepError] = []
        for idx, it in enumerate(intents):
            action = (getattr(it, "action", "") or "").strip().lower()
            table = getattr(it, "table_hint", None)
            sheet = getattr(it, "sheet_hint", None)
            if action not in {"add", "modify", "set", "delete", "get", "col"}:
                errors.append(StepError(
                    step_id=STEP2_VALIDATE,
                    error_type="invalid_action",
                    message=f"Intent {idx + 1} has unsupported action: {action or '<empty>'}",
                    table=table, sheet=sheet, is_hard=True))
            if action in {"add", "modify", "set", "delete", "get", "col"} and not table:
                errors.append(StepError(
                    step_id=STEP2_VALIDATE,
                    error_type="table_missing",
                    message=f"Intent {idx + 1} has no target table",
                    sheet=sheet, is_hard=True))
            if action in {"add", "set"}:
                fields = (getattr(it, "extras", None) or {}).get("fields")
                if fields is not None and not isinstance(fields, dict):
                    errors.append(StepError(
                        step_id=STEP2_VALIDATE,
                        error_type="fields_not_object",
                        message=f"Intent {idx + 1} fields must be a JSON object",
                        table=table, sheet=sheet, is_hard=True))
                if action == "add" and not isinstance(fields, dict):
                    errors.append(StepError(
                        step_id=STEP2_VALIDATE,
                        error_type="add_fields_missing",
                        message=f"Intent {idx + 1} add operation has no fields object",
                        table=table, sheet=sheet, is_hard=True))
                if action == "set":
                    has_target = bool(getattr(it, "target_field", None))
                    has_fields = isinstance(fields, dict) and bool(fields)
                    if not has_target and not has_fields:
                        errors.append(StepError(
                            step_id=STEP2_VALIDATE,
                            error_type="set_target_missing",
                            message=f"Intent {idx + 1} set operation has no target field or fields",
                            table=table, sheet=sheet, is_hard=True))
        return errors

    @staticmethod
    def _step1_quality_errors(step1_quality: dict | None) -> list[StepError]:
        if not isinstance(step1_quality, dict):
            return []
        issues = step1_quality.get("issues") or []
        errors: list[StepError] = []
        for issue in issues:
            if not isinstance(issue, dict):
                continue
            if issue.get("severity") != "hard":
                continue
            issue_type = str(issue.get("type") or "step1_quality").strip()
            errors.append(StepError(
                step_id=STEP2_VALIDATE,
                error_type=f"step1_{issue_type}",
                message="Step1 produced structurally unsafe intent JSON",
                root_cause=str(issue),
                table=issue.get("table"),
                sheet=issue.get("sheet"),
                column=issue.get("column") or issue.get("col"),
                suggestion="Fix Step1 reference graph before validation/execution",
                is_hard=True))
        return errors

    @staticmethod
    def _semantic_compile_errors(report: dict | None) -> list[StepError]:
        if not isinstance(report, dict):
            return []
        errors: list[StepError] = []
        for issue in report.get("issues") or []:
            if not isinstance(issue, dict):
                continue
            if issue.get("severity") != "hard":
                continue
            issue_type = str(issue.get("type") or "semantic_compile").strip()
            errors.append(StepError(
                step_id=STEP2_VALIDATE,
                error_type=f"semantic_{issue_type}",
                message="Semantic plan could not be safely compiled",
                root_cause=str(issue),
                suggestion="Fix semantic plan structure before validation/execution",
                is_hard=True))
        return errors

    def execute(self, ctx: StepContext) -> StepResult:
        """Step2 执行：对 Step1 产出的 intents 跑 validate_two_layer。

        错误归属：所有错误 step_id=STEP2_VALIDATE。
        - 校验发现 issue 且用户 skip/未解 → soft error（仍交 Step3，可能失败）
        - 校验异常 → soft error（不阻断，交 Step3 尝试）
        - Step1 无 intents → 直接 ok=True 跳过（Step1 已 hard 报错）
        """
        t0 = time.time()
        errors: list[StepError] = []
        warnings: list[str] = []
        s1 = ctx.get_result("step1_parse")
        intents = (s1.artifacts.get("intents") if s1 and s1.ok else None) or []
        _HARD_TYPES: set[str] = {
            "unique_violation",
            "type_mismatch",
            "col_not_found",
        }

        def _is_hard_validation_issue(error_type: str = "",
                                      issue_type: str = "",
                                      root: str = "") -> bool:
            vals = {
                str(error_type or "").strip().lower(),
                str(issue_type or "").strip().lower(),
            }
            root_l = str(root or "").strip().lower()
            return bool(vals & _HARD_TYPES) or any(
                root_l == t or root_l.startswith(f"{t}:")
                for t in _HARD_TYPES
            ) or (("missing_required" in vals or root_l.startswith("missing_required:"))
                  and ("业务必填列" in str(root or "")
                       or "指令明确" in str(root or "")))

        if not intents:
            # Step1 已 hard 报错，Step2 无需校验
            return StepResult(
                step_id=STEP2_VALIDATE, ok=True,
                warnings=["Step1 无 intents，跳过校验"],
                metrics={"dur_ms": int((time.time() - t0) * 1000),
                         "intents": 0},
                artifacts={"validated": []})

        validated = copy.deepcopy(intents)
        errors.extend(self._structural_errors(validated))
        step1_quality = s1.artifacts.get("step1_quality") if s1 else None
        semantic_plan = s1.artifacts.get("semantic_plan") if s1 else None
        semantic_compile_report = (
            s1.artifacts.get("semantic_compile_report") if s1 else None
        )
        errors.extend(self._step1_quality_errors(step1_quality))
        errors.extend(self._semantic_compile_errors(semantic_compile_report))
        if self._services is not None:
            try:
                # 复用 legacy validate_two_layer + ask + 修正
                # _stream_res 用真实首个 intent 构造（原硬编码 action="get" 伪造语义错误，
                # 仅用于 _step2_validate_intents 内 add_thinking 标签 + failures 收集载体）。
                from ..agent import AgentResult
                _tmp_res = AgentResult(ok=True, intent=validated[0])
                _tmp_res = self._services.wire_sinks(_tmp_res)
                # locator_result 从 s1.artifacts 读（替代探 _last_locator_result 私态）。
                # Step1 已显式产出到 artifacts["locator_results"]（全段 list），取首段
                # 与原单值 _last_locator_result 语义一致。消除 contracts.py:16 步间隔离违反。
                _lr = s1.artifacts.get("locator_result") if s1 else None
                if _lr is None:
                    _locator_results = (s1.artifacts.get("locator_results") if s1 else None) or []
                    _lr = _locator_results[0] if _locator_results else None
                validated = self._services.validate_intents(
                    validated, _tmp_res, ctx.session_id, locator_result=_lr)
                # 收集校验产出的 failures，据 issue_type 映射硬/软类别上报。
                from ...subagent.validator_agent import IssueType
                # 口径对齐 validator_agent.validate_two_layer 内部真实 gate
                # （_hard_issue_types @ validator_agent.py:1285 + _is_pk_missing）：
                #   UNIQUE_VIOLATION / TYPE_MISMATCH / COL_NOT_FOUND → Step 级 hard
                #     （validator 已在 validate_field_layer 做中文表头/英文规范名/去下标/
                #      点分末段多级规范化匹配，仍不命中=真列名错，ask 让用户改而非扔 Step3 兜底）
                #   MISSING_REQUIRED → 仅【主键列】缺失才 hard（validator 内部
                #     _is_pk_missing 精确判定后标 skipped）；非主键必填缺失降 warning，
                #     交 Step3 写盘兜底。本层无 _pk_cols_cache 无法判主键，故不把
                #     MISSING_REQUIRED 整类标 hard——否则非主键必填命中即触发
                #     has_hard_error()→orchestrator hard→_final→Step3 整批不跑，
                #     一条可恢复问题拖垮整条流水线（过度阻断而非漏校验）。
                # 单条 intent 的阻断由 validator 内部 _mark_intent_skipped 精确落地，
                # 本层 is_hard 仅作前端/Step4 旗标镜像，不承担 主键判定职责。
                _HARD_TYPES = {
                    IssueType.UNIQUE_VIOLATION.value,
                    IssueType.TYPE_MISMATCH.value,
                    IssueType.COL_NOT_FOUND.value,
                }
                _skipped_ids = {
                    id(it) for it in validated
                    if getattr(it, "validation", None)
                    and getattr(it.validation, "skipped", False)
                }
                if _skipped_ids:
                    _tmp_res.add_thinking(
                        "校验", f"{len(_skipped_ids)} 条子任务 Step2 未解决，保留到 Step3 显式跳过并上报")
                for f in (getattr(_tmp_res, "failures", None) or []):
                    if isinstance(f, dict):
                        _itype = f.get("issue_type") or f.get("type", "")
                        _root = f.get("root_cause") or f.get("message") or ""
                        _is_hard = _is_hard_validation_issue(
                            f.get("type", "validate_issue"), _itype, _root)
                        errors.append(StepError(
                            step_id=STEP2_VALIDATE,
                            error_type=f.get("type", "validate_issue"),
                            message=f.get("root_cause") or f.get("message") or "校验问题",
                            table=f.get("table"), sheet=f.get("sheet"),
                            column=f.get("col"),
                            suggestion=f.get("suggestion"),
                            is_hard=_is_hard))
            except Exception as e:  # noqa: BLE001
                logger.warning("Step2 校验异常", exc_info=True)
                errors.append(StepError(
                    step_id=STEP2_VALIDATE, error_type="validate_internal",
                    message="指令校验失败",
                    root_cause=f"{type(e).__name__}: {e}", is_hard=False))
                warnings.append("校验异常，保持原意图交 Step3")

        # validated 只存 Step2 自己的 artifacts（§硬隔离不变量：前一态不被后步改写）。
        # 原 s1.artifacts["validated"] = validated 已删除（违反 contracts.py 步间只追加不回退）。
        # Step3 只从 s2.artifacts["validated"] 取（step3:58-61 已优先 s2）。
        seen = {
            (e.error_type, e.table, e.sheet, e.column, e.root_cause or e.message)
            for e in errors
        }
        for it in validated:
            for f in (getattr(it, "failures", None) or []):
                if not isinstance(f, dict):
                    continue
                table = f.get("table") or getattr(it, "table_hint", None)
                sheet = f.get("sheet") or getattr(it, "sheet_hint", None)
                root = f.get("root_cause") or f.get("message") or ""
                key = (f.get("type", "validation_tip"), table, sheet, f.get("col"), root)
                if key in seen:
                    continue
                seen.add(key)
                issue_type = f.get("issue_type") or f.get("type", "")
                errors.append(StepError(
                    step_id=STEP2_VALIDATE,
                    error_type=key[0],
                    message=root or "Validation warning",
                    root_cause=root,
                    table=table, sheet=sheet, column=f.get("col"),
                    suggestion=f.get("suggestion"),
                    is_hard=_is_hard_validation_issue(key[0], issue_type, root)))

        ok = not any(e.is_hard for e in errors)
        return StepResult(
            step_id=STEP2_VALIDATE, ok=ok,
            errors=errors, warnings=warnings,
            metrics={"dur_ms": int((time.time() - t0) * 1000),
                     "intents": len(validated),
                     "step1_quality_hard": (
                         step1_quality or {}).get("hard_count", 0)
                     if isinstance(step1_quality, dict) else 0},
            artifacts={"validated": validated,
                       "step1_quality": step1_quality,
                       "semantic_plan": semantic_plan,
                       "semantic_compile_report": semantic_compile_report})


__all__ = ["Step2ValidateSubAgent"]

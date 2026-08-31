"""4-Step V2 Step3 Execute SubAgent（§设计 S3）。

职责（严格限定）：
  - 拓扑排序（复用 OperationOrchestrator）
  - 确定化派发原子 API
  - 占位符字面代换（consume:label → 真实 PK）
  - 收集 results + failures

严禁（D4 硬约束）：
  - LLM 调用（plan/validate/diagnose/repair/reparse 全禁）
  - 字段校验（属 Step2，已做完）
  - ask 冲突处理（属 Step2，已做完）
  - 输入分析（属 Step1）

复用现有 _run_single（已含 dispatch），通过 no_llm=True 参数透传零 LLM 不变量：
  - _run_single 内部临时设 agent.execute_no_llm=True（thread-local，try/finally 还原）
  - 短路 _phase_execute:6420 的越界 LLM 路径（ai_plan/validate/diagnose/repair/reparse）
  - 替代原 os.environ["CODEMAKER_EXECUTE_NO_LLM"] 进程级突变（污染并发 + 触发 P19）
错误归属固定 step3_execute。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

from .contracts import STEP3_EXECUTE, StepContext, StepError, StepResult

logger = logging.getLogger(__name__)

# 与 operation_orchestrator._PLACEHOLDER_RE 同构（本地复制，避免依赖其内部私有符号
# 跨模块耦合）：匹配 <label> 形式占位符，label 允许中英文/下划线。
_PLACEHOLDER_RE = re.compile(r"<\s*([\w\u4e00-\u9fff]+)\s*>")


def _find_unresolved_placeholders(it: Any) -> list[str]:
    """扫描 intent 可代换字段，收集 resolve 后仍悬空的上游引用标签（排除 <auto>）。

    §P0 缓解（无跨表回滚场景下的孤儿行）：<auto> 是"待补"哨兵，_coerce_value
    已优雅处理为跳过该列，非上游依赖引用，不算悬空依赖。其余 <label> 若
    _resolve_placeholders 后仍原样保留，说明其 producer 未产出（拓扑序内未执行到，
    或已执行但 res.ok is False 未被 _capture_produced 收录）——继续跑 run_single
    会让 _coerce_value 把该 FK 列静默置空写入（只留 needs_user_fill 提示，行本身
    仍落盘），产出缺外键的孤儿行。改为执行前显式拦截 + 清晰上报，不产生残缺行。

    §自增主键占位符豁免：Step1 对"主键自动分配"的 add 会给主键列填
    `<new_<stem>_id>`（本 intent 自己的 produces_label），这是"本行待分配主键"，
    不是引用上游产出。若把它当悬空依赖拦截，所有自增主键的 add 都会被误跳过
    （寒铁矿石单条新增即因此 Step3 假失败）。本函数排除本 intent 自身的
    produces_label（及 extras.produces）后再判悬空。
    """
    found: list[str] = []
    _own_labels = set()
    for _src in (getattr(it, "produces_label", None),
                 (getattr(it, "extras", None) or {}).get("produces")):
        if isinstance(_src, str) and _src.strip():
            _own_labels.add(_src.strip().strip("<>").strip())

    def _scan(v: Any) -> None:
        if isinstance(v, str) and "<" in v:
            for m in _PLACEHOLDER_RE.finditer(v):
                label = m.group(1)
                if label.lower() == "auto":
                    continue
                if label in _own_labels:
                    continue  # 本行自增主键占位符，非上游引用
                found.append(label)

    _scan(getattr(it, "locator_value", None))
    _scan(getattr(it, "value", None))
    for v in (getattr(it, "locator_values", None) or []):
        _scan(v)
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if isinstance(fields, dict):
        for v in fields.values():
            _scan(v)
    return found


class Step3ExecuteSubAgent:
    """Step3：指令的高效拓扑应用。零 LLM。"""

    def __init__(self, services: Any = None):
        # 注入 ExcelAgentServices（替代原 agent=self 散播私态）。
        # services 收口 run_single 接口 + no_llm 透传通道（状态落地走 agent thread-local）。
        self._services = services

    def execute(self, ctx: StepContext) -> StepResult:
        """Step3 执行：validated intents → 拓扑派发 → results + failures。

        D4 硬约束：本步零 LLM。通过 no_llm=True 透传到 _run_single，
        内部临时设 agent.execute_no_llm=True（thread-local）短路越界 LLM 路径。
        """
        t0 = time.time()
        errors: list[StepError] = []
        warnings: list[str] = []
        all_steps: list = []
        all_result_rows: list = []
        sub_tasks: list = []
        # §中危 4 修复：execute 前后读 counter 差值 = 本步 LLM 调用数（替代入口 reset
        # 后的累计值，避免含 Step1/Step2 的 LLM 被计入 Step3 metrics）。
        _cnt_before = self._services.peek_llm_total() if self._services is not None else 0
        all_failures: list = []

        def _record_failure(f: dict, *, default_type: str = "execute_fail",
                            default_message: str = "Execute failed",
                            hard: bool = False) -> None:
            if not isinstance(f, dict):
                return
            all_failures.append(f)
            errors.append(StepError(
                step_id=STEP3_EXECUTE,
                error_type=f.get("type", default_type),
                message=f.get("root_cause") or f.get("message") or default_message,
                table=f.get("table"),
                sheet=f.get("sheet"), column=f.get("col"),
                suggestion=f.get("suggestion"),
                is_hard=hard))

        s1 = ctx.get_result("step1_parse")
        s2 = ctx.get_result("step2_validate")
        # 优先用 Step2 校验后的；无 Step2 则用 Step1 原始
        source = None
        if s2 and s2.artifacts.get("validated"):
            source = s2.artifacts["validated"]
        elif s1 and s1.artifacts.get("intents"):
            source = s1.artifacts["intents"]
        intents = source or []

        if not intents:
            return StepResult(
                step_id=STEP3_EXECUTE, ok=True,
                warnings=["无 intents，跳过执行"],
                metrics={"dur_ms": int((time.time() - t0) * 1000),
                         "intents": 0},
                artifacts={"subtasks": [], "results": []})

        if self._services is None:
            _failures = []
            for i, it in enumerate(intents):
                _fail = {
                    "type": "execute_service_missing",
                    "table": getattr(it, "table_hint", "") or "",
                    "sheet": getattr(it, "sheet_hint", "") or "",
                    "col": "",
                    "root_cause": "Step3 has no ExcelAgentServices; cannot dispatch intent",
                    "attempted_strategies": "",
                    "suggestion": "Inject ExcelAgentServices or run through TableAgent.run_v2",
                    "status": "failed",
                    "user_reply": None,
                }
                _failures.append(_fail)
                sub_tasks.append({
                    "index": i + 1,
                    "intent_action": getattr(it, "action", ""),
                    "ok": False,
                    "needs_confirm": False,
                    "message": _fail["root_cause"],
                    "steps": [],
                    "result_rows": [],
                    "table_stem": _fail["table"],
                    "table_sheet": _fail["sheet"],
                    "needs_user_fill": [],
                    "partial": False,
                })
                errors.append(StepError(
                    step_id=STEP3_EXECUTE,
                    error_type="execute_service_missing",
                    message=_fail["root_cause"],
                    table=_fail["table"] or None,
                    sheet=_fail["sheet"] or None,
                    suggestion=_fail["suggestion"],
                    is_hard=True))
            return StepResult(
                step_id=STEP3_EXECUTE, ok=False,
                errors=errors, warnings=warnings,
                metrics={
                    "dur_ms": int((time.time() - t0) * 1000),
                    "intents": len(intents),
                    "subtasks_ok": 0,
                    "subtasks_fail": len(sub_tasks),
                    "subtasks_pending": 0,
                    "llm_calls": 0,
                },
                artifacts={
                    "subtasks": sub_tasks, "results": all_result_rows,
                    "steps": all_steps, "failures": _failures,
                })

        # §P0-2 V2 跨表占位符链：接入 OperationOrchestrator 的拓扑排序 + produced 累积 +
        # 占位符替换。原 V2 逐条 run_single 无 produced dict，跨表 add 链（quest→spawn_quest_entity
        # consumes <new_quest_id>）占位符全悬空 → 弹 ask/失败。现按拓扑序执行，producer 先于
        # consumer，每条执行前替换 consumes 占位符，执行后捕获 produces 新 ID 写入 produced。
        try:
            from ..operation_orchestrator import OperationOrchestrator as _OO
            _ordered_idx = _OO._topo_order(intents)
            ordered = [intents[i] for i in _ordered_idx] if _ordered_idx else list(intents)
        except Exception:
            logger.debug("Step3 拓扑排序失败,按原序执行", exc_info=True)
            ordered = list(intents)
        # produced: {label: new_pk_value}，跨条 intent 累积，供后续 consumes 替换
        produced: dict[str, str] = {}
        _seq_counter: dict[str, int] = {}

        # D4 硬约束：执行阶段零 LLM。通过 no_llm=True 透传到 services.run_single
        # → _run_single，_run_single 内部设 agent.execute_no_llm（thread-local）
        # 短路越界 LLM 路径（_phase_execute 各 gate 读 thread-local 属性）。
        # services 侧不再冗余写 agent 实例属性，状态唯一来源 = agent thread-local。

        try:
            for i, it in enumerate(ordered):
                # §P0-2 占位符替换：执行前把 consumes 的 <label> 占位符替换成 produced 真实 ID
                try:
                    _OO._resolve_placeholders(it, produced)
                except Exception:
                    logger.debug("Step3 _resolve_placeholders 失败", exc_info=True)
                # §P0 孤儿行防护：resolve 后仍悬空的非 <auto> 占位符 = 上游 producer
                # 未产出（拓扑序内还没跑到，或已跑但失败未被 _capture_produced 收录）。
                # 不拦截会走到 run_single → _coerce_value 静默把该 FK 列置空写入
                # （只留 needs_user_fill 提示，行仍落盘）→ 产出缺外键的孤儿行。
                # 本步无跨表事务回滚能力（Excel 写入非事务性，回滚需整文件快照，
                # 超出本次改动范围），但至少不该在明知依赖悬空时继续新写残缺行。
                _unresolved = _find_unresolved_placeholders(it)
                if _unresolved:
                    _rc = (f"上游依赖 {', '.join(sorted(set(_unresolved)))} 未产出"
                           f"（producer 未执行到或已失败），已跳过本条写入，"
                           f"避免生成缺失外键的残缺行")
                    _fail = {
                        "type": "upstream_placeholder_unresolved",
                        "table": getattr(it, "table_hint", "") or "",
                        "sheet": getattr(it, "sheet_hint", "") or "",
                        "col": "", "root_cause": _rc,
                        "attempted_strategies": "", "suggestion": "检查上游新增操作是否成功",
                        "status": "failed", "user_reply": None,
                    }
                    all_failures.append(_fail)
                    _is_partial = bool(getattr(sub_res, "partial", False))
                    _subtask_ok = False if _is_partial else sub_res.ok
                    sub_tasks.append({
                        "index": i + 1, "intent_action": it.action, "ok": False,
                        "needs_confirm": False, "message": _rc,
                        "steps": [], "result_rows": [],
                        "table_stem": getattr(it, "table_hint", "") or "",
                        "table_sheet": getattr(it, "sheet_hint", "") or "",
                        "needs_user_fill": [], "partial": False,
                    })
                    errors.append(StepError(
                        step_id=STEP3_EXECUTE, error_type="upstream_placeholder_unresolved",
                        message=_rc, table=getattr(it, "table_hint", None),
                        sheet=getattr(it, "sheet_hint", None), is_hard=False))
                    continue
                try:
                    sub_res = self._services.run_single(
                        it, getattr(ctx, "confirm_token", None), ctx.session_id,
                        suppress_phase_thinking=False, no_llm=True)
                    # §P0-2 捕获 produced：add 成功后新 PK ID 写入 produced 供下游 consumes
                    try:
                        _OO._capture_produced(sub_res, it, produced, _seq_counter)
                    except Exception:
                        logger.debug("Step3 _capture_produced 失败", exc_info=True)
                    if sub_res is None:
                        _rc = "Step3 dispatcher returned no result"
                        _fail = {
                            "type": "execute_empty_result",
                            "table": getattr(it, "table_hint", "") or "",
                            "sheet": getattr(it, "sheet_hint", "") or "",
                            "col": "",
                            "root_cause": _rc,
                            "attempted_strategies": "direct_dispatch",
                            "suggestion": "Check TableAgent._run_single dispatch path",
                            "status": "failed",
                            "user_reply": None,
                        }
                        all_failures.append(_fail)
                        sub_tasks.append({
                            "index": i + 1,
                            "intent_action": getattr(it, "action", ""),
                            "ok": False,
                            "needs_confirm": False,
                            "message": _rc,
                            "steps": [],
                            "result_rows": [],
                            "table_stem": _fail["table"],
                            "table_sheet": _fail["sheet"],
                            "needs_user_fill": [],
                            "partial": False,
                        })
                        errors.append(StepError(
                            step_id=STEP3_EXECUTE,
                            error_type="execute_empty_result",
                            message=_rc,
                            table=_fail["table"] or None,
                            sheet=_fail["sheet"] or None,
                            suggestion=_fail["suggestion"],
                            is_hard=False))
                        continue
                    # §中危 8 修复：把 Step2 校验遗留的 intent.failures（soft tips）
                    # transfer 到 sub_res.failures，让 all_failures 聚合 + Step4 汇总
                    # 上报（对齐 legacy 6 步 partition 的 intent.failures transfer，
                    # agent.py:_run_single 不做此 transfer，否则 V2 下 Step2 校验
                    # 产出的 validation_tip 软失败全丢失，Step4 汇总看不到校验问题）。
                    _intent_fails = getattr(it, "failures", None)
                    if _intent_fails:
                        try:
                            sub_res.failures.extend(_intent_fails)
                        except Exception:
                            logger.debug("Step3 intent.failures transfer 失败", exc_info=True)
                    for f in (_intent_fails or []):
                        if isinstance(f, dict):
                            _record_failure(
                                f, default_type="validation_tip",
                                default_message="Validation warning")
                    all_steps.extend(sub_res.steps or [])
                    if sub_res.result_rows:
                        all_result_rows.extend(sub_res.result_rows)
                    _is_partial = bool(getattr(sub_res, "partial", False))
                    _subtask_ok = False if _is_partial else sub_res.ok
                    sub_tasks.append({
                        # §低危修复：对齐 legacy 多任务路径 sub_tasks 形状（index/
                        # needs_user_fill/partial），前端分段渲染依赖。
                        "index": i + 1,
                        "intent_action": it.action,
                        "ok": _subtask_ok,
                        "needs_confirm": getattr(sub_res, "needs_confirm", False),
                        "message": sub_res.message,
                        "steps": sub_res.steps or [],
                        "result_rows": sub_res.result_rows or [],
                        "table_stem": sub_res.table_stem,
                        "table_sheet": sub_res.table_sheet,
                        "needs_user_fill": list(getattr(sub_res, "needs_user_fill", [])),
                        "partial": _is_partial,
                    })
                    if getattr(sub_res, "partial", False):
                        failed_steps = [
                            s for s in (getattr(sub_res, "steps", None) or [])
                            if isinstance(s, dict) and s.get("ok") is False
                        ]
                        detail = "; ".join(
                            str(s.get("message") or s.get("reason") or s.get("error") or "")[:120]
                            for s in failed_steps[:3]
                        )
                        _record_failure({
                            "type": "partial_write",
                            "table": getattr(sub_res, "table_stem", "") or getattr(it, "table_hint", "") or "",
                            "sheet": getattr(sub_res, "table_sheet", "") or getattr(it, "sheet_hint", "") or "",
                            "col": "",
                            "root_cause": (
                                "Subtask wrote a partial row; some fields were skipped"
                                + (f": {detail}" if detail else "")
                            ),
                            "attempted_strategies": "execute_partial",
                            "suggestion": "Review skipped fields before treating the task as complete",
                            "status": "warning",
                            "user_reply": None,
                        }, default_type="partial_write",
                            default_message="Subtask wrote a partial row")
                    # §低危修复：needs_confirm（行未命中跨表搜索暂停）是"待确认"语义，
                    # 非"失败"。原 `if not sub_res.ok` 把 ok=None（needs_confirm 默认态）
                    # 当 False 走失败分支 → metrics subtasks_fail 误计 + failures 重复
                    # 收集。改为：needs_confirm 单独透传为软失败（供前端渲染选择按钮，
                    # 不阻断），仅 ok is False 才进真失败分支。
                    # §确认链路修复：原仅 pending_search 非空时才记录 failure → 级联删除/
                    # 列删除/反模式 confirm（confirm_token 有但 pending_search 空）的
                    # needs_confirm 信号被丢弃，run_v2 从 s3.failures 找不到 confirm_token
                    # → _stream_res.needs_confirm 不回填 → 确认链路断裂。现统一记录所有
                    # needs_confirm 类型，run_v2 聚合时按 confirm_token 回填顶层字段。
                    if getattr(sub_res, "needs_confirm", False):
                        _ps = getattr(sub_res, "pending_search", None) or {}
                        _ctk = getattr(sub_res, "confirm_token", "") or ""
                        _ckind = getattr(sub_res, "confirm_kind", "") or ""
                        if _ps:
                            _rtype = "row_not_found_needs_confirm"
                            _rc = (f"在 {_ps.get('table_stem','')}/{_ps.get('sheet','')} "
                                   f"未找到「{_ps.get('value','')}」"
                                   f"（定位列={_ps.get('col_name','')}）")
                            _strat = "跨表搜索暂停待用户确认"
                            _sug = "点选下方相近项或手动输入正确值"
                            _tbl = _ps.get("table_stem", "")
                            _sht = _ps.get("sheet", "")
                            _col = _ps.get("col_name", "")
                        else:
                            _rtype = _ckind or "needs_confirm"
                            _rc = (getattr(sub_res, "message", "") or
                                   "操作需用户确认后执行")
                            _strat = "危险操作预览暂停待用户确认"
                            _sug = "确认后执行已预览的操作"
                            _tbl = getattr(sub_res, "table_stem", "") or ""
                            _sht = getattr(sub_res, "table_sheet", "") or ""
                            _col = ""
                        if _ctk:
                            _cf = {
                                "type": _rtype,
                                "table": _tbl, "sheet": _sht, "col": _col,
                                "root_cause": _rc,
                                "attempted_strategies": _strat,
                                "suggestion": _sug,
                                "pending_search": _ps or None,
                                "confirm_token": _ctk,
                                "confirm_kind": _ckind,
                                # 行歧义删除候选行（含 summary，供前端渲染候选卡片）：
                                # _fill_row_evidence 已填 sub_res.row_evidence.alternatives。
                                # 透传到 failure dict 让 run_v2 回填 _stream_res.row_evidence，
                                # _finalize_crud 再映射为 row_alternatives 给前端。
                                "row_evidence": getattr(sub_res, "row_evidence", None),
                                "status": "pending",
                            }
                            all_failures.append(_cf)
                            # 软失败（is_hard=False）：不阻断本步但供前端展示选择按钮
                            errors.append(StepError(
                                step_id=STEP3_EXECUTE,
                                error_type=_rtype,
                                message=_rc,
                                table=_tbl or None,
                                sheet=_sht or None,
                                column=_col or None,
                                suggestion=_sug,
                                is_hard=False))
                    elif sub_res.ok is False:
                        for f in (getattr(sub_res, "failures", None) or []):
                            if isinstance(f, dict):
                                all_failures.append(f)
                                errors.append(StepError(
                                    step_id=STEP3_EXECUTE,
                                    error_type=f.get("type", "execute_fail"),
                                    message=f.get("root_cause") or "执行失败",
                                    table=f.get("table"),
                                    sheet=f.get("sheet"), column=f.get("col"),
                                    suggestion=f.get("suggestion"),
                                    is_hard=False))
                except Exception as e:  # noqa: BLE001
                    logger.warning("Step3 子任务执行异常", exc_info=True)
                    _rc = f"{type(e).__name__}: {e}"
                    try:
                        # thinking_sink 从 StepContext 取（替代探 agent._agent_thinking_sink 私态）
                        sk = ctx.thinking_sink
                        if sk:
                            sk("执行", f"Step3 子任务异常({it.action}): {_rc[:120]}")
                    except Exception:
                        pass
                    # §中危 5 修复：子任务异常补写 all_failures + sub_tasks（原只加通用
                    # StepError，具体表/列/根因丢失 + 汇总漏计）。对齐 legacy 6 步路径
                    # 的 dispatch_exception failure 形状。
                    _fail = {
                        "type": "dispatch_exception",
                        "table": getattr(it, "table_hint", "") or "",
                        "sheet": getattr(it, "sheet_hint", "") or "",
                        "col": "",
                        "root_cause": _rc,
                        "attempted_strategies": "",
                        "suggestion": "",
                        "status": "failed",
                        "user_reply": None,
                    }
                    all_failures.append(_fail)
                    sub_tasks.append({
                        "intent_action": it.action,
                        "ok": False,
                        "message": f"子任务执行异常：{_rc[:120]}",
                        "steps": [],
                        "result_rows": [],
                        "table_stem": getattr(it, "table_hint", "") or "",
                        "table_sheet": getattr(it, "sheet_hint", "") or "",
                    })
                    errors.append(StepError(
                        step_id=STEP3_EXECUTE, error_type="execute_internal",
                        message=f"子任务执行异常：{it.action}",
                        root_cause=_rc, is_hard=False))
        finally:
            pass  # no_llm 作用域在 _run_single 内部 finally 还原，无需外层清理

        # §中危 7 修复：所有子任务执行失败（无成功 + 无 needs_confirm 待定）→ hard error。
        # 原 Step3 全 soft，hard 语义形同空设（仅 Step1 parse_empty hard）。
        # 全失败时 Step3 无可执行结果，后续无意义；但 orchestrator _final 仍跑 Step4 汇总。
        # §低危修复：needs_confirm（ok=None 待确认）不计失败——全失败判断排除待确认项，
        # 否则单任务 needs_confirm 会被当全失败报 hard error。
        _has_ok = any(s.get("ok") for s in sub_tasks)
        _has_pending = any(s.get("needs_confirm") for s in sub_tasks)
        if sub_tasks and not _has_ok and not _has_pending:
            if not any(e.is_hard for e in errors):
                errors.append(StepError(
                    step_id=STEP3_EXECUTE, error_type="all_subtasks_failed",
                    message="全部子任务执行失败",
                    root_cause=f"{len(sub_tasks)} 个子任务均失败",
                    is_hard=True))
        ok = (
            not any(e.is_hard for e in errors)
            and not any(s.get("ok") is False for s in sub_tasks)
        )
        return StepResult(
            step_id=STEP3_EXECUTE, ok=ok,
            errors=errors, warnings=warnings,
            metrics={
                "dur_ms": int((time.time() - t0) * 1000),
                "intents": len(intents),
                "subtasks_ok": sum(1 for s in sub_tasks if s.get("ok")),
                # §低危修复：ok=None（needs_confirm 待确认）不计失败，单独计 pending。
                # 原 `not s.get("ok")` 把 None 当失败，needs_confirm 被误归 fail。
                "subtasks_fail": sum(1 for s in sub_tasks if s.get("ok") is False),
                "subtasks_pending": sum(1 for s in sub_tasks if s.get("needs_confirm")
                                         and s.get("ok") is None),
                "subtasks_partial": sum(1 for s in sub_tasks if s.get("partial")),
                # 本步 LLM 调用数（差值法，替代原 peek_total 累计值含 Step1/Step2）。
                # 注：no_llm 短路后实际应为 0；差值法保真，即使短路未完全覆盖也报真实值。
                "llm_calls": max(0, (self._services.peek_llm_total()
                                     if self._services is not None else 0) - _cnt_before),
            },
            artifacts={
                # 收敛冗余键：原 steps/all_steps、results/all_result_rows 同源，删冗余。
                "subtasks": sub_tasks, "results": all_result_rows,
                "steps": all_steps, "failures": all_failures,
            })


__all__ = ["Step3ExecuteSubAgent"]

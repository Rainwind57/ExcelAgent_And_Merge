"""4-Step V2 Step4 Conclude SubAgent（§设计 S3）。

职责（严格限定）：
  - 汇总 results/failures → 面向用户的自然语言总结
  - 结构化失败清单（#40）
  - skill_updater 反模式归纳（pending_review → active 经门控）

严禁：
  - 执行/写入（属 Step3）
  - 校验（属 Step2）
  - 输入分析（属 Step1）

复用 induce_anti_patterns（内联 _phase_conclude 核心逻辑，去掉 type("R",...)
临时对象伪造）+ 简单汇总，不触发 Step3 那种补建写入越界。
错误归属固定 step4_conclude。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any

from .contracts import STEP4_CONCLUDE, StepContext, StepError, StepResult

logger = logging.getLogger(__name__)


def _collect_field_corrections(validated: list) -> list[dict]:
    """§9.6：从 Step2 校验后的 intent 台账（user_resolved_fields）重建列别名纠正对。

    Step2 全字段编辑里，用户把旧列名改成新列名时，_apply_full_field_edit 会记
    两条台账：旧列 {old: 值, new: ""}（删除）+ 新列 {old: "", new: 值}（新增）。
    此处按「同一值从旧列名移到新列名」重建改名对 (query=旧列名, resolved=新列名)，
    供 Step4 沉淀为可审查的列别名候选（走既有 promote 门控，非写死代码）。

    只取 source="user" 的黄金信号（用户明确手改）；source="auto" 是 AI 建议，
    不作别名候选（避免把 LLM 猜测当真理）。
    """
    out: list[dict] = []
    for it in (validated or []):
        if not isinstance(it, dict) and not hasattr(it, "extras"):
            continue
        extras = getattr(it, "extras", None) or {}
        book = extras.get("user_resolved_fields") or {}
        if not isinstance(book, dict):
            continue
        table = getattr(it, "table_hint", "") or ""
        sheet = getattr(it, "sheet_hint", "") or ""
        deletes: dict[str, str] = {}  # 旧列名 -> 被删除列的原值
        adds: dict[str, str] = {}     # 新列名 -> 新增列的值
        for col, rec in book.items():
            if not isinstance(rec, dict):
                continue
            if rec.get("source") != "user":
                continue
            old_col = str(rec.get("old_col", "") or "").strip()
            new_col = str(rec.get("new_col", "") or "").strip()
            if old_col and new_col and old_col != new_col:
                out.append({
                    "table_stem": table, "sheet": sheet,
                    "query": old_col, "resolved": new_col,
                })
                continue
            old = str(rec.get("old", "") or "").strip()
            new = str(rec.get("new", "") or "").strip()
            if old and not new:
                deletes[str(col)] = old
            elif new and not old:
                adds[str(col)] = new
        for dcol, dval in deletes.items():
            for acol, aval in adds.items():
                if dval and dval == aval and dcol != acol:
                    out.append({
                        "table_stem": table, "sheet": sheet,
                        "query": dcol, "resolved": acol,
                    })
                    break
    return out


def _prior_step_failures(ctx: StepContext) -> list[dict]:
    """Convert prior StepError objects into the Step4 failure shape."""
    out: list[dict] = []
    for sid, result in (ctx.results or {}).items():
        if sid == STEP4_CONCLUDE:
            continue
        for err in (getattr(result, "errors", None) or []):
            out.append({
                "type": getattr(err, "error_type", "step_error"),
                "table": getattr(err, "table", "") or "",
                "sheet": getattr(err, "sheet", "") or "",
                "col": getattr(err, "column", "") or "",
                "root_cause": (
                    getattr(err, "root_cause", "") or getattr(err, "message", "")
                    or "Step failed"),
                "attempted_strategies": getattr(err, "step_id", sid),
                "suggestion": getattr(err, "suggestion", "") or "",
                "status": "failed" if getattr(err, "is_hard", False) else "warning",
                "user_reply": None,
            })
    return out


class Step4ConcludeSubAgent:
    """Step4：总结归纳、生成经验。"""

    def __init__(self, services: Any = None):
        # 注入 ExcelAgentServices（替代原 agent=self 散播私态）。
        # services 收口 enable_skill / ai_enhancer 接口。
        self._services = services

    def execute(self, ctx: StepContext) -> StepResult:
        """Step4 执行：聚合 Step3 results/failures → summary + 反模式归纳。

        - 无 LLM 汇总时走模板拼接（成功 X / 失败 Y / 跳过 Z）
        - failures 非空 + skill 开启 → induce_anti_patterns（1 次 LLM，失败降级）
        - 不做执行/补建（杜绝 Step4 越界）
        """
        t0 = time.time()
        errors: list[StepError] = []
        warnings: list[str] = []

        s3 = ctx.get_result("step3_execute")
        subtasks = (s3.artifacts.get("subtasks") if s3 else []) or []
        failures = list((s3.artifacts.get("failures") if s3 else []) or [])
        seen = {
            (f.get("type"), f.get("table"), f.get("sheet"), f.get("col"),
             f.get("root_cause"))
            for f in failures if isinstance(f, dict)
        }
        for f in _prior_step_failures(ctx):
            key = (f.get("type"), f.get("table"), f.get("sheet"), f.get("col"),
                   f.get("root_cause"))
            if key not in seen:
                failures.append(f)
                seen.add(key)

        # Step4 只汇总，但最终 ok 必须反映全部前序 step，而非只镜像 Step3。
        prior = [r for sid, r in ctx.results.items() if sid != STEP4_CONCLUDE]
        all_ok = bool(prior) and all(r.ok for r in prior)
        # §7 修复（通用，不绑业务）：Step1 产空以外的"用户意图被丢/部分漏解析"信号
        # （segment_no_intent / segment_partial_coverage）是 soft error（is_hard=False），
        # 不翻转 Step1.ok，于是 all_ok 恒 True → Step4 仍报"完成 N 个子任务"（假成功：
        # 用户某条意图根本没进 Step2/3，却被当作全成功）。修：把"意图被丢/漏解析"类
        # 前序错误纳入 all_ok 判据（无论 hard/soft），并在文案里显式点出漏解析段数，
        # 让 Step4 不再对不完整链路报干净成功。判据是错误类型（结构信号），非业务词。
        _DROPPED_INTENT_ERROR_TYPES = {
            "segment_no_intent", "segment_partial_coverage",
            "parse_empty", "parse_internal",
        }
        n_dropped = 0
        for _r in prior:
            for _e in (getattr(_r, "errors", None) or []):
                _et = getattr(_e, "error_type", "")
                if _et in _DROPPED_INTENT_ERROR_TYPES or getattr(_e, "is_hard", False):
                    n_dropped += 1
        has_incomplete = n_dropped > 0
        # §低危修复：ok=None（needs_confirm 待确认）不计失败，与 Step3 口径一致。
        # 原 `not s.get("ok")` 把 None 当失败 → n_fail 误计。
        n_ok = sum(1 for s in subtasks if s.get("ok") is True)
        n_fail = sum(1 for s in subtasks if s.get("ok") is False)
        n_skipped = sum(1 for s in subtasks if s.get("skipped"))
        n_pending = sum(1 for s in subtasks if s.get("needs_confirm")
                        and s.get("ok") is None)
        # 最终 ok：前序全 ok + 无 Step3 硬失败 + 无"意图被丢/漏解析"信号。
        all_ok = all_ok and n_fail == 0 and not has_incomplete

        # 汇总文案（模板，无 LLM）
        if all_ok and subtasks:
            summary = f"完成 {n_ok} 个子任务"
            if n_pending:
                summary += f"，{n_pending} 个待确认"
        elif has_incomplete and subtasks:
            # 有子任务执行成功，但存在意图被丢/漏解析：如实报"部分完成"，不报干净成功。
            summary = (f"部分完成：{n_ok} 个子任务已处理，"
                       f"但有 {n_dropped} 处意图未能解析/被丢弃，请检查指令覆盖")
            if n_fail:
                summary += f"；{n_fail} 个失败"
            if n_pending:
                summary += f"；{n_pending} 个待确认"
            for f in failures[:5]:
                loc = f"{f.get('table', '?')}/{f.get('sheet', '?')}"
                col = f" 列[{f.get('col')}]" if f.get("col") else ""
                rc = f.get("root_cause") or "未知"
                summary += f"\n- {loc}{col}：{rc}"
        elif subtasks:
            summary = f"完成 {n_ok}/{len(subtasks)} 个子任务，{n_fail} 个失败"
            if n_skipped:
                summary += f"（其中 {n_skipped} 个因 Step2 未解决被跳过写入）"
            if n_pending:
                summary += f"，{n_pending} 个待确认"
            for f in failures[:5]:
                loc = f"{f.get('table', '?')}/{f.get('sheet', '?')}"
                col = f" 列[{f.get('col')}]" if f.get("col") else ""
                rc = f.get("root_cause") or "未知"
                summary += f"\n- {loc}{col}：{rc}"
        elif failures:
            summary = ctx.folded_message()
            for f in failures[:5]:
                loc = f"{f.get('table') or '?'}/{f.get('sheet') or '?'}"
                col = f" [{f.get('col')}]" if f.get("col") else ""
                rc = f.get("root_cause") or "unknown"
                summary += f"\n- {loc}{col}: {rc}"
        else:
            summary = ctx.folded_message()

        # 反模式归纳（用共享 helper，替代原内联 _phase_conclude 核心逻辑）。
        # _collect_failed_traces + _induce_anti_patterns_via 在 TableAgent 上，
        # V2 Step4 与 legacy _phase_conclude 共用，消除双份漂移。
        # §低危修复：induced_count 原硬编码 len(failures)（失败条数，与"归纳产出
        # 几条候选"语义无关——即便 induce 未开启/异常/产出 0 条，也照样报
        # induced_count=len(failures)，前端读到的"学习产出数"是假数据）。
        # 现用 _induce_anti_patterns_via 的真实返回值 n（默认 0，异常/未触发保持 0）。
        induced_count = 0
        if failures and self._services is not None:
            try:
                if self._services.enable_skill:
                    _enh = self._services.ai_enhancer
                    if _enh is not None:
                        from ..agent import TableAgent
                        traces = TableAgent._collect_failed_traces(failures)
                        induced_count = TableAgent._induce_anti_patterns_via(traces, _enh) or 0
                        if induced_count:
                            warnings.append(
                                f"反模式归纳产出 {induced_count} 条候选"
                                f"（pending_review，待 promote）")
            except Exception as e:  # noqa: BLE001
                logger.warning("Step4 反模式归纳失败（降级）", exc_info=True)
                warnings.append(f"反模式归纳失败：{e}")

        # §9.6 修复经验沉淀：把 Step2 用户手动字段修正（user_resolved_fields
        # 台账里的改名对）沉淀为可审查的列别名候选，走 skill_updater 既有
        # promote_with_guard 门控（快照→回归→回滚/隔离），不写死代码。
        alias_ingested = 0
        try:
            s2 = ctx.get_result("step2_validate")
            validated = (s2.artifacts.get("validated") if s2 else None) or []
            _corrections = _collect_field_corrections(validated)
            if _corrections:
                from ..skill_updater import get_skill_updater
                alias_ingested = get_skill_updater().ingest_field_corrections(_corrections)
                if alias_ingested:
                    warnings.append(
                        f"沉淀 {alias_ingested} 条用户字段修正候选"
                        f"（column_alias_candidates，待 promote 门控）")
        except Exception as e:  # noqa: BLE001
            logger.warning("Step4 修复经验沉淀失败（降级）", exc_info=True)

        return StepResult(
            step_id=STEP4_CONCLUDE, ok=all_ok,
            errors=errors, warnings=warnings,
            metrics={
                "dur_ms": int((time.time() - t0) * 1000),
                "subtasks_ok": n_ok, "subtasks_fail": n_fail,
                "subtasks_pending": n_pending,
                "subtasks_skipped": n_skipped,
                "failures": len(failures),
                "alias_ingested": alias_ingested,
            },
            artifacts={
                "summary": summary, "failures": failures,
                "induced_count": induced_count,
            })


__all__ = ["Step4ConcludeSubAgent"]

"""4-Step V2 流水线 Orchestrator（§设计 S1）。

替代旧 run() 神函数（agent.py:4248-5277, 6000+ 行交织兜底）。

职责：
  - 4 步固定循环，每步恰好一次 stage_start/stage_end。
  - hard error 硬停本步 + 后续不跑，但**仍走 Step4 做汇总聚合**（§设计 _final）。
  - 兜底是各 SubAgent 内部细节，对上层不可见。
  - 全事件带 step_id，前端按 step_id 路由。

过渡期：Step2/3/4 SubAgent 是 legacy 私有方法的簿记包装（见 contracts.py docstring）。
开关：CODEMAKER_EXCEL_PIPELINE_V2 默认 ON 走本 orchestrator；=0 显式降级到旧 run() 6 步。

SSE 输出形态：本类为 generator，yield SSE 事件 dict，由 agent_service 转 SSE 流。
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Iterator, Optional

from .contracts import (
    SSE, STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE,
    STEP_ORDER, STEP_TITLES, StepContext, StepError, StepHardError, StepResult,
)

logger = logging.getLogger(__name__)


class ExcelAgentPipeline:
    """4-Step 固定流水线。run() 从千行神函数收缩为 ~80 行循环。"""

    def __init__(self, step1=None, step2=None, step3=None, step4=None,
                 legacy_agent: Any = None):
        # V2 四 step 由 run_v2 实例化时全部注入（agent.py:4362-4365），均非空。
        # 原 _run_legacy_step 兜底（step 为 None 时）已删——迁移完成，无死代码。
        self._step1 = step1
        self._step2 = step2
        self._step3 = step3
        self._step4 = step4
        self._legacy_agent = legacy_agent

    def run(self, ctx: StepContext) -> Iterator[dict]:
        """主循环。yield SSE 事件。

        流程：
          for step in [Step1, Step2, Step3, Step4]:
              yield stage_start; result = step.execute(ctx)
              ctx.set_result(step.id, result); yield stage_end(+errors[])
              hard → 仍走 _final 做汇总聚合后 return
          yield done(ctx.folded())
        """
        total = len(STEP_ORDER)
        try:
            for step_id in STEP_ORDER:
                step = self._get_step(step_id)
                yield SSE.stage_start(step_id, total=total)
                t0 = time.time()
                result: Optional[StepResult] = None
                try:
                    result = step.execute(ctx)
                except StepHardError as e:
                    result = StepResult(
                        step_id=e.step_id, ok=False,
                        errors=[e.to_error()],
                        metrics={"dur_ms": int((time.time() - t0) * 1000)})
                    ctx.set_result(e.step_id, result)
                    yield SSE.stage_end(e.step_id, result)
                    yield from self._final(ctx)
                    return
                except Exception as e:  # noqa: BLE001
                    # 兜底：任何未声明硬错误都转 soft StepError，不吞
                    logger.warning("Step %s 未捕获异常,转 soft error", step_id,
                                   exc_info=True)
                    result = StepResult(
                        step_id=step_id, ok=False,
                        errors=[StepError(
                            step_id=step_id, error_type="internal",
                            message=f"{step_id} 内部错误",
                            root_cause=f"{type(e).__name__}: {e}",
                            is_hard=False)],
                        metrics={"dur_ms": int((time.time() - t0) * 1000)})
                # None 兜底：step.execute 返回 None（理论不发生，但防御 AttributeError）
                if result is None:
                    logger.warning("Step %s execute 返回 None,转 soft error", step_id)
                    result = StepResult(
                        step_id=step_id, ok=False,
                        errors=[StepError(
                            step_id=step_id, error_type="execute_none",
                            message=f"{step_id} 未产出结果",
                            is_hard=False)],
                        metrics={"dur_ms": int((time.time() - t0) * 1000)})
                if result.metrics.get("dur_ms") is None:
                    result.metrics["dur_ms"] = int((time.time() - t0) * 1000)
                ctx.set_result(step_id, result)
                yield SSE.stage_end(step_id, result)
                if result.has_hard_error():
                    yield from self._final(ctx)
                    return
            yield from self._final(ctx)
        except Exception:  # noqa: BLE001
            logger.warning("ExcelAgentPipeline 顶层异常", exc_info=True)
            yield from self._final(ctx)

    def _get_step(self, step_id: str):
        return {STEP1_PARSE: self._step1, STEP2_VALIDATE: self._step2,
                STEP3_EXECUTE: self._step3, STEP4_CONCLUDE: self._step4}.get(step_id)

    def _final(self, ctx: StepContext) -> Iterator[dict]:
        """统一出口：仍跑 Step4 汇总聚合（即使中途硬停）。

        Step4 已由 run_v2 注入（非空），硬停路径会走到此补跑 Step4。
        """
        if self._step4 is not None and STEP4_CONCLUDE not in ctx.results:
            t0 = time.time()
            try:
                r4 = self._step4.execute(ctx)
            except Exception as e:  # noqa: BLE001
                logger.warning("Step4 汇总异常", exc_info=True)
                r4 = StepResult(
                    step_id=STEP4_CONCLUDE, ok=False,
                    errors=[StepError(
                        step_id=STEP4_CONCLUDE, error_type="conclude_fail",
                        message="汇总失败",
                        root_cause=f"{type(e).__name__}: {e}", is_hard=False)],
                    metrics={"dur_ms": int((time.time() - t0) * 1000)})
            ctx.set_result(STEP4_CONCLUDE, r4)
            yield SSE.stage_end(STEP4_CONCLUDE, r4)
        yield SSE.done(ctx)


def is_v2_enabled() -> bool:
    """统一单一开关，默认 ON。=0 显式降级到旧 run() 6 步路径。"""
    return os.getenv("CODEMAKER_EXCEL_PIPELINE_V2", "1") != "0"


__all__ = ["ExcelAgentPipeline", "is_v2_enabled"]

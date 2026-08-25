"""4-Step V2 流水线契约（§设计 S1）。

过渡期真实状态（V2 复用 legacy SubAgent 实现）：
  - Step2/3/4 的 SubAgent 通过 agent=self 调用旧 _step2_validate_intents /
    _run_single / induce_anti_patterns，是 legacy 私有方法的簿记包装，
    非真正独立服务对象。S3 阶段提取为独立服务注入后，下方"硬隔离"才完全成立。
  - Step3 零 LLM 通过 no_llm 参数透传到 _run_single（临时设实例属性，
    try/finally 还原），替代原 os.environ 进程级突变。
  - Step2 过渡期读 _locator_agent._last_locator_result 私态（Step1 副作用），
    S3 改为 Step1 显式产出到 s1.artifacts。

步间隔离约定（过渡期尽力保证，非绝对）：
  - 每步只对自己的错误负责：错误以 StepError 在本步 StepResult.errors[] 落地，
    绝不吞、不改道到别步。
  - hard error = 本步硬停，后续步不跑（由 Orchestrator 统一规约）。
  - StepContext 步间只追加不回退；前一态不被后步改写（Step2 原写回 s1.artifacts
    已删除，validated 只存 s2.artifacts）。
  - 一个事件流一个 stage 名单：每事件带 step_id，前端按 step_id 路由，废弃字符串前缀猜测。

与其它编排层的关系：
  - CODEMAKER_EXCEL_PIPELINE_V2=1（默认 ON）走本契约 + orchestrator。
  - =0 走旧 run() 6 步 _phase_* 路径（降级通道，用 s1_parse 阶段命名）。
  - pipeline/（7 步管道）是独立子系统（关键词触发），用 pipeline/types.StepResult，
    与本 contracts.StepResult 语义不同，不合并。
  - 7 步管道复用 subagent/dispatcher、subagent/roles、core/checkpoint、
    core/operation_orchestrator、pipeline/verifier（原 engine_core 双骨架层已合并）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ── step_id 常量（唯一、稳定，前端按此路由） ──────────────────
STEP1_PARSE = "step1_parse"
STEP2_VALIDATE = "step2_validate"
STEP3_EXECUTE = "step3_execute"
STEP4_CONCLUDE = "step4_conclude"

STEP_ORDER: list[str] = [STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE]

STEP_TITLES: dict[str, str] = {
    STEP1_PARSE: "Step1 解析",
    STEP2_VALIDATE: "Step2 校验",
    STEP3_EXECUTE: "Step3 执行",
    STEP4_CONCLUDE: "Step4 汇总",
}


@dataclass
class StepError:
    """错误归属固定在本步。绝不跨步冒泡。

    Attributes:
        step_id: 必填，错误归属的 step（与抛错步一致）。
        error_type: 复用 error_classifier.ErrorType 枚举字符串。
        message: 用户友好文案（前端直出）。
        root_cause: 技术根因（日志/调试用）。
        is_hard: hard=硬停本步+后续；soft=警告继续。
        segment_idx: 回溯到 Step1 段，便于定位漏哪条（None 表示非段级）。
    """
    step_id: str
    error_type: str
    message: str
    root_cause: str = ""
    table: Optional[str] = None
    sheet: Optional[str] = None
    column: Optional[str] = None
    suggestion: Optional[str] = None
    is_hard: bool = False
    segment_idx: Optional[int] = None

    def to_event(self) -> dict:
        """序列化为 SSE event payload。"""
        d = {
            "step_id": self.step_id,
            "error_type": self.error_type,
            "message": self.message,
            "is_hard": self.is_hard,
        }
        if self.table:
            d["table"] = self.table
        if self.sheet:
            d["sheet"] = self.sheet
        if self.column:
            d["column"] = self.column
        if self.suggestion:
            d["suggestion"] = self.suggestion
        if self.segment_idx is not None:
            d["segment_idx"] = self.segment_idx
        if self.root_cause:
            d["root_cause"] = self.root_cause
        return d


@dataclass
class StepResult:
    """每步统一出口。artifacts 按 step 存放各自产物。

    ok=False 时 errors 必非空；ok=True 时 errors 可为空或仅 soft。
    """
    step_id: str
    ok: bool
    errors: list[StepError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    metrics: dict = field(default_factory=dict)
    artifacts: dict = field(default_factory=dict)

    def has_hard_error(self) -> bool:
        return any(e.is_hard for e in self.errors)

    def to_event(self) -> dict:
        return {
            "step_id": self.step_id,
            "ok": self.ok,
            "errors": [e.to_event() for e in self.errors],
            "warnings": list(self.warnings),
            "metrics": dict(self.metrics),
        }


class StepHardError(Exception):
    """本步不可继续的硬错误。Orchestrator 捕获后硬停 + 仍走 Step4 汇总。

    不同于普通 Exception：普通异常由 SubAgent 内部 except 转为 StepError(soft/hard)，
    StepHardError 是 SubAgent 主动声明"本步无法继续"的唯一外部可见信号。
    """
    def __init__(self, step_id: str, error_type: str, message: str,
                 root_cause: str = "", **kwargs):
        super().__init__(message)
        self.step_id = step_id
        self.error_type = error_type
        self.message = message
        self.root_cause = root_cause
        self.kwargs = kwargs

    def to_error(self) -> StepError:
        return StepError(
            step_id=self.step_id,
            error_type=self.error_type,
            message=self.message,
            root_cause=self.root_cause,
            table=self.kwargs.get("table"),
            sheet=self.kwargs.get("sheet"),
            column=self.kwargs.get("column"),
            suggestion=self.kwargs.get("suggestion"),
            is_hard=True,
            segment_idx=self.kwargs.get("segment_idx"),
        )


@dataclass
class StepContext:
    """步间不可变追加上下文。

    user_text 永不变；segments 由 Step1 产出后只读；results 步间只追加。
    schema_cache 全流程共享，避免重复拉表头（替代旧 _suggest_cache 散落）。
    """
    session_id: str
    user_text: str
    segments: list = field(default_factory=list)
    results: dict[str, StepResult] = field(default_factory=dict)
    schema_cache: Any = None
    # 旧 agent 句柄（过渡期 V2 复用旧 SubAgent 实现，S3 完全剥离后删）
    legacy_agent: Any = None
    thinking_sink: Any = None
    cancel_event: Any = None
    checkpoint_id: Optional[str] = None

    def set_result(self, step_id: str, result: StepResult) -> None:
        self.results[step_id] = result

    def get_result(self, step_id: str) -> Optional[StepResult]:
        return self.results.get(step_id)

    def all_ok(self) -> bool:
        return all(r.ok for r in self.results.values())

    def folded_message(self) -> str:
        """折叠为面向用户的最终一句话。"""
        n_ok = sum(1 for r in self.results.values() if r.ok)
        n_total = len(STEP_ORDER)
        if self.all_ok():
            return f"全部 {n_total} 步完成"
        failed = [STEP_TITLES.get(sid, sid) for sid, r in self.results.items() if not r.ok]
        return f"{n_ok}/{n_total} 步完成，{ '、'.join(failed) } 未通过"


# ── SSE 事件构造（单一、带 step_id） ─────────────────────────────
class SSE:
    """SSE 事件工厂。所有事件带 step_id，前端按此路由到 StepCard。"""

    @staticmethod
    def stage_start(step_id: str, total: int = 4, **extra) -> dict:
        ev = {"type": "stage_start", "step_id": step_id,
              "title": STEP_TITLES.get(step_id, step_id), "total": total}
        ev.update(extra)
        return ev

    @staticmethod
    def progress(step_id: str, kind: str, **payload) -> dict:
        """kind: thinking | step | tool | llm_token。全部带 step_id。"""
        ev = {"type": "progress", "step_id": step_id, "kind": kind}
        ev.update(payload)
        return ev

    @staticmethod
    def subtask(step_id: str, idx: int, total: int, **payload) -> dict:
        """仅 Step3 发。"""
        ev = {"type": "subtask", "step_id": step_id, "idx": idx, "total": total}
        ev.update(payload)
        return ev

    @staticmethod
    def ask(step_id: str, reason: str, **payload) -> dict:
        """交互卡，限定到步。"""
        ev = {"type": "ask", "step_id": step_id, "reason": reason}
        ev.update(payload)
        return ev

    @staticmethod
    def stage_end(step_id: str, result: StepResult, **extra) -> dict:
        ev = {"type": "stage_end", "step_id": step_id}
        ev.update(result.to_event())
        ev.update(extra)
        return ev

    @staticmethod
    def done(ctx: StepContext, **extra) -> dict:
        ev = {"type": "done", "ok": ctx.all_ok(),
              "message": ctx.folded_message(),
              "steps": [r.to_event() for r in ctx.results.values()],
              "checkpoint_id": ctx.checkpoint_id}
        ev.update(extra)
        return ev


__all__ = [
    "STEP1_PARSE", "STEP2_VALIDATE", "STEP3_EXECUTE", "STEP4_CONCLUDE",
    "STEP_ORDER", "STEP_TITLES",
    "StepError", "StepResult", "StepHardError", "StepContext", "SSE",
]

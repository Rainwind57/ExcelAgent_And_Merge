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
STEP1_5_CONTRACT = "step1_5_contract"
STEP2_VALIDATE = "step2_validate"
STEP3_EXECUTE = "step3_execute"
STEP4_CONCLUDE = "step4_conclude"

STEP_ORDER: list[str] = [
    STEP1_PARSE, STEP1_5_CONTRACT, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE,
]

STEP_TITLES: dict[str, str] = {
    STEP1_PARSE: "Step1 解析",
    STEP1_5_CONTRACT: "Step1.5 契约校验",
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
    # 二次确认令牌（V2 透传）：run_v2 入口接收，Step3 用于 run_single 短路
    # __delete_confirmed__/__anti_pattern_confirmed__ 等标记，恢复 V2 下确认链路。
    confirm_token: Optional[str] = None
    # ── 主线1 通用 resolution 台账（跨 Step 结构化决策账本）──
    # issue_id(内容派生,稳定) → resolution。让所有 ask/校验结论只发生一次、
    # 跨 Step/跨 deepcopy 稳定。以 dict 形态持久（随 ctx 携带），懒初始化为
    # ResolutionLedger（见 resolution_ledger.py）。默认空 dict，不影响既有流程。
    resolution_ledger: dict = field(default_factory=dict)

    def get_ledger(self):
        """返回 ResolutionLedger 视图（从 resolution_ledger dict 构造，回写同步）。

        懒加载：首次访问从持久 dict 复原；调用方 record 后需 sync_ledger 回写，
        或直接用返回对象操作后调 sync_ledger。为简化，这里返回持有引用的对象，
        其内部 _items 与本 ctx.resolution_ledger 通过 sync_ledger 同步。
        """
        from .resolution_ledger import ResolutionLedger
        return ResolutionLedger.from_dict(self.resolution_ledger)

    def sync_ledger(self, ledger) -> None:
        """把 ResolutionLedger 回写到持久 dict（跨 Step 携带）。"""
        try:
            self.resolution_ledger = ledger.to_dict()
        except Exception:
            pass

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


@dataclass
class StepCard:
    """文件解析产出的单步卡片。

    Attributes:
        step_id: 步骤编号(如 "1.1")
        title: 步骤标题
        content: 步骤内容(非对话部分,如旁白/交互描述)
        involved_elements: 涉及的实体列表,每项 {"type","symbol","name"}
        branches: 分支信息(如选项跳转),无分支为空
        dialog_fragments: 对话片段列表,每项 {"speaker","text","symbol"}
    """
    step_id: str = ""
    title: str = ""
    content: str = ""
    involved_elements: list[dict] = field(default_factory=list)
    branches: list[dict] = field(default_factory=list)
    dialog_fragments: list[dict] = field(default_factory=list)


@dataclass
class DocIntent:
    """文件解析产出的结构化意图。

    Attributes:
        source_path: 源文件路径
        file_type: 文件类型(md/xlsx/csv/txt)
        steps: 步骤卡片列表(md 才有,xlsx/csv 为空)
        records: 行记录列表(xlsx/csv 才有,每行一 dict)
        symbol_map: 符号映射表 {"<placeholder>": "原始名"}
        raw_text: 原始文本(txt 降级用)
        ok: 解析是否成功
        error: 失败时的错误信息
    """
    source_path: str = ""
    file_type: str = ""
    steps: list[StepCard] = field(default_factory=list)
    records: list[dict] = field(default_factory=list)
    symbol_map: dict[str, str] = field(default_factory=dict)
    raw_text: str = ""
    ok: bool = True
    error: str = ""

    def add_symbol(self, placeholder: str, name: str) -> None:
        """分配符号到映射表,重复名复用已有符号。"""
        # 反查:已有同名则复用
        for ph, nm in self.symbol_map.items():
            if nm == name:
                return
        self.symbol_map[placeholder] = name


@dataclass
class AgentFragment:
    """SubAgent 产出单元:含 placeholder 声明 + SQL片段/操作列表 + thinking。

    Attributes:
        agent_name: 产出该 fragment 的 SubAgent 名(如 "Dialog配表专家")
        produces: 该 fragment 声明产出的 placeholder 符号(如 "<npc_laochen>")
        references: 该 fragment 引用的其他 placeholder 列表
        sql_or_ops: 生成的 SQL 片段或操作列表
        thinking_steps: 思考步骤,每项 {"phase","detail"}
        ok: 是否成功
        error: 失败详情
        target_table: 目标表 stem(供 Step4 汇总排序)
        target_sheet: 目标 sheet
    """
    agent_name: str = ""
    produces: Optional[str] = None
    references: list[str] = field(default_factory=list)
    sql_or_ops: list[Any] = field(default_factory=list)
    thinking_steps: list[dict] = field(default_factory=list)
    ok: bool = True
    error: str = ""
    target_table: str = ""
    target_sheet: str = ""


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
    "STEP1_PARSE", "STEP1_5_CONTRACT", "STEP2_VALIDATE", "STEP3_EXECUTE", "STEP4_CONCLUDE",
    "STEP_ORDER", "STEP_TITLES",
    "StepError", "StepResult", "StepHardError", "StepContext", "SSE",
    "StepCard", "DocIntent", "AgentFragment",
]

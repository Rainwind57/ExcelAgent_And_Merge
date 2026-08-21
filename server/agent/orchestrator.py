"""OrchestratorAgent：基于 LangGraph 的主智能体（聊天对话 + 调度子智能体）。

架构（LangGraph，见 graph.py）：
  START → classify(LLM 意图分类) ─┬─ qa   → QAHandler.answer ─→ END
                                  └─ crud → TableAgent.run    ─→ END
  dry_run 模式下 crud 节点跳过执行（不写盘），供预览分诊 classify 复用。

对外契约保持不变：chat/classify 返回 RouteResult，签名与旧版一致，
AgentService / FastAPI 路由层无需改动。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .excel.agent import AgentResult, TableAgent
from .codemaker_client import CodemakerClient
from .graph import build_agent_graph
from .qa_handler import QAHandler


@dataclass
class RouteResult:
    """路由结果。

    Attributes:
        intent: qa 或 crud
        message: 简短概括
        qa_answer: QA 回答（仅 qa 意图）
        crud_result: CRUD 执行结果（仅 crud 意图；classify 预览模式下留空）
    """
    intent: str = "qa"
    message: str = ""
    qa_answer: str = ""
    crud_result: Optional[AgentResult] = None


class OrchestratorAgent:
    """主智能体：LangGraph 编排意图分类与子智能体调度。

    用法：
        orch = OrchestratorAgent(table_agent, resources_dir)
        result = orch.chat("增加建筑名称为瞭望塔，赋值它的id是99999", session_id="s1")
    """

    def __init__(self, table_agent: TableAgent, resources_dir: Path,
                 client: CodemakerClient | None = None):
        self.table_agent = table_agent
        self.resources_dir = resources_dir
        self.client = client or CodemakerClient()
        self.qa_handler = QAHandler(client=self.client, resources_dir=resources_dir)
        # 复用同一 qa_handler 实例，使 chat_stream 注入的 sink 能透传到 graph 内 qa 节点；
        # classify_think 传绑定方法，分类决策后立即推送「意图分类完成」
        self.graph = build_agent_graph(table_agent, resources_dir, self.client,
                                       qa_handler=self.qa_handler,
                                       classify_think=self._think)
        # 流式思考回调（chat_stream 注入；非流式调用时为 None，静默降级）
        self._thinking_sink = None
        self._step_sink = None

    def _think(self, phase: str, detail: str = "") -> None:
        """推送 thinking 事件（sink 未注入时静默跳过）。"""
        sink = self._thinking_sink
        if sink:
            try:
                sink(phase, detail)
            except Exception:
                pass

    def _step(self, name: str, ok: bool = True, detail: str = "") -> None:
        """推送 step 事件（sink 未注入时静默跳过）。"""
        sink = self._step_sink
        if sink:
            try:
                sink({"name": name, "ok": ok, "detail": detail})
            except Exception:
                pass

    def _invoke(self, text: str, session_id: str, context: str,
                dry_run: bool) -> RouteResult:
        """统一入口：跑 graph，把终态映射回 RouteResult。

        codemaker 不可用或分类失败时，graph.invoke 抛 RuntimeError，
        透传给上层（与旧版 _ensure_cm_session fail-fast 行为一致）。
        """
        if not text or not text.strip():
            return RouteResult(intent="qa", message="空输入",
                               qa_answer="请输入您的问题或操作指令。")

        self._think("意图分类", "规则短路优先，未命中则 LLM 判定 qa / crud")
        state = self.graph.invoke({
            "text": text,
            "session_id": session_id,
            "context": context or "",
            "dry_run": dry_run,
        })

        intent = state.get("intent", "qa")
        summary = state.get("summary", text)
        # 「意图分类完成」已移入 classify 节点内推送（早于 CRUD 步骤，归入 s1_decompose 阶段）
        if intent == "qa":
            return RouteResult(intent="qa", message=summary,
                               qa_answer=state.get("qa_answer", ""))
        return RouteResult(intent="crud", message=summary,
                           crud_result=state.get("crud_result"))

    def chat(self, text: str, session_id: str = "default",
             context: str = "") -> RouteResult:
        """主入口：分类意图 → 调度 QA 或 CRUD 子智能体（真实写盘）。"""
        return self._invoke(text, session_id, context, dry_run=False)

    # 兼容旧接口名（route）
    route = chat

    def classify(self, text: str, session_id: str = "default") -> RouteResult:
        """仅做意图分类，不执行 CRUD（dry-run）。

        qa 分支预取答案；crud 分支 crud_result 留空，由调用方自行预览
        （临时副本上执行，不能走真实写盘的 chat）。供 AgentService._dry_run_chat 复用。
        """
        return self._invoke(text, session_id, "", dry_run=True)

"""LangGraph 主智能体状态定义。"""

from __future__ import annotations

from typing import Optional, TypedDict

from .excel.agent import AgentResult


class AgentState(TypedDict, total=False):
    """主智能体图状态。

    classify 节点写入 intent/summary；qa 节点写入 qa_answer；
    crud 节点写入 crud_result。dry_run 控制是否真正写盘。
    """

    text: str
    session_id: str
    context: str
    dry_run: bool
    intent: str
    summary: str
    qa_answer: str
    crud_result: Optional[AgentResult]

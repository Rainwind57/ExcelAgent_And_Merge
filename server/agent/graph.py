"""LangGraph 主智能体：编译 classify → {qa | crud} 图。

  START → classify(LLM 意图分类) ─┬─ qa   → QAHandler.answer ─→ END
                                  └─ crud → TableAgent.run    ─→ END

build_agent_graph 可由 OrchestratorAgent 注入已构造的 TableAgent（FastAPI 路径），
亦可仅传 config 由 _build_engine_from_config 自建引擎（langgraph-cli / Platform 路径）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from langgraph.graph import END, START, StateGraph

from .codemaker_client import CodemakerClient, CodemakerClientConfig
from .configuration import Configuration, get_config
from .excel.agent import TableAgent
from .excel.cli_interface import StubCodeMakerCLI
from .excel.codemaker_parser import CodemakerNLParser
from .excel.real_cli import RealCodeMakerCLI
from .llm import CodemakerChatModel
from .nodes import (
    make_classify_node,
    make_crud_node,
    make_qa_node,
    route_after_classify,
)
from .qa_handler import QAHandler
from .state import AgentState
from .tools import make_crud_tool, make_qa_tool


def _build_engine_from_config(cfg: Configuration):
    """从 Configuration 自建 TableAgent + CodemakerClient。

    供 langgraph-cli / LangGraph Platform 独立运行（无 FastAPI 注入引擎时）。
    与 services.agent_service 的构造逻辑保持一致。
    """
    resources_dir = Path(cfg.directory) if cfg.directory else None
    client = CodemakerClient(CodemakerClientConfig(
        server_url=cfg.codemaker_server_url,
        username=cfg.codemaker_username,
        password=cfg.codemaker_password,
        default_model=cfg.model_name,
    ))
    workspace = resources_dir or Path(cfg.directory)
    try:
        cli = RealCodeMakerCLI(workspace=workspace)
    except Exception:
        cli = StubCodeMakerCLI(workspace=workspace)
    parser = CodemakerNLParser(directory=str(resources_dir) if resources_dir else "")
    table_agent = TableAgent(cli=cli, parser=parser)
    return table_agent, resources_dir, client


def build_agent_graph(table_agent: Optional[TableAgent] = None,
                      resources_dir: Optional[Path] = None,
                      client: Optional[CodemakerClient] = None,
                      config=None, qa_handler: Optional[QAHandler] = None,
                      classify_think=None):
    """构建并编译主智能体 LangGraph。

    Args:
        table_agent: CRUD 子智能体。缺省时由 config 自建（langgraph-cli 路径）。
        resources_dir: 资源目录。
        client: 共享 codemaker 客户端。
        config: langgraph 配置（{"configurable": {...}}），覆盖默认值。
        qa_handler: 可选，复用外部 QAHandler（如 OrchestratorAgent 持有的实例），
            使 chat_stream 注入的 sink 能透传到 graph 内 qa 节点。缺省自建。

    Returns:
        编译后的 CompiledGraph，invoke 时传入 AgentState 初始值。
    """
    cfg = get_config(config)
    if table_agent is None:
        table_agent, resources_dir, client = _build_engine_from_config(cfg)
    resources_dir = Path(resources_dir) if resources_dir else Path(cfg.directory)
    client = client or CodemakerClient()
    # model_name 显式从 client.cfg.default_model 取（即 .env 的 CODEMAKER_MODEL），
    # 不依赖 CodemakerChatModel 的 __init__ 默认（pydantic BaseChatModel 可能不透传子类 __init__ 的处理）。
    model_name = getattr(getattr(client, "cfg", None), "default_model", "") or cfg.model_name
    model = CodemakerChatModel(client=client, directory=str(resources_dir),
                               model_name=model_name or None)
    if qa_handler is None:
        qa_handler = QAHandler(client=client, model=model, resources_dir=resources_dir)
    else:
        # 复用外部实例，注入 model 走 LangChain 路径
        qa_handler.model = model
    crud_tool = make_crud_tool(table_agent)
    qa_tool = make_qa_tool(qa_handler, resources_dir)

    builder = StateGraph(AgentState)
    builder.add_node("classify", make_classify_node(model, client, think=classify_think))
    builder.add_node("qa", make_qa_node(qa_tool))
    builder.add_node("crud", make_crud_node(crud_tool))
    builder.add_edge(START, "classify")
    builder.add_conditional_edges(
        "classify", route_after_classify, {"qa": "qa", "crud": "crud"})
    builder.add_edge("qa", END)
    builder.add_edge("crud", END)
    return builder.compile()

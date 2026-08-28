"""AI 配表助手 Agent 包（LangGraph 架构）。

结构：
  根层（LangGraph 图编排）：configuration / state / prompts / llm / tools /
        nodes / graph / orchestrator / qa_handler / codemaker_client
  excel/（Excel 引擎子包）：TableAgent、解析器、匹配器、索引、CLI 等

对外公共 API 经本 __init__ 重导出；旧路径（agent.cli_interface 等）由 _compat 注册到 sys.modules 兼容。
"""

from . import _compat  # noqa: F401  注册已迁移模块的旧导入路径

from .excel.cli_interface import CodeMakerCLI, CLICallResult, StubCodeMakerCLI
from .excel.real_cli import RealCodeMakerCLI
from .excel.codemaker_parser import CodemakerNLParser
from .codemaker_client import (
    CodemakerClient,
    CodemakerClientConfig,
    PromptResult,
    SessionCreateResult,
)
from .excel.column_matcher import ColumnMatcher, ColumnMatch
from .excel.nl_parser import NLIntent
from .excel.table_index import (
    TableMeta,
    SheetMeta,
    build_index,
    load_index,
    refresh_if_changed,
    RefreshResult,
    compute_md5,
)
from .excel.alias_mapping import AliasMapping
from .excel.fuzzy_matcher import FuzzyMatcher, FuzzyCandidate
from .excel.table_resolver import TableResolver, TableResolve
from .excel.table_locator import TableLocator, LocateResult, LocateOutcome
from .excel.llm_context import (
    LLMContextBuilder,
    ContextResult,
    SkillExecutor,
    SKILL_DEFINITIONS,
    OPERATION_RULES,
    estimate_tokens,
    format_skills,
)
from .excel.backup_audit import BackupAuditor, AuditEntry
from .excel.table_relations import RelationGraph, TableRelation
from .excel.file_watcher import TableFileWatcher, has_watchdog
from .excel.skill_loader import (
    ColumnAliasConfig,
    RowAliasConfig,
    TableContextConfig,
)
from .excel.core.agent import TableAgent, AgentResult, AgentStep
from .orchestrator import OrchestratorAgent, RouteResult
from .qa_handler import QAHandler
from .configuration import Configuration, get_config
from .llm import CodemakerChatModel
from .tools import make_crud_tool, make_qa_tool, make_skill_tools
from .state import AgentState
from .graph import build_agent_graph

__all__ = [
    # Excel 引擎
    "CodeMakerCLI",
    "CLICallResult",
    "StubCodeMakerCLI",
    "RealCodeMakerCLI",
    "CodemakerNLParser",
    "ColumnMatcher",
    "ColumnMatch",
    "NLIntent",
    "TableMeta",
    "SheetMeta",
    "build_index",
    "load_index",
    "refresh_if_changed",
    "RefreshResult",
    "compute_md5",
    "AliasMapping",
    "FuzzyMatcher",
    "FuzzyCandidate",
    "TableResolver",
    "TableResolve",
    "TableLocator",
    "LocateResult",
    "LocateOutcome",
    "LLMContextBuilder",
    "ContextResult",
    "SkillExecutor",
    "SKILL_DEFINITIONS",
    "OPERATION_RULES",
    "estimate_tokens",
    "format_skills",
    "BackupAuditor",
    "AuditEntry",
    "RelationGraph",
    "TableRelation",
    "TableFileWatcher",
    "has_watchdog",
    "ColumnAliasConfig",
    "RowAliasConfig",
    "TableContextConfig",
    "TableAgent",
    "AgentResult",
    "AgentStep",
    # codemaker 客户端
    "CodemakerClient",
    "CodemakerClientConfig",
    "PromptResult",
    "SessionCreateResult",
    # LangGraph 图编排层
    "OrchestratorAgent",
    "RouteResult",
    "QAHandler",
    "Configuration",
    "get_config",
    "CodemakerChatModel",
    "make_crud_tool",
    "make_qa_tool",
    "make_skill_tools",
    "AgentState",
    "build_agent_graph",
]

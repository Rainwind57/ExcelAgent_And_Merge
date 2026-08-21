"""LangChain 工具：把 TableAgent / QAHandler / SkillExecutor 暴露为可调用 tool。

深度工具化：CRUD 与 QA 以 @tool 形式注册，由 LangGraph 节点按路由结果
确定性 invoke（codemaker 不支持 function-calling，故不走 LLM 自主选 tool）。

skill tools（make_skill_tools）用于 repair Level 2 的手写文本协议 ReAct 循环：
LLM 输出结构化 tool_call 标记 → 解析 → 调 skill_executor.call → 结果以「工具」角色
回灌 prompt。成功路径不调用 skill tools（仍走 build_skill_context 知识注入，零额外 LLM 往返）。
写类 skill tool（add_column / update_enum_mapping）返回确认提案，不直接落盘。
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from langchain_core.tools import tool

from .excel.agent import AgentResult

if TYPE_CHECKING:
    from pathlib import Path

    from .excel.agent import TableAgent
    from .excel.llm_context import SkillExecutor
    from .qa_handler import QAHandler


def make_crud_tool(table_agent: "TableAgent"):
    """构建 CRUD 工具：调用 TableAgent.run 执行表格增删改查。"""

    @tool
    def execute_crud(text: str, context: str = "") -> AgentResult:
        """执行自然语言表格操作指令（查询/修改/新增/删除）。

        Args:
            text: 用户的自然语言指令。
            context: 对话上下文摘要，用于 LLM 代词消解。
        """
        return table_agent.run(text, context=context)

    return execute_crud


def make_qa_tool(qa_handler: "QAHandler", resources_dir: "Path | None"):
    """构建 QA 工具：回答关于配表的自然语言问题。"""

    @tool
    def answer_question(question: str, history: str = "") -> str:
        """回答关于 Excel 配表结构/关系/用途的自然语言问题。

        Args:
            question: 用户问题。
            history: 对话上下文摘要（最近操作/问答），用于多轮记忆与代词消解。
        """
        return qa_handler.answer(question, history=history, resources_dir=resources_dir)

    return answer_question


def make_skill_tools(skill_executor: "SkillExecutor"):
    """构建 7 个 skill tool，供 repair Level 2 ReAct 循环 LLM 自主探查调用。

    每个 tool 转发到 skill_executor.call(skill_name, **kwargs)。写类 skill
    （add_column / update_enum_mapping）返回确认提案不直接落盘，由 repair 循环
    走 dry_run 预览确认流程。
    """

    @tool
    def locate_table(text: str) -> str:
        """根据自然语言描述定位到具体的表格文件和 Sheet（5 级递进策略，歧义返回候选）。"""
        result = skill_executor.call("locate_table", text=text)
        return json.dumps(result, ensure_ascii=False)

    @tool
    def fuzzy_search_value(query: str, candidates: str) -> str:
        """在候选值集合中模糊搜索，返回按相似度降序的候选列表。

        candidates 用逗号分隔的字符串（如 "刑天一阶,刑天二阶,饕餮"）。
        """
        cand_list = [c.strip() for c in (candidates or "").split(",") if c.strip()]
        result = skill_executor.call("fuzzy_search_value", query=query, candidates=cand_list)
        return json.dumps(result, ensure_ascii=False)

    @tool
    def get_table_structure(path: str) -> str:
        """获取指定表格的完整结构：每个 Sheet 的列名、数据起始行、行数、样本。"""
        result = skill_executor.call("get_table_structure", path=path)
        return json.dumps(result, ensure_ascii=False)

    @tool
    def list_all_tables() -> str:
        """列出所有已注册表格的简要信息（文件名 + Sheet 名列表）。"""
        result = skill_executor.call("list_all_tables")
        return json.dumps(result, ensure_ascii=False)

    @tool
    def analyze_enum_columns(path: str, sheet: str = "", max_unique: int = 20) -> str:
        """分析指定表所有 int 列的实际数据值，识别候选枚举列（无需人工配置）。"""
        result = skill_executor.call(
            "analyze_enum_columns", path=path, sheet=sheet or None, max_unique=max_unique
        )
        return json.dumps(result, ensure_ascii=False)

    @tool
    def add_column(path: str, sheet: str, name: str, after: int = -1,
                   type_str: str = "", default: str = "") -> str:
        """向指定 Sheet 添加列（写操作，返回确认提案，不直接落盘，待用户确认）。

        after=-1 表示追加末尾；type_str/default 为空表示不标注/不填充。
        """
        proposal = {
            "needs_confirm": True,
            "skill": "add_column",
            "args": {
                "path": path, "sheet": sheet, "name": name,
                "after": None if after < 0 else after,
                "type_str": type_str or None,
                "default": None if default == "" else default,
            },
        }
        return json.dumps(proposal, ensure_ascii=False)

    @tool
    def update_enum_mapping(stem: str, sheet: str, col_name: str, mappings: str) -> str:
        """为指定 int 列写入枚举映射（写操作，返回确认提案，不直接落盘，待用户确认）。

        mappings 用 JSON 字符串（如 '{"蓝":1,"紫":2}'）。
        """
        try:
            mapping_dict = json.loads(mappings) if isinstance(mappings, str) else mappings
        except (ValueError, TypeError):
            return json.dumps({"ok": False, "error": "mappings 需为 JSON 字符串"}, ensure_ascii=False)
        proposal = {
            "needs_confirm": True,
            "skill": "update_enum_mapping",
            "args": {"stem": stem, "sheet": sheet, "col_name": col_name, "mappings": mapping_dict},
        }
        return json.dumps(proposal, ensure_ascii=False)

    return [
        locate_table,
        fuzzy_search_value,
        get_table_structure,
        list_all_tables,
        analyze_enum_columns,
        add_column,
        update_enum_mapping,
    ]

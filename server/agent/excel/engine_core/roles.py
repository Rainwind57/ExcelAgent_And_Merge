"""SubAgent 角色细化:按表族派生,注入对应表 schema + 角色 prompt。

对齐 example.html flow-quest 的 3 个并行 Agent:
- DialogFillAgent: 对话表族(interaction.Interaction/InteractionConv/InteractionConvOption)
- ItemNpcFillAgent: NPC+道具+显隐+奇遇联合(entity_prefab/spawn_world_entity/item/...)
- ButterflyEventFillAgent: 任务主表族(task.Task/TaskBurstTask/TaskTaskState)

每个角色 SubAgent:
- _build_prompt 注入对应表的 Row2 英文字段名(供 LLM 产 fields 时用真实列名)
- 角色专用 prompt 模板(职责说明)
- 读 schema 通过 cli.read_header 动态获取(不依赖静态 skill 文档)

迁移自 subagent/roles.py（方案 A1 双骨架统一）；原文件保留 shim 兼容入口。
"""
from __future__ import annotations

import logging
from typing import Optional

from ..subagent.base import SubAgent
from ..subagent.llm_agent import LLMSubAgent

logger = logging.getLogger(__name__)


def _inject_schema(parts: list[str], cli, table_stem: str,
                   sheets: list[str], label: str) -> None:
    """读取指定表的 Row2 字段名,注入 prompt 供 LLM 用真实列名。

    Args:
        parts: prompt 行列表(函数内 append)
        cli: CodeMakerCLI 实例(读 read_header 取 Row1 中文,需 Row2 英文则用 read_table_rows)
        table_stem: 表 stem(如 "interaction")
        sheets: sheet 名列表(如 ["Interaction","InteractionConv"])
        label: schema 块标题(如 "对话表族 schema")
    """
    if cli is None:
        return
    try:
        # 用 list_tables 找到该 stem 的真实路径
        tables = cli.list_tables() if hasattr(cli, "list_tables") else []
        path = next((p for p in tables if p.stem == table_stem), None)
        if path is None:
            return
        parts.append(f"## {label}")
        for sn in sheets:
            try:
                headers = cli.read_header(path, sn)
                # read_header 返回 Row1 中文,需 Row2 英文用 table_case_eval.read_table_rows
                # 但 SubAgent 不应依赖 tests 模块。降级用中文表头作 schema 提示。
                parts.append(f"- {table_stem}.{sn}: {', '.join(h for h in headers if h)}")
            except Exception:
                continue
        parts.append("")
    except Exception:
        logger.debug(f"schema 注入失败 {table_stem}", exc_info=True)


class DialogFillAgent(LLMSubAgent):
    """对话表族配表专家:产 interaction.Interaction/InteractionConv/InteractionConvOption 片段。"""

    def __init__(self, parser=None, thinking_sink=None, cli=None):
        super().__init__("Dialog配表专家", parser=parser,
                         thinking_sink=thinking_sink,
                         prompt_template="生成对话表族 INSERT 片段")
        self._cli = cli

    def _build_prompt(self, task_prompt: str, context: dict) -> str:
        parts = [
            "你是对话配表专家,负责 interaction.xlsx 的对话表族配表。",
            "产出 Interaction(交互效果)+ InteractionConv(对话内容)+ "
            "InteractionConvOption(对话选项)的 INSERT 片段。",
            "",
        ]
        _inject_schema(parts, self._cli, "interaction",
                       ["Interaction", "InteractionConv", "InteractionConvOption"],
                       "对话表族 schema(列名)")
        return super()._build_prompt(task_prompt or self.prompt_template, context) + "\n" + "\n".join(parts)


class ItemNpcFillAgent(LLMSubAgent):
    """NPC+道具+显隐+奇遇联合 Agent:产多表片段。

    对齐 example.html 的 Agent B(NPC+Item+Showhide+Qiyu 联合)。
    """

    def __init__(self, parser=None, thinking_sink=None, cli=None):
        super().__init__("Item+Npc+Showhide+Qiyu联合", parser=parser,
                         thinking_sink=thinking_sink,
                         prompt_template="生成 NPC+道具+显隐+奇遇表 INSERT 片段")
        self._cli = cli

    def _build_prompt(self, task_prompt: str, context: dict) -> str:
        parts = [
            "你是 NPC+道具+显隐+奇遇联合配表 Agent,产多表 INSERT 片段:",
            "- entity_prefab.Base(NPC 实体,prefab_id/interaction_id 等)",
            "- spawn_world_entity.SpawnWorldEntity(场景刷新)",
            "- item.ItemBase(道具,按需)",
            "- showhide_npc.Tool(显隐,按需)",
            "- qiyu.Message(奇遇,按需)",
            "",
        ]
        _inject_schema(parts, self._cli, "entity_prefab", ["Base"], "NPC schema")
        _inject_schema(parts, self._cli, "spawn_world_entity", ["SpawnWorldEntity"], "刷新 schema")
        _inject_schema(parts, self._cli, "item", ["ItemBase"], "道具 schema")
        return super()._build_prompt(task_prompt or self.prompt_template, context) + "\n" + "\n".join(parts)


class ButterflyEventFillAgent(LLMSubAgent):
    """任务主表族 Agent:产 task.Task/BurstTask/TaskState 片段。

    对齐 example.html 的 Agent C(ButterflyEvent 主表族)。
    """

    def __init__(self, parser=None, thinking_sink=None, cli=None):
        super().__init__("ButterflyEvent主表族", parser=parser,
                         thinking_sink=thinking_sink,
                         prompt_template="生成任务主表族 INSERT 片段")
        self._cli = cli

    def _build_prompt(self, task_prompt: str, context: dict) -> str:
        parts = [
            "你是任务主表族配表 Agent,产 task.xlsx 的 Task/BurstTask/TaskState 片段。",
            "",
        ]
        _inject_schema(parts, self._cli, "task",
                       ["Task", "BurstTask", "TaskState"],
                       "任务表族 schema")
        return super()._build_prompt(task_prompt or self.prompt_template, context) + "\n" + "\n".join(parts)


class GenericFillAgent(LLMSubAgent):
    """通用填表 Agent:无明确表族归属时的兜底。"""

    def __init__(self, parser=None, thinking_sink=None, cli=None):
        super().__init__("通用填表", parser=parser,
                         thinking_sink=thinking_sink,
                         prompt_template="根据拆解结果生成对应表 INSERT 片段")
        self._cli = cli

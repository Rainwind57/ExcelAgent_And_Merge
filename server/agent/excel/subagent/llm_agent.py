"""LLM 驱动的 SubAgent 实现:调 codemaker LLM 产出 AgentFragment。

替代基类 SubAgent(raise NotImplementedError),让管道 Step3 在真实 LLM 环境可跑通。

设计:
- _run_impl 构造 prompt(任务描述 + context 符号映射表 + 分区表)
  → _call_llm → 解析返回 dict(sql_or_ops/produces/references/target_table/target_sheet)
- LLM 返回 JSON 格式:
  {"sql_or_ops":[{"action":"add","table_hint":"pet","sheet_hint":"Pet",
                 "fields":{"灵兽id":<id>,"名称":"xx"}}],
   "produces":"<pet_new>","references":[],"target_table":"pet","target_sheet":"Pet"}
- 失败(parser=None / LLM 不可达 / 返回非 dict)返回 None → run() 包成 ok=False fragment
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from .base import SubAgent

logger = logging.getLogger(__name__)


class LLMSubAgent(SubAgent):
    """LLM 驱动的通用填表 SubAgent。

    复用 CodemakerNLParser 的 HTTP 通道(_call_llm),按 prompt 产 fragment。
    子类可重写 _build_prompt 定制角色 prompt,默认用构造时传入的 prompt 模板。
    """

    def __init__(self, name: str, parser=None, thinking_sink=None,
                 prompt_template: str = "", default_phase: str = "执行"):
        super().__init__(name, parser=parser, thinking_sink=thinking_sink,
                          default_phase=default_phase)
        self.prompt_template = prompt_template

    def _build_prompt(self, task_prompt: str, context: dict) -> str:
        """构造 LLM prompt:任务描述 + 符号映射表 + 分区。"""
        parts = [task_prompt or self.prompt_template, ""]
        # 符号映射表在 doc_intent.symbol_map(dispatch 传 ctx.__dict__)
        doc = context.get("doc_intent")
        symbol_map = (getattr(doc, "symbol_map", None) or {}) if doc else {}
        if symbol_map:
            parts.append("## 符号映射表(引用时用 placeholder,不要写死 id)")
            for ph, origin in symbol_map.items():
                parts.append(f"- {ph}: {origin}")
            parts.append("")
        # 注入分区(已定位的表/sheet)
        partitions = context.get("partitions") or {}
        if partitions:
            parts.append("## 已定位目标表")
            for clue, loc in partitions.items():
                parts.append(f"- {clue}: {loc}")
            parts.append("")
        parts.append(
            "## 输出格式(JSON,不要 markdown 代码块)\n"
            "返回 {\"sql_or_ops\":[{\"action\":\"add\","
            "\"table_hint\":\"表stem\",\"sheet_hint\":\"Sheet\","
            "\"fields\":{\"列名\":值或<placeholder>}}],"
            "\"produces\":\"<占位符>\",\"references\":[],"
            "\"target_table\":\"表stem\",\"target_sheet\":\"Sheet\"}"
        )
        return "\n".join(parts)

    def _run_impl(self, prompt: str, skill_docs: list, context: dict):
        """调 LLM 产出 fragment dict。"""
        full_prompt = self._build_prompt(prompt, context)
        self.add_thinking("细分", f"{self.name} 调 LLM 产 fragment")
        data = self._call_llm(full_prompt)
        if data is None:
            return None
        # 校验必要字段
        if not isinstance(data, dict) or not data.get("sql_or_ops"):
            self.add_thinking("细分", f"{self.name} LLM 返回缺 sql_or_ops")
            return None
        self.add_thinking("细分",
                          f"{self.name} 产出 {len(data['sql_or_ops'])} ops, "
                          f"produces={data.get('produces')}")
        return data

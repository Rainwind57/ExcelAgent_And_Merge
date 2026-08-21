"""LangGraph 节点：意图分类 / QA / CRUD。

节点以工厂函数形式定义，捕获各自依赖（model / client / tool），
由 graph.build_agent_graph 装配。
"""

from __future__ import annotations

from langchain_core.messages import HumanMessage

from .excel.agent import AgentResult
from .excel.codemaker_parser import PromptMode, mode_classify
from .excel.nl_parser import NLIntent
from .prompts import ROUTE_SYSTEM_PROMPT

# 命中 ADD/MODIFY/DELETE 关键词即视为明确的表格操作意图，跳过 LLM 分类
# 这一跳（省一次 create_session + 一次 prompt，最坏节省 50s×2 重试）。
# 不含 QUERY：QUERY 关键词（"有哪些"/"是什么"等）与纯问答句式高度重叠，
# 误判风险高，仍走 LLM 判定，保证 qa/crud 路由的语义准确性。
_CRUD_SHORTCUT_MODES = (PromptMode.ADD, PromptMode.MODIFY, PromptMode.DELETE)


def make_classify_node(model, client, think=None):
    """意图分类节点：先规则短路命中明确表格操作，否则 LLM 判定 qa / crud。

    think: 可选回调 (phase, detail)，分类决策后立即推送「意图分类完成」，
    使该事件早于 CRUD 步骤到达，阶段切分能归入 s1_decompose 气泡。
    """

    def classify_node(state) -> dict:
        text = (state.get("text") or "").strip()
        if not text:
            return {"intent": "qa", "summary": "空输入",
                    "qa_answer": "请输入您的问题或操作指令。"}
        if mode_classify(text) in _CRUD_SHORTCUT_MODES:
            if think:
                think("意图分类完成", f"→ crud（规则短路）: {text[:60]}")
            return {"intent": "crud", "summary": text[:60]}
        # 注入对话上下文摘要，帮助 LLM 消解代词（"它/这个/上一句"等）
        context = (state.get("context") or "").strip()
        ctx_block = f"\n\n## 对话上下文（最近操作摘要）\n{context}" if context else ""
        prompt = f"{ROUTE_SYSTEM_PROMPT}{ctx_block}\n\n现在分类：{text}"
        resp = model.invoke([HumanMessage(content=prompt)])
        parsed = client.extract_json_from_response(resp.content)
        if isinstance(parsed, dict) and "intent" in parsed:
            intent = parsed.get("intent", "qa")
            summary = parsed.get("message", text)
            if think:
                think("意图分类完成", f"→ {intent}: {summary[:60]}")
            return {"intent": intent, "summary": summary}
        raise RuntimeError(f"codemaker 意图分类失败：{resp.content[:200]}")

    return classify_node


def make_qa_node(qa_tool):
    """QA 节点：调用 qa 工具回答配表问题。"""

    def qa_node(state) -> dict:
        answer = qa_tool.invoke({
            "question": state.get("text", ""),
            "history": state.get("context", ""),
        })
        return {"qa_answer": answer}

    return qa_node


def make_crud_node(crud_tool):
    """CRUD 节点：调用 crud 工具执行表格操作。dry_run 时跳过写盘。"""

    def crud_node(state) -> dict:
        if state.get("dry_run"):
            return {"crud_result": None}
        try:
            result = crud_tool.invoke({
                "text": state.get("text", ""),
                "context": state.get("context", ""),
            })
            return {"crud_result": result}
        except Exception as e:
            fallback = AgentResult(
                ok=False,
                intent=NLIntent(raw=state.get("text", ""), action="unknown"),
                message=f"CRUD 执行异常: {e}",
            )
            return {"crud_result": fallback}

    return crud_node


def route_after_classify(state) -> str:
    """条件边：按 intent 路由到 qa 或 crud。"""
    return "qa" if state.get("intent") == "qa" else "crud"

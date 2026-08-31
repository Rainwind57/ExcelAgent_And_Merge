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

# 规则兜底关键词：仅当 LLM 分类不可用/返回无法解析时才用（AI 为主判据）。
# 不在这里做"命中即短路"——LLM 是主分类器，规则只负责兜底。
_CRUD_RULE_FALLBACK_WORDS = (
    "新增", "添加", "增加", "加一个", "加一条", "建一个", "配一个", "创建",
    "放一个", "放个", "改成", "改为", "修改", "删除", "移除", "设置", "赋值",
    "调整", "add", "insert", "create", "update", "delete", "remove", "set",
)


def make_classify_node(model, client, think=None):
    """意图分类节点：LLM 判定为主，规则兜底。

    think: 可选回调 (phase, detail)，分类决策后立即推送「意图分类完成」，
    使该事件早于 CRUD 步骤到达，阶段切分能归入 s1_decompose 气泡。
    """

    def classify_node(state) -> dict:
        text = (state.get("text") or "").strip()
        if not text:
            return {"intent": "qa", "summary": "空输入",
                    "qa_answer": "请输入您的问题或操作指令。"}
        # 注入对话上下文摘要，帮助 LLM 消解代词（"它/这个/上一句"等）
        context = (state.get("context") or "").strip()
        ctx_block = f"\n\n## 对话上下文（最近操作摘要）\n{context}" if context else ""
        prompt = f"{ROUTE_SYSTEM_PROMPT}{ctx_block}\n\n现在分类：{text}"
        try:
            resp = model.invoke([HumanMessage(content=prompt)])
        except Exception as e:
            if think:
                think("意图分类完成", f"→ LLM 调用失败，规则兜底: {str(e)[:60]}")
            return _rule_fallback(text, think)
        parsed = client.extract_json_from_response(resp.content)
        if isinstance(parsed, dict) and "intent" in parsed:
            intent = parsed.get("intent", "qa")
            summary = parsed.get("message", text)
            if think:
                think("意图分类完成", f"→ {intent}: {summary[:60]}")
            return {"intent": intent, "summary": summary}
        # LLM 返回无法解析的 JSON：降级为规则启发式，而不是让整条请求失败
        if think:
            think("意图分类完成", "→ LLM 输出无法解析，规则兜底")
        return _rule_fallback(text, think)

    return classify_node


def _rule_fallback(text: str, think=None) -> dict:
    """规则启发式兜底：命中明确改表操作词 → crud；否则按 qa 处理。

    宁可误判进问答，也不让整条链路崩。
    """
    t = (text or "").lower()
    if any(w in t for w in _CRUD_RULE_FALLBACK_WORDS):
        return {"intent": "crud", "summary": text[:60]}
    return {"intent": "qa", "summary": text[:60]}


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
                "session_id": state.get("session_id", "") or "",
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

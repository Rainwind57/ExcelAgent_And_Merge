"""LLM 层：把 CodemakerClient 包装为 LangChain BaseChatModel。

LangGraph 节点通过标准 ChatModel 接口调用 LLM，底层仍走 codemaker serve。
每次 _generate 创建独立 codemaker 会话提交 prompt（无状态）——多轮记忆由
LangGraph 的 state/checkpointer 维护，每轮把完整上下文重新拼进 messages。
"""

from __future__ import annotations

import os
from typing import Any, List, Optional

from langchain_core.callbacks.manager import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import ConfigDict, Field, PrivateAttr

from .codemaker_client import CodemakerClient

_ROLE_LABEL = {"system": "系统", "human": "用户", "ai": "助手", "tool": "工具"}


def _format_messages(messages: List[BaseMessage]) -> str:
    """把 LangChain 消息列表折叠为 codemaker 可消费的单段文本 prompt。

    codemaker 的 /prompt 接口只接收一段文本，故按角色标签拼接。
    """
    parts: list[str] = []
    for m in messages:
        role = getattr(m, "type", "user")
        content = m.content
        if not isinstance(content, str):
            content = str(content)
        label = _ROLE_LABEL.get(role, role)
        parts.append(f"【{label}】\n{content}")
    return "\n\n".join(parts)


class CodemakerChatModel(BaseChatModel):
    """CodemakerClient 的 LangChain ChatModel 适配层。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    client: Any = Field(default=None)
    directory: str = ""
    # 默认从 client.cfg.default_model 取（即 .env 的 CODEMAKER_MODEL）。
    # 用 default_factory 而非 __init__ 重写，避开 pydantic BaseChatModel 对子类 __init__ 的处理差异。
    model_name: str = Field(default_factory=lambda: os.environ.get("CODEMAKER_MODEL", "") or "codemaker")
    # 会话缓存：同实例内跨 invoke 复用，避免每次分类都重建 codemaker session。
    _session_id: str = PrivateAttr(default="")

    def __init__(self, client: Optional[CodemakerClient] = None,
                 directory: str = "", model_name: Optional[str] = None, **kwargs: Any):
        cli = client or CodemakerClient()
        # model_name 未显式传入时，从 client 配置的 default_model 取（来自 .env 的 CODEMAKER_MODEL）。
        # 否则会用占位 "codemaker"，_to_model_ref 无 "/" → None → serve 回退默认模型，
        # .env 的 CODEMAKER_MODEL 配置形同虚设。
        if model_name is None:
            model_name = getattr(getattr(cli, "cfg", None), "default_model", "") or "codemaker"
        super().__init__(client=cli, directory=directory, model_name=model_name, **kwargs)

    @property
    def _llm_type(self) -> str:
        return "codemaker"

    @property
    def _identifying_params(self) -> dict:
        return {"model_name": self.model_name, "directory": self.directory}

    def _ensure_session(self) -> str:
        """确保有一个活跃的 codemaker 会话，同实例内缓存复用。

        对照 CodemakerNLParser._ensure_session：分类节点此前每次 invoke 都
        新建 session（一次额外 HTTP 往返，超时 CODEMAKER_API_TIMEOUT=120s），
        与 parser 侧的会话复用不对称。同一 OrchestratorAgent/model 实例的多次
        分类调用（如二次确认 confirm_cascade 续传）可安全复用同一会话。
        """
        if self._session_id:
            return self._session_id
        sess = self.client.create_session(directory=self.directory, model=self.model_name)
        if not sess.ok:
            err = RuntimeError(f"创建 codemaker 会话失败：{sess.error}")
            err.error_type = getattr(sess, "error_type", "")
            raise err
        self._session_id = sess.session_id
        return self._session_id

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Optional[CallbackManagerForLLMRun] = None,
                  **kwargs: Any) -> ChatResult:
        prompt = _format_messages(messages)
        if not self.client.health_check():
            err = RuntimeError("codemaker serve 不可用，请先启动 codemaker serve")
            err.error_type = "serve_down"
            raise err
        session_id = self._ensure_session()
        resp = self.client.prompt(session_id, prompt, model=self.model_name,
                                  cancel_event=getattr(self, "_cancel_event", None))
        if not resp.ok:
            err = RuntimeError(f"codemaker 调用失败：{resp.error}")
            err.error_type = getattr(resp, "error_type", "")
            raise err
        msg = AIMessage(content=resp.response_text)
        return ChatResult(generations=[ChatGeneration(message=msg)])

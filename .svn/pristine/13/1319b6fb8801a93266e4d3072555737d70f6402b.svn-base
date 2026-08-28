"""SubAgent 抽象基类:并行 LLM 子 Agent + thinking 流聚合。

设计:
- run(prompt, skill_docs, context) -> AgentFragment
- 注入 thinking_sink 回调,add_thinking 同步推送主流 SSE
- LLM 调用复用 CodemakerNLParser 的 HTTP 通道(不新建 LLM 基础设施)
- 隔离失败:单 SubAgent 异常不中断其他,返回 ok=False fragment
"""

from __future__ import annotations

import logging
import os
import tempfile
import atexit
import shutil
import time
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# thinking sink 签名:(phase: str, detail: str) -> 注入
ThinkingSink = Callable[[str, str], None]

# ── R7 修复:子 Agent 会话上下文隔离 ──────────────────────────────
# codemaker serve 对 directory 内文件做自动上下文(读 xlsx)，prompt 文本出现表 stem
# 时会触发其内部 agent 去读 quest.xlsx/reward.xlsx 等 → 90s+ 超时返空、143.8k Token
# 浪费(用户原始 [step3] 卡死根因)。excel-agent 全程把真实 schema 注入 prompt
# (DecomposeAgent schema_block / parse_multi skill_context)，并不依赖 serve 读项目文件，
# 故子 Agent 会话用「空临时目录」隔离是安全且大幅提速的。
# 用 CODEMAKER_SUBAGENT_ISOLATE_CONTEXT=0 关闭(回退用 parser.directory)。
_ISOLATED_DIR: Optional[str] = None


def _isolated_empty_dir() -> str:
    """惰性创建并缓存一个空临时目录供 serve 会话上下文隔离。"""
    global _ISOLATED_DIR
    if _ISOLATED_DIR and os.path.isdir(_ISOLATED_DIR):
        return _ISOLATED_DIR
    d = tempfile.mkdtemp(prefix="codemaker_subagent_iso_")
    atexit.register(lambda: shutil.rmtree(d, ignore_errors=True))
    _ISOLATED_DIR = d
    return d


def _AgentFragment():
    """延迟导入 pipeline.types.AgentFragment 避免循环 import
    (subagent ← pipeline ← subagent)。"""
    from ..pipeline.types import AgentFragment
    return AgentFragment


class SubAgent:
    """SubAgent 抽象基类:子类实现 _run_impl 产出 AgentFragment。

    子类典型:
    - DecomposeAgent: Step1 拆解(parse_file → DocIntent + 符号映射)
    - DialogFillAgent: Step3 Dialog 配表专家
    - ItemNpcFillAgent: Step3 Item+Npc+Showhide+Qiyu 联合
    - ButterflyEventFillAgent: Step3 ButterflyEvent 主表族

    Attributes:
        name: Agent 名(如 "Dialog配表专家")
        parser: CodemakerNLParser 实例(复用 HTTP 通道)
        thinking_sink: 注入的 thinking 回调,add_thinking 时同步推送
    """

    def __init__(self, name: str, parser=None, thinking_sink: Optional[ThinkingSink] = None):
        self.name = name
        self.parser = parser
        self._thinking_sink = thinking_sink
        # 独立 session id(不共享 parser 的 session,避免并行 SubAgent 同 sid 冲突)
        self._sid: str = ""

    def set_thinking_sink(self, sink: ThinkingSink) -> None:
        """dispatcher 注入:聚合到主流单一 sink。"""
        self._thinking_sink = sink

    def add_thinking(self, phase: str, detail: str) -> None:
        """添加思考步骤:本地记录 + 同步推送主流 SSE。

        复用 agent.py:358 add_thinking 机制。phase 沿用现有约定
        (解析/路由/定位/校验/执行/跨表探索)。
        """
        if phase and detail and self._thinking_sink:
            try:
                self._thinking_sink(phase, detail)
            except Exception:
                pass

    def run(self, prompt: str, skill_docs: list[str] = None,
            context: dict = None):
        """执行 SubAgent,返回 AgentFragment。失败不抛,返回 ok=False fragment。

        Args:
            prompt: 任务描述(prompt 模板,子类构造或 dispatcher 传入)
            skill_docs: 子 Agent 用 Read 工具按需读取的 skill 文档路径列表
            context: 上下文(符号映射表/分区/拆解结果等纯结构数据)
        """
        AF = _AgentFragment()
        try:
            self.add_thinking("执行", f"{self.name} 开始执行")
            result = self._run_impl(prompt, skill_docs or [], context or {})
            frag = AF(agent_name=self.name)
            if result is None:
                # 子类返回 None = 失败(如 LLM 不可达/返回无效)
                frag.ok = False
                frag.error = f"{self.name} 未产出 fragment(返回 None)"
            elif isinstance(result, AF):
                frag = result
                frag.agent_name = self.name
            else:
                frag.sql_or_ops = result.get("sql_or_ops", [])
                frag.produces = result.get("produces")
                frag.references = result.get("references", [])
                frag.target_table = result.get("target_table", "")
                frag.target_sheet = result.get("target_sheet", "")
            frag.agent_name = self.name
            self.add_thinking("执行", f"{self.name} 执行完成")
        except Exception as e:
            logger.warning(f"SubAgent {self.name} 执行失败", exc_info=True)
            frag.ok = False
            frag.error = f"{type(e).__name__}: {e}"
            self.add_thinking("执行", f"{self.name} 执行失败: {e}")
        return frag

    def _run_impl(self, prompt: str, skill_docs: list[str],
                  context: dict) -> Optional[AgentFragment | dict]:
        """子类实现:产出 AgentFragment 或 dict(sql_or_ops/produces/references)。

        子类可:
        - 用 self.parser 复用 HTTP 通道调 LLM
        - 用 Read 工具读 skill_docs(由子类自行调 read_file)
        - 从 context 取符号映射表/分区等
        """
        raise NotImplementedError

    def _ensure_own_session(self) -> str:
        """本 SubAgent 独立 session（不共享 parser 的 session）。

        复用 parser 的 HTTP client，但自建 session id 缓存在实例上，避免并行
        SubAgent 共用同一 sid 被 serve 端串行化或状态混乱。失败返回 ""。
        """
        if self._sid:
            return self._sid
        if not self.parser:
            return ""
        client = getattr(self.parser, "client", None)
        if client is None:
            return ""
        try:
            # 子 Agent 会话上下文隔离(R7):用空临时目录而非 parser.directory(资源目录),
            # 避免 serve 端读 prompt 中出现的表 stem 对应 xlsx 致 90s+ 超时空回复。
            if os.environ.get("CODEMAKER_SUBAGENT_ISOLATE_CONTEXT", "1") != "0":
                session_dir = _isolated_empty_dir()
            else:
                session_dir = getattr(self.parser, "directory", "")
            result = client.create_session(
                directory=session_dir,
                model=getattr(self.parser, "model", ""))
        except Exception:
            return ""
        if getattr(result, "ok", False):
            self._sid = result.session_id
            return self._sid
        return ""

    def _bump_llm(self, site: str) -> None:
        """LLM 调用计数：inc + merge_to_instance，使心跳 peek_total 实时可见。

        decompose/locator/validator 的 LLM 调用经此打点（agent._llm_counter 经
        parser 下传）。token 估算留 0（调用次数是心跳/eval 主指标；token 精确
        估算需 estimate_tokens，此处不引入额外依赖）。
        """
        c = getattr(self.parser, "_llm_counter", None) if self.parser else None
        if c is None:
            return
        try:
            c.inc(site)
            c.merge_to_instance()
        except Exception:
            pass

    def _call_llm(self, prompt: str, timeout: int = 90) -> Optional[dict]:
        """复用 CodemakerNLParser HTTP 通道调 LLM,返回解析后 JSON dict。

        子类用此方法调 LLM 产出 SQL 片段等,不新建 HTTP client。
        使用本 SubAgent 独立 session(非 parser 共享 session),保证并行隔离。
        """
        if not self.parser:
            logger.warning(f"SubAgent {self.name} 无 parser,LLM 调用跳过")
            return None
        sid = self._ensure_own_session()
        if not sid:
            return None
        self._bump_llm(self.name or "subagent")
        from .llm_gate import llm_throttle
        with llm_throttle():
            resp = self.parser.client.prompt(sid, prompt, timeout=timeout,
                                              model=getattr(self.parser, "model", ""),
                                              cancel_event=getattr(self.parser, "_cancel_event", None))
        if not resp.ok:
            self.add_thinking("执行", f"LLM 调用失败: {resp.error}")
            return None
        data = self.parser.client.extract_json_from_response(resp.response_text)
        if isinstance(data, list) and data:
            data = data[0]
        return data if isinstance(data, dict) else None

    def _call_llm_raw(self, prompt: str, timeout: int = 90) -> Optional[str]:
        """调 LLM 返回原始响应文本(不经 extract_json_from_response)。

        供需要 JSON 数组(多 op)或自定义解析的子类使用,如 DecomposeAgent
        产每表一 op 的 JSON 数组(基类 _call_llm 强制取首元素会破坏多 op)。
        """
        if not self.parser:
            logger.warning(f"SubAgent {self.name} 无 parser,LLM 调用跳过")
            return None
        sid = self._ensure_own_session()
        if not sid:
            return None
        self._bump_llm(self.name or "subagent")
        try:
            from .llm_gate import llm_throttle
            with llm_throttle():
                resp = self.parser.client.prompt(sid, prompt, timeout=timeout,
                                                  model=getattr(self.parser, "model", ""),
                                                  cancel_event=getattr(self.parser, "_cancel_event", None))
        except Exception as e:
            self.add_thinking("执行", f"LLM 调用异常: {e}")
            return None
        if not resp.ok:
            self.add_thinking("执行", f"LLM 调用失败: {resp.error}")
            return None
        return resp.response_text or ""

"""SubAgent 并行派发器:asyncio.gather + thinking 聚合。

设计:
- dispatch(subagents, context) -> list[AgentFragment]
- asyncio.gather 并发执行,thinking_sink 聚合到主流单一 sink
- 超时(默认 120s):超时取消该 SubAgent,返回 fragment ok=False error="timeout"
- 隔离失败:单 SubAgent 失败不中断其他,返回顺序与输入对齐

线程化 worker 内 asyncio loop 嵌套处理:
agent_service.py:1658 chat_stream 用 thread worker 跑同步 chat()。
dispatcher 在该 thread 内用 asyncio.run() 起新 loop 执行 gather,
thinking 经 sink → queue → 主 async loop yield SSE。
"""

import asyncio
import logging
import os
from typing import Optional

from .base import SubAgent, ThinkingSink

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = int(os.environ.get("CODEMAKER_SUBAGENT_TIMEOUT", "120"))


def _AgentFragment():
    """延迟导入 pipeline.types.AgentFragment 避免循环 import。"""
    from ..pipeline.types import AgentFragment
    return AgentFragment


def dispatch(subagents: list, prompts: list,
             context: dict, thinking_sink: Optional[ThinkingSink] = None,
             timeout: int = _DEFAULT_TIMEOUT) -> list:
    """并发派发 SubAgent,返回 AgentFragment 列表(顺序与输入对齐)。

    Args:
        subagents: SubAgent 实例列表
        prompts: 每个 SubAgent 的 prompt(与 subagents 等长)
        context: 上下文(符号映射表/分区/拆解结果)
        thinking_sink: 主流 SSE thinking sink,聚合各 SubAgent 的 add_thinking
        timeout: 单个 SubAgent 超时秒数

    Returns:
        AgentFragment 列表,顺序与 subagents 对齐;失败/超时返回 ok=False fragment
    """
    if len(subagents) != len(prompts):
        raise ValueError(f"subagents({len(subagents)}) 与 prompts({len(prompts)}) 长度不符")

    # 注入 thinking_sink 到每个 SubAgent
    for sa in subagents:
        sa.set_thinking_sink(thinking_sink or (lambda p, d: None))

    async def _run_one(sa: SubAgent, prompt: str, idx: int):
        """单 SubAgent 异步执行,带超时。"""
        AF = _AgentFragment()
        try:
            # SubAgent.run 是同步方法,用 to_thread 不阻塞 loop
            frag = await asyncio.wait_for(
                asyncio.to_thread(sa.run, prompt, [], context),
                timeout=timeout,
            )
            return frag
        except asyncio.TimeoutError:
            logger.warning(f"SubAgent {sa.name} 超时({timeout}s)")
            return AF(agent_name=sa.name, ok=False, error="timeout")
        except Exception as e:
            logger.warning(f"SubAgent {sa.name} 执行异常", exc_info=True)
            return AF(agent_name=sa.name, ok=False,
                                 error=f"{type(e).__name__}: {e}")

    async def _gather_all() -> list[AgentFragment]:
        return await asyncio.gather(*[
            _run_one(subagents[i], prompts[i], i) for i in range(len(subagents))
        ])

    # 在当前线程起 event loop(兼容 thread worker 场景)
    try:
        # 若已有 running loop(如已在 async 上下文),用 asyncio.run 会报错
        # 此时改用 run_in_executor 或新 thread 跑
        try:
            asyncio.get_running_loop()
            # 已在 async 上下文:用新线程跑 gather,避免嵌套
            import threading
            result_box: list = []
            def _runner():
                result_box.extend(asyncio.run(_gather_all()))
            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join(timeout + 10)  # 留余量
            if result_box:
                return result_box
            AF = _AgentFragment()
            return [AF(agent_name=sa.name, ok=False, error="gather thread timeout")
                    for sa in subagents]
        except RuntimeError:
            # 无 running loop:直接 asyncio.run
            return asyncio.run(_gather_all())
    except Exception as e:
        logger.error("dispatch 执行失败", exc_info=True)
        AF = _AgentFragment()
        return [AF(agent_name=sa.name, ok=False, error=f"dispatch error: {e}")
                for sa in subagents]

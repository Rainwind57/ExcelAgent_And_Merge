"""全局 LLM 调用 throttle(Semaphore)。

O3 优化:跨 phase(SubAgent/StepAIEnhancer/DecomposeAgent)共享一个 BoundedSemaphore,
封顶并发 LLM 调用数,防 serve 端被叠峰压垮(R7 hang 143.8k token/156s 根因之一)。

设计:
- 模块级单例 Semaphore,max=N(env CODEMAKER_LLM_GLOBAL_MAX,默认 5)
- 提供 llm_throttle() context manager,调用点 `with llm_throttle():` 即可
- 无 nested LLM 调用风险(各 _call_llm/_run_one 只 client.prompt 一次,不重入)
- acquire 阻塞不超 LLM_THROTTLE_TIMEOUT_S(默认 180s),超时放行降级(不死锁)
"""
from __future__ import annotations

import logging
import os
import threading
from contextlib import contextmanager

logger = logging.getLogger(__name__)

_MAX = max(1, int(os.environ.get("CODEMAKER_LLM_GLOBAL_MAX", "5")))
_TIMEOUT = max(10, int(os.environ.get("CODEMAKER_LLM_THROTTLE_TIMEOUT_S", "180")))
_sem = threading.BoundedSemaphore(_MAX)
_acquired_count = 0
_wait_total = 0.0
_lock = threading.Lock()


@contextmanager
def llm_throttle():
    """全局 LLM 并发节流。acquire 超时降级放行(记 warning),不死锁调用方。"""
    global _acquired_count, _wait_total
    import time as _t
    t0 = _t.monotonic()
    ok = _sem.acquire(timeout=_TIMEOUT)
    waited = _t.monotonic() - t0
    if not ok:
        logger.warning("llm_throttle acquire 超时 %ss(并发已满 %d),降级放行",
                       round(waited, 1), _MAX)
    with _lock:
        _acquired_count += 1
        _wait_total += waited
    try:
        yield
    finally:
        if ok:
            _sem.release()


def stats() -> dict:
    """节流统计(供心跳/eval 观测并发健康度)。"""
    with _lock:
        return {
            "global_max": _MAX,
            "acquired_total": _acquired_count,
            "wait_total_s": round(_wait_total, 2),
            "acquire_timeout_s": _TIMEOUT,
        }


__all__ = ["llm_throttle", "stats"]

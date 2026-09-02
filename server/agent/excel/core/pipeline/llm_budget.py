"""建议2：Step1 LLM 调用预算（纯函数/纯状态，0 IO）。

背景（docs §"优化目标不是更长 timeout，而是更小问题" + 交接建议2）：复杂任务
Step1 会 decompose → backfill → complete → cross_ref 各自串行调 LLM，一处超时就继续
串行补洞，墙钟爆炸。本模块提供一个**硬预算计数器**：单次 Step1（decompose）内 LLM
调用数封顶（默认 3）；超预算后 try_consume() 返回 False，调用方据此**跳过后续可选
LLM 阶段**（backfill/complete），返回已有结果 + 结构化 partial，而非继续串行补洞。

纯状态对象、无 IO、无 LLM、确定性，可离线单测。
"""
from __future__ import annotations

__all__ = ["LLMBudget"]


class LLMBudget:
    """Step1 LLM 调用硬预算计数器。

    用法：
        b = LLMBudget(3)
        if b.try_consume(): <调 LLM>     # 前 3 次返回 True 并 +1
        else: <跳过，走 partial/兜底>     # 第 4 次起返回 False
    """

    def __init__(self, limit: int = 3):
        try:
            self.limit = max(0, int(limit))
        except (TypeError, ValueError):
            self.limit = 3
        self.used = 0

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def exhausted(self) -> bool:
        return self.used >= self.limit

    def can_afford(self) -> bool:
        """是否还有预算（不消费）。"""
        return self.used < self.limit

    def try_consume(self, n: int = 1) -> bool:
        """尝试消费 n 次预算。成功（预算足）返回 True 并累加；否则返回 False 不累加。"""
        if self.used + n <= self.limit:
            self.used += n
            return True
        return False

    def snapshot(self) -> dict:
        return {"limit": self.limit, "used": self.used,
                "remaining": self.remaining, "exhausted": self.exhausted}

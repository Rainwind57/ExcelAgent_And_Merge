"""LLM 调用计数器：per-run 打点 LLM 往返次数 + token 估算。

设计动机：
    eval 报告需 LLM 调用次数与 token 消耗支撑优化前后对比（如 ai_confirm_table 冗余
    削减的次数证据）。elapsed_ms 是唯一成本代理但无法拆分往返构成。

实现：
    线程本地计数器（threading.local）避免多线程 eval（skill_ab_test 并行）竞态。
    agent._call_llm / parser.parse_multi 入口 inc(site, tokens)。
    run 结束 snapshot() 返回统计挂 AgentResult 供 eval 采集。
    生产路径不调 snapshot 则仅空累加（零 IO 零副作用）。

capability: llm-call-instrumentation
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field


@dataclass
class _SiteStats:
    calls: int = 0
    tokens: int = 0
    # 可观测性扩展（StepTrace §P0「先做可观测性」）：证明「慢在哪」而非改逻辑。
    # observe() 在 LLM 往返结束后累加，不改变 calls（inc() 负责计次）。
    dur_ms: int = 0        # 该 site 全部往返累计耗时
    prompt_chars: int = 0  # 送入模型的 prompt 字符数累计（近似 schema/上下文体量）
    resp_chars: int = 0    # 模型响应字符数累计
    timeouts: int = 0      # 超时次数
    errors: int = 0        # 失败/空响应次数


@dataclass
class LLMStats:
    """snapshot 返回的统计快照。"""

    total_calls: int = 0
    total_tokens: int = 0
    by_site: dict[str, dict] = field(default_factory=dict)
    success_path_calls: int = 0
    failure_path_calls: int = 0
    # 可观测性聚合（跨 site 求和），供 Step4/eval 报告直接展示「慢因」。
    total_dur_ms: int = 0
    total_prompt_chars: int = 0
    total_resp_chars: int = 0
    total_timeouts: int = 0
    total_errors: int = 0


class LLMCounter:
    """线程本地 LLM 调用计数器。

    用法：
        counter = LLMCounter()
        counter.inc("parse", tokens=1500)  # 在 LLM 调用点
        counter.mark_failure_path()        # 失败路径标记
        stats = counter.snapshot()         # run 结束取快照
        counter.reset()                    # 下次 run 清零
    """

    def __init__(self) -> None:
        self._instance_lock = threading.Lock()
        self._instance_stats: dict[str, _SiteStats] = {}
        self._instance_success_calls = 0
        self._instance_failure_calls = 0
        self._instance_failure_marked = False
        self._tls = threading.local()

    def _tls_stats(self) -> dict[str, _SiteStats]:
        if not hasattr(self._tls, "stats"):
            self._tls.stats = {}
        return self._tls.stats

    def inc(self, site: str = "unknown", tokens: int = 0) -> None:
        """记录一次 LLM 调用。线程本地累加，末尾 merge_to_instance 汇总。"""
        stats = self._tls_stats()
        s = stats.get(site)
        if s is None:
            s = _SiteStats()
            stats[site] = s
        s.calls += 1
        s.tokens += tokens

    def observe(self, site: str = "unknown", *, dur_ms: int = 0,
                prompt_chars: int = 0, resp_chars: int = 0,
                timeout: bool = False, error: bool = False) -> None:
        """记录一次 LLM 往返的可观测性指标（不增加 calls，inc() 已计次）。

        在 client.prompt 返回后调用，累加耗时/prompt 体量/响应体量/超时/错误。
        与 inc() 同 site 键累加到同一 _SiteStats（同线程 tls），merge 时统一汇总。
        纯累加、无 IO、失败也应被记录（error=True）以便归因。
        """
        stats = self._tls_stats()
        s = stats.get(site)
        if s is None:
            s = _SiteStats()
            stats[site] = s
        try:
            s.dur_ms += int(dur_ms or 0)
            s.prompt_chars += int(prompt_chars or 0)
            s.resp_chars += int(resp_chars or 0)
        except (TypeError, ValueError):
            pass
        if timeout:
            s.timeouts += 1
        if error:
            s.errors += 1

    def mark_failure_path(self) -> None:
        """标记当前 run 走了失败路径（verify-repair 触发）。"""
        self._tls.failure_marked = True

    def merge_to_instance(self) -> None:
        """把线程本地计数汇总到实例属性（run 结束前调）。"""
        with self._instance_lock:
            for site, s in self._tls_stats().items():
                tgt = self._instance_stats.get(site)
                if tgt is None:
                    tgt = _SiteStats()
                    self._instance_stats[site] = tgt
                tgt.calls += s.calls
                tgt.tokens += s.tokens
                tgt.dur_ms += s.dur_ms
                tgt.prompt_chars += s.prompt_chars
                tgt.resp_chars += s.resp_chars
                tgt.timeouts += s.timeouts
                tgt.errors += s.errors
            if getattr(self._tls, "failure_marked", False):
                self._instance_failure_marked = True
            # 清线程本地（已汇总）
            self._tls.stats = {}
            self._tls.failure_marked = False

    def peek_total(self) -> int:
        """返回当前累计 LLM 调用次数（不 merge、不清 tls），供心跳实时读取。
        加锁：心跳在 asyncio loop 读，worker 线程可能同时 merge_to_instance。"""
        with self._instance_lock:
            inst = sum(s.calls for s in self._instance_stats.values())
        tls = sum(s.calls for s in self._tls_stats().values())
        return inst + tls

    def snapshot(self) -> LLMStats:
        """返回统计快照并按 success/failure 路径分流。"""
        self.merge_to_instance()
        total_calls = sum(s.calls for s in self._instance_stats.values())
        total_tokens = sum(s.tokens for s in self._instance_stats.values())
        by_site = {
            site: {
                "calls": s.calls, "tokens": s.tokens,
                "dur_ms": s.dur_ms, "prompt_chars": s.prompt_chars,
                "resp_chars": s.resp_chars, "timeouts": s.timeouts,
                "errors": s.errors,
            }
            for site, s in self._instance_stats.items()
        }
        # failure_path_calls = failure_marked 时的全部调用；success_path = 总数 - failure
        failure_calls = total_calls if self._instance_failure_marked else 0
        success_calls = total_calls - failure_calls
        return LLMStats(
            total_calls=total_calls,
            total_tokens=total_tokens,
            by_site=by_site,
            success_path_calls=success_calls,
            failure_path_calls=failure_calls,
            total_dur_ms=sum(s.dur_ms for s in self._instance_stats.values()),
            total_prompt_chars=sum(s.prompt_chars for s in self._instance_stats.values()),
            total_resp_chars=sum(s.resp_chars for s in self._instance_stats.values()),
            total_timeouts=sum(s.timeouts for s in self._instance_stats.values()),
            total_errors=sum(s.errors for s in self._instance_stats.values()),
        )

    def reset(self) -> None:
        """清零（agent.run 入口调）。"""
        with self._instance_lock:
            self._instance_stats = {}
            self._instance_success_calls = 0
            self._instance_failure_calls = 0
            self._instance_failure_marked = False
        self._tls.stats = {}
        self._tls.failure_marked = False

    def as_dict(self) -> dict:
        """snapshot 的 dict 形式（挂 AgentResult.extras 用）。"""
        s = self.snapshot()
        return {
            "total_calls": s.total_calls,
            "total_tokens": s.total_tokens,
            "by_site": s.by_site,
            "success_path_calls": s.success_path_calls,
            "failure_path_calls": s.failure_path_calls,
            "total_dur_ms": s.total_dur_ms,
            "total_prompt_chars": s.total_prompt_chars,
            "total_resp_chars": s.total_resp_chars,
            "total_timeouts": s.total_timeouts,
            "total_errors": s.total_errors,
        }

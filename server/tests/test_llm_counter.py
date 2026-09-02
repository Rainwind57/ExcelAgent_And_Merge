"""LLMCounter 单测（capability: llm-call-instrumentation）。"""
from __future__ import annotations

import os
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.llm_counter import LLMCounter


def test_inc_accumulates_by_site():
    c = LLMCounter()
    c.inc("parse", tokens=1500)
    c.inc("parse", tokens=500)
    c.inc("confirm_table", tokens=800)
    s = c.snapshot()
    assert s.total_calls == 3
    assert s.total_tokens == 2800
    assert s.by_site["parse"]["calls"] == 2
    assert s.by_site["parse"]["tokens"] == 2000
    assert s.by_site["confirm_table"]["calls"] == 1


def test_thread_isolation():
    """多线程 inc 互不串扰（线程本地）。"""
    c = LLMCounter()
    results = {}

    def worker(name, n):
        for _ in range(n):
            c.inc(name)
        c.merge_to_instance()
        results[name] = c.snapshot().total_calls

    t1 = threading.Thread(target=worker, args=("t1", 5))
    t2 = threading.Thread(target=worker, args=("t2", 3))
    t1.start(); t2.start()
    t1.join(); t2.join()
    # 汇总后总数 = 8（两线程 inc 都 merge 到实例）
    c.merge_to_instance()
    assert c.snapshot().total_calls == 8


def test_success_failure_path_split():
    c = LLMCounter()
    c.inc("parse")
    c.inc("plan")
    c.mark_failure_path()
    c.inc("react")
    c.merge_to_instance()
    s = c.snapshot()
    assert s.failure_path_calls == 3
    assert s.success_path_calls == 0


def test_reset_clears():
    c = LLMCounter()
    c.inc("parse")
    c.merge_to_instance()
    assert c.snapshot().total_calls == 1
    c.reset()
    assert c.snapshot().total_calls == 0


def test_as_dict_format():
    c = LLMCounter()
    c.inc("parse", tokens=100)
    d = c.as_dict()
    assert d["total_calls"] == 1
    assert d["total_tokens"] == 100
    assert d["by_site"]["parse"]["calls"] == 1
    assert "success_path_calls" in d


def test_unknown_site_default():
    c = LLMCounter()
    c.inc()  # 默认 site
    s = c.snapshot()
    assert s.by_site["unknown"]["calls"] == 1


# ── StepTrace §P0 可观测性扩展 ────────────────────────────────

def test_observe_accumulates_metrics():
    """observe 累加 dur/prompt/resp/timeout/error，且不增加 calls。"""
    c = LLMCounter()
    c.inc("decompose")
    c.observe("decompose", dur_ms=1200, prompt_chars=8000, resp_chars=600)
    c.observe("decompose", dur_ms=800, prompt_chars=5000, resp_chars=400,
              timeout=True, error=True)
    s = c.snapshot()
    assert s.by_site["decompose"]["calls"] == 1  # observe 不计次
    assert s.by_site["decompose"]["dur_ms"] == 2000
    assert s.by_site["decompose"]["prompt_chars"] == 13000
    assert s.by_site["decompose"]["resp_chars"] == 1000
    assert s.by_site["decompose"]["timeouts"] == 1
    assert s.by_site["decompose"]["errors"] == 1


def test_observe_totals_and_as_dict():
    c = LLMCounter()
    c.inc("locate"); c.observe("locate", dur_ms=300, prompt_chars=1000, resp_chars=200)
    c.inc("decompose"); c.observe("decompose", dur_ms=1500, prompt_chars=9000,
                                  resp_chars=700, error=True)
    s = c.snapshot()
    assert s.total_dur_ms == 1800
    assert s.total_prompt_chars == 10000
    assert s.total_resp_chars == 900
    assert s.total_errors == 1
    d = c.as_dict()
    assert d["total_dur_ms"] == 1800
    assert d["total_prompt_chars"] == 10000
    assert d["total_timeouts"] == 0
    assert d["total_errors"] == 1


def test_observe_only_no_inc():
    """未 inc 的 site 先 observe 也安全（自动建条，calls=0）。"""
    c = LLMCounter()
    c.observe("orphan", dur_ms=50, prompt_chars=10)
    s = c.snapshot()
    assert s.by_site["orphan"]["calls"] == 0
    assert s.by_site["orphan"]["dur_ms"] == 50


def test_observe_delta_snapshot():
    """差值法（Step1 用）：两次 as_dict 相减得到本阶段增量。"""
    c = LLMCounter()
    c.inc("a"); c.observe("a", dur_ms=100, prompt_chars=500)
    before = c.as_dict()
    c.inc("b"); c.observe("b", dur_ms=400, prompt_chars=3000)
    after = c.as_dict()
    assert after["total_dur_ms"] - before["total_dur_ms"] == 400
    assert after["total_prompt_chars"] - before["total_prompt_chars"] == 3000

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

"""3.5 compare 任务队列进度 + apply 快照落盘单测。

不依赖 HTTP/SSE/svn:直接测 parallel_map_tables progress_cb、_compare_task_emit
状态流转、apply 快照往返。
"""
from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

from engine.parallel_compare import parallel_map_tables
from engine.models import MergeRequest


# ── parallel_map_tables progress_cb ────────────────────────────────

def test_parallel_map_tables_progress_serial():
    """单表/串行路径:progress_cb 每表回调,终态 (total,total)。"""
    def worker(gn):
        return (gn, gn.upper())
    events = []
    out = parallel_map_tables(worker, ["a", "b", "c"],
                              progress_cb=lambda d, t: events.append((d, t)))
    assert out == [("a", "A"), ("b", "B"), ("c", "C")]
    assert events[-1] == (3, 3)
    assert all(d <= t for d, t in events)


def test_parallel_map_tables_no_progress_cb_ok():
    """progress_cb 缺省 → 不报错,结果正确。"""
    out = parallel_map_tables(lambda gn: (gn, gn), ["x", "y"])
    assert out == [("x", "x"), ("y", "y")]


def test_parallel_map_tables_progress_cb_exception_swallowed():
    """progress_cb 抛异常 → 不影响主流程。"""
    def bad_cb(d, t):
        raise RuntimeError("boom")
    out = parallel_map_tables(lambda gn: (gn, gn), ["a", "b"], progress_cb=bad_cb)
    assert out == [("a", "a"), ("b", "b")]


# ── compare 任务队列 _compare_task_emit ────────────────────────────

def test_compare_task_emit_progress_and_done():
    from routers.merge_branch import _compare_task_emit, _COMPARE_TASKS, _COMPARE_TASKS_LOCK
    tid = uuid.uuid4().hex
    with _COMPARE_TASKS_LOCK:
        _COMPARE_TASKS[tid] = {"status": "running", "events": [],
                               "result": None, "error": ""}
    _compare_task_emit(tid, {"type": "progress", "phase": "compare_tables",
                             "done": 1, "total": 3})
    _compare_task_emit(tid, {"type": "done", "result": {"groups": {}}})
    with _COMPARE_TASKS_LOCK:
        t = _COMPARE_TASKS[tid]
    assert t["status"] == "done"
    assert t["result"] == {"groups": {}}
    assert len(t["events"]) == 2
    # 清理
    with _COMPARE_TASKS_LOCK:
        _COMPARE_TASKS.pop(tid, None)


def test_compare_task_emit_error_sets_terminal():
    from routers.merge_branch import _compare_task_emit, _COMPARE_TASKS, _COMPARE_TASKS_LOCK
    tid = uuid.uuid4().hex
    with _COMPARE_TASKS_LOCK:
        _COMPARE_TASKS[tid] = {"status": "running", "events": [],
                               "result": None, "error": ""}
    _compare_task_emit(tid, {"type": "error", "error": "boom"})
    with _COMPARE_TASKS_LOCK:
        t = _COMPARE_TASKS[tid]
    assert t["status"] == "error"
    assert t["error"] == "boom"
    with _COMPARE_TASKS_LOCK:
        _COMPARE_TASKS.pop(tid, None)


def test_compare_task_emit_unknown_task_noop():
    from routers.merge_branch import _compare_task_emit
    # 未知 task_id → 不报错
    _compare_task_emit("nonexistent", {"type": "progress", "phase": "x"})


# ── apply 快照落盘往返 ─────────────────────────────────────────────

def test_apply_snapshot_persist_and_clear(tmp_path, monkeypatch):
    from routers import merge_stages
    monkeypatch.setattr(merge_stages, "APPLY_SNAPSHOT_DIR", tmp_path / "_apply_snapshots")
    req = MergeRequest(group_name="grp1")
    merge_stages._persist_apply_snapshot("grp1", req)
    p = merge_stages._apply_snapshot_path("grp1")
    assert p.is_file()
    # 内容是 req 的 json
    import json
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["group_name"] == "grp1"
    # 清理
    merge_stages._clear_apply_snapshot("grp1")
    assert not p.is_file()


def test_apply_snapshot_clear_missing_noop(tmp_path, monkeypatch):
    from routers import merge_stages
    monkeypatch.setattr(merge_stages, "APPLY_SNAPSHOT_DIR", tmp_path / "_apply_snapshots")
    # 不存在的快照清理不报错
    merge_stages._clear_apply_snapshot("never_existed")

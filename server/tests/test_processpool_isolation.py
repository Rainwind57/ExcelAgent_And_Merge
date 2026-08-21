"""4.4 worker 模块隔离 + 4.5 ProcessPool 启用单测。

4.4:子进程导入 merge_branch/merge_subdir/diff/merge_stages 不触 agent 包(避免
ProcessPool spawn 死锁)。
4.5:阈值降到 4 后,>=2 表(测试内 monkeypatch 阈值)走 ProcessPool 真并行,结果正确。
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

import pytest

_SERVER_DIR = str(Path(__file__).resolve().parent.parent)


def _agent_loaded_in_subprocess(import_stmt: str) -> tuple[bool, str]:
    """子进程导入指定模块,返回 (agent 是否被加载, stderr 摘要)。"""
    code = (
        "import sys\n"
        f"{import_stmt}\n"
        "loaded = [m for m in sys.modules if m == 'agent' or m.startswith('agent.')]\n"
        "print('AGENT_LOADED' if loaded else 'AGENT_FREE')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code], cwd=_SERVER_DIR,
        capture_output=True, text=True, timeout=120,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    loaded = "AGENT_LOADED" in (proc.stdout or "")
    return loaded, out


@pytest.mark.parametrize("mod,stmt", [
    ("routers.merge_branch", "from routers import merge_branch"),
    ("routers.merge_subdir", "from routers import merge_subdir"),
    ("routers.diff", "from routers import diff"),
    ("routers.merge_stages", "from routers import merge_stages"),
])
def test_worker_modules_agent_free(mod, stmt):
    """4.4:worker 宿主模块导入不触发 agent 包(ProcessPool spawn 安全)。"""
    loaded, out = _agent_loaded_in_subprocess(stmt)
    assert not loaded, f"{mod} 导入触发了 agent 包(子进程会死锁):\n{out[-800:]}"


def test_processpool_path_runs_and_returns(monkeypatch):
    """4.5:阈值低时走 ProcessPool,结果正确,不崩溃。"""
    from engine import parallel_compare
    from routers._pp_smoke_worker import smoke_worker

    monkeypatch.setattr(parallel_compare, "_PROCESS_THRESHOLD", 2)
    monkeypatch.setattr(parallel_compare, "_MAX_PROCESS_WORKERS", 2)

    attempted = {"pp": False}
    orig_pp = parallel_compare.ProcessPoolExecutor

    class SpyPP(orig_pp):
        def __init__(self, *a, **kw):
            attempted["pp"] = True
            super().__init__(*a, **kw)

    monkeypatch.setattr(parallel_compare, "ProcessPoolExecutor", SpyPP)

    out = parallel_compare.parallel_map_tables(smoke_worker, ["a", "b", "c", "d"])
    assert sorted(out) == [("a", "A"), ("b", "B"), ("c", "C"), ("d", "D")]
    assert attempted["pp"], "阈值=2 + 4 表应尝试 ProcessPool"


def test_processpool_fallback_to_threadpool_on_failure(monkeypatch):
    """4.5:ProcessPool 失败自动回退 ThreadPool,结果仍正确(已有兼容机制)。"""
    from engine import parallel_compare
    from routers._pp_smoke_worker import smoke_worker

    monkeypatch.setattr(parallel_compare, "_PROCESS_THRESHOLD", 2)

    class BoomPP:
        def __init__(self, *a, **kw):
            raise RuntimeError("模拟 ProcessPool 不可用")
        def __enter__(self): return self
        def __exit__(self, *a): return False

    monkeypatch.setattr(parallel_compare, "ProcessPoolExecutor", BoomPP)

    out = parallel_compare.parallel_map_tables(smoke_worker, ["a", "b", "c"])
    assert sorted(out) == [("a", "A"), ("b", "B"), ("c", "C")]

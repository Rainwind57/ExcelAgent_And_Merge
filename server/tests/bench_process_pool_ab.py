"""#32 真实 A/B bench：demo_svn 26 表 branch_compare。

ProcessPool(after #32) vs ThreadPool(before #32)，用 MERGE_PROCESS_THRESHOLD
切换。同时统计冲突格数（#24 语义归一后）。

用法: python tests/bench_process_pool_ab.py
"""
from __future__ import annotations

import os
import sys
import time
import statistics
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from routers import merge_branch
from routers.merge_branch import branch_compare, BranchCompareRequest
import engine.parallel_compare as pc

DEMO_SVN_WC = SERVER_DIR.parent / "merge" / "svn" / "demo_svn" / "wc"


def _count_conflicts(resp):
    total = 0
    for gn, fg in resp.groups.items():
        for sk, s in fg.sheets.items():
            total += s.stats.get("conflicts", 0)
    return total


def _run_once(req):
    merge_branch._DIRS_CACHE["data"] = None
    merge_branch._DIRS_CACHE["ts"] = 0.0
    t0 = time.perf_counter()
    resp = branch_compare(req)
    return (time.perf_counter() - t0) * 1000, resp


def _bench(req, runs, label):
    samples, conflicts, n_tables = [], None, None
    for _ in range(runs):
        ms, resp = _run_once(req)
        samples.append(ms)
        if conflicts is None:
            conflicts = _count_conflicts(resp)
            n_tables = len(resp.groups)
    med = statistics.median(samples) if samples else 0.0
    print(f"  {label}: {med:.0f}ms (runs={runs}, tables={n_tables}, conflicts={conflicts})",
          flush=True)
    return med, conflicts, n_tables


def main():
    src = str(DEMO_SVN_WC / "branches" / "dev1")
    tgt = str(DEMO_SVN_WC / "trunk")
    req = BranchCompareRequest(direction="absorb", source_branch=src, target_branch=tgt)
    runs = 2

    print("=" * 60, flush=True)
    print(f"#32 A/B: branch_compare dev1->trunk (runs={runs})", flush=True)
    print("=" * 60, flush=True)

    # after #32: ProcessPool (threshold=4, 26 表 >= 4 → ProcessPool)
    pc._PROCESS_THRESHOLD = 4
    t_proc, conf_proc, n = _bench(req, runs, "ProcessPool(after #32)")

    # before #32: ThreadPool (threshold=9999 → 强制 ThreadPool)
    pc._PROCESS_THRESHOLD = 9999
    t_thread, conf_thread, _ = _bench(req, runs, "ThreadPool (before #32)")

    speedup = t_thread / t_proc if t_proc > 0 else 0.0
    print(f"\n  before(ThreadPool): {t_thread:.0f}ms", flush=True)
    print(f"  after (ProcessPool): {t_proc:.0f}ms", flush=True)
    print(f"  加速比: {speedup:.2f}x", flush=True)
    print(f"  冲突格数(#24 语义归一后): {conf_proc}", flush=True)
    print(f"  两模式冲突数一致: {conf_proc == conf_thread}", flush=True)

    # 写报告
    rep = TESTS_DIR / "reports" / "bench_process_pool_ab_latest.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    import json
    rep.write_text(json.dumps({
        "tables": n, "runs": runs,
        "threadpool_ms_median": round(t_thread, 1),
        "processpool_ms_median": round(t_proc, 1),
        "speedup": round(speedup, 2),
        "conflicts_after_24": conf_proc,
        "conflict_count_match": conf_proc == conf_thread,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告: {rep}", flush=True)


if __name__ == "__main__":
    main()

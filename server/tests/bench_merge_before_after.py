"""#24/#33 真实 A/B bench：demo_svn 74 表 branch_compare（ThreadPool）。

A/B 开关：
  - MERGE_DISABLE_NO_CHANGE_SKIP=1 → 禁用 #33 跳过（before #33，全 74 表跑 compare）
  - 默认 → 启用 #33 跳过（after #33，未变更表跳过）
  - MERGE_PROCESS_THRESHOLD=9999 → 强制 ThreadPool（#32 回退态，避免 ProcessPool worker 导入卡死）

#24 效果：冲突格数（语义归一后）。
#33 效果：no_change 表数 + 总耗时对比。

用法: python tests/bench_merge_before_after.py
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

# 强制 ThreadPool（#32 ProcessPool 在 demo_svn worker 导入重，回退 ThreadPool）
os.environ["MERGE_PROCESS_THRESHOLD"] = "9999"

from routers import merge_branch
from routers.merge_branch import branch_compare, BranchCompareRequest

DEMO_SVN_WC = SERVER_DIR.parent / "merge" / "svn" / "demo_svn" / "wc"


def _count(resp):
    conf = sum(s.stats.get("conflicts", 0) for fg in resp.groups.values() for s in fg.sheets.values())
    nc = sum(1 for fg in resp.groups.values() if fg.structural_status == "no_change")
    return conf, nc, len(resp.groups)


def _run(req):
    merge_branch._DIRS_CACHE["data"] = None
    merge_branch._DIRS_CACHE["ts"] = 0.0
    t0 = time.perf_counter()
    resp = branch_compare(req)
    return (time.perf_counter() - t0) * 1000, resp


def _bench(req, runs, label, disable_skip):
    os.environ["MERGE_DISABLE_NO_CHANGE_SKIP"] = "1" if disable_skip else "0"
    samples, info = [], None
    for _ in range(runs):
        ms, resp = _run(req)
        samples.append(ms)
        if info is None:
            conf, nc, n = _count(resp)
            info = (conf, nc, n)
    med = statistics.median(samples)
    conf, nc, n = info
    print(f"  {label}: {med:.0f}ms (tables={n}, no_change={nc}, conflicts={conf})", flush=True)
    return med, conf, nc, n


def main():
    src = str(DEMO_SVN_WC / "branches" / "dev1")
    tgt = str(DEMO_SVN_WC / "trunk")
    req = BranchCompareRequest(direction="absorb", source_branch=src, target_branch=tgt)
    runs = 1  # runs=2 内存累积(74表 CompareResponse 不释放)，1 轮已够 A/B

    print("=" * 64, flush=True)
    print(f"merge A/B: branch_compare dev1->trunk (runs={runs}, ThreadPool)", flush=True)
    print("=" * 64, flush=True)

    # before #33: 禁用 skip（全 74 表跑 compare_sheet）
    t_before, conf_before, nc_before, n = _bench(req, runs, "before #33(skip禁用)", True)

    # after #33: 启用 skip（未变更表跳过）+ #24 语义归一
    t_after, conf_after, nc_after, _ = _bench(req, runs, "after  #33+#24(skip启用)", False)

    speedup = t_before / t_after if t_after > 0 else 0.0
    print(f"\n  before(全表compare): {t_before:.0f}ms, no_change=0, conflicts={conf_before}", flush=True)
    print(f"  after (skip+归一):   {t_after:.0f}ms, no_change={nc_after}, conflicts={conf_after}", flush=True)
    print(f"  #33 加速比: {speedup:.2f}x (跳过 {nc_after}/{n} 表)", flush=True)
    print(f"  #24 冲突格: {conf_before}(skip禁用,含假冲突) vs {conf_after}(归一后)", flush=True)

    rep = TESTS_DIR / "reports" / "bench_merge_before_after_latest.json"
    rep.parent.mkdir(parents=True, exist_ok=True)
    import json
    rep.write_text(json.dumps({
        "tables": n, "runs": runs,
        "before_ms_median": round(t_before, 1),
        "after_ms_median": round(t_after, 1),
        "speedup_33": round(speedup, 2),
        "no_change_tables": nc_after,
        "conflicts_before_24": conf_before,
        "conflicts_after_24": conf_after,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n  报告: {rep}", flush=True)


if __name__ == "__main__":
    main()

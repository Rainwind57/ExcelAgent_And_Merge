"""eval_baseline：eval run 版本归档 + 跨 run diff 对比 + 归档清理。

capability: eval-baseline-management

提供：
  - make_run_id(tag/run_id)：生成 YYYYMMDD_HHMMSS_<gitshort> 格式 run_id
  - archive_run(script, result, report_md, tag, run_id)：归档到 reports/archive/<script>_<run_id>.{json,md}
  - load_archived(script, run_id)：读归档
  - compare_runs(baseline_run_id, current_run_id, script)：白名单指标 delta + status
  - format_diff_report(diff)：markdown 报告
  - prune_archives(keep, script)：保留每脚本最近 N 个归档

CLI: python -m tests.eval_baseline --compare <id1> <id2> [--script s] [--prune-keep N]
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Optional

TESTS_DIR = Path(__file__).resolve().parent
ARCHIVE_DIR = TESTS_DIR / "reports" / "archive"

# 指标白名单 + 方向（higher_better=值越大越好；lower_better=值越小越好）
METRIC_WHITELIST: dict[str, dict] = {
    "ok_rate": {"direction": "higher_better"},
    "strict_pass": {"direction": "higher_better"},
    "avg_elapsed_ms": {"direction": "lower_better"},
    "coverage": {"direction": "higher_better"},
    "locate_rate": {"direction": "higher_better"},
    "field_accuracy": {"direction": "higher_better"},
}
ERROR_TYPE_METRIC = "error_type_distribution"

# run_id 正则：script_<YYYYMMDD>_<HHMMSS>_<git>([_<tag>])
_RUN_ID_RE = re.compile(r"^(.+?)_(\d{8}_\d{6}_\w+(?:_.+)?)$")


def _git_short() -> str:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return "nogit"


def make_run_id(tag: Optional[str] = None, run_id: Optional[str] = None) -> str:
    """生成 run_id：YYYYMMDD_HHMMSS_<gitshort>[_<tag>]。git 不可用 fallback nogit。"""
    if run_id:
        return run_id
    rid = f"{time.strftime('%Y%m%d_%H%M%S')}_{_git_short()}"
    if tag:
        rid = f"{rid}_{tag}"
    return rid


def archive_run(script: str, result: dict, report_md: str = "",
                tag: Optional[str] = None, run_id: Optional[str] = None) -> str:
    """归档一次 eval run 到 <script>_<run_id>.{json,md}，返回 run_id。"""
    rid = make_run_id(tag=tag, run_id=run_id)
    ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
    header = {
        "run_id": rid,
        "git_hash": _git_short(),
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "script": script,
        "case_count": len(result.get("cases", [])) if isinstance(result, dict) else 0,
    }
    payload = {"_header": header, "results": result}
    (ARCHIVE_DIR / f"{script}_{rid}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (ARCHIVE_DIR / f"{script}_{rid}.md").write_text(report_md, encoding="utf-8")
    return rid


def load_archived(script: str, run_id: str) -> Optional[dict]:
    p = ARCHIVE_DIR / f"{script}_{run_id}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _status(direction: str, delta) -> str:
    if delta is None or delta == 0:
        return "neutral"
    improved = (delta > 0) if direction == "higher_better" else (delta < 0)
    return "improved" if improved else "regressed"


def compare_runs(baseline_run_id: str, current_run_id: str, script: str) -> dict:
    """对白名单指标计算 delta + status（improved/regressed/neutral）。"""
    base = load_archived(script, baseline_run_id) or {}
    curr = load_archived(script, current_run_id) or {}
    base_r = base.get("results", {}) if isinstance(base, dict) else {}
    curr_r = curr.get("results", {}) if isinstance(curr, dict) else {}
    base_sum = base_r.get("summary", {}) if isinstance(base_r, dict) else {}
    curr_sum = curr_r.get("summary", {}) if isinstance(curr_r, dict) else {}

    diffs = []
    for metric, cfg in METRIC_WHITELIST.items():
        b = base_sum.get(metric)
        c = curr_sum.get(metric)
        if b is None or c is None:
            diffs.append({"metric": metric, "baseline": b, "current": c,
                          "delta": None, "direction": cfg["direction"], "status": "neutral"})
            continue
        delta = c - b
        diffs.append({"metric": metric, "baseline": b, "current": c,
                      "delta": delta, "direction": cfg["direction"],
                      "status": _status(cfg["direction"], delta)})

    # error_type_distribution：按 error_type 细分，count 方向 lower_better
    b_etd = base_r.get(ERROR_TYPE_METRIC, {}) if isinstance(base_r, dict) else {}
    c_etd = curr_r.get(ERROR_TYPE_METRIC, {}) if isinstance(curr_r, dict) else {}
    if isinstance(b_etd, dict) and isinstance(c_etd, dict) and (b_etd or c_etd):
        etd_delta = []
        for et in sorted(set(list(b_etd.keys()) + list(c_etd.keys()))):
            b = b_etd.get(et, 0)
            c = c_etd.get(et, 0)
            d = c - b
            etd_delta.append({"error_type": et, "baseline": b, "current": c,
                              "delta": d, "status": _status("lower_better", d)})
        diffs.append({"metric": ERROR_TYPE_METRIC, "baseline": b_etd, "current": c_etd,
                      "delta": etd_delta, "direction": "lower_better",
                      "status": _status("lower_better", sum(e["delta"] for e in etd_delta))})

    return {"baseline_run_id": baseline_run_id, "current_run_id": current_run_id,
            "script": script, "diffs": diffs}


def format_diff_report(diff: dict) -> str:
    lines = [f"# Diff 报告: {diff.get('script', '')}", "",
             f"- baseline: {diff.get('baseline_run_id')}",
             f"- current: {diff.get('current_run_id')}", "",
             "| 指标 | baseline | current | delta | 方向 | 状态 |",
             "|---|---|---|---|---|---|"]
    for d in diff.get("diffs", []):
        m = d["metric"]
        if m == ERROR_TYPE_METRIC and isinstance(d.get("delta"), list):
            for e in d["delta"]:
                lines.append(f"| {m}/{e['error_type']} | {e['baseline']} | {e['current']} | "
                             f"{e['delta']:+d} | lower_better | {e['status']} |")
        else:
            delta = d.get("delta")
            if delta is None:
                delta_s = "-"
            elif isinstance(delta, (int, float)):
                delta_s = f"{delta:+}"
            else:
                delta_s = str(delta)
            lines.append(f"| {m} | {d['baseline']} | {d['current']} | {delta_s} | "
                         f"{d['direction']} | {d['status']} |")
    return "\n".join(lines) + "\n"


def prune_archives(keep: int = 5, script: Optional[str] = None) -> int:
    """保留每个脚本最近 N 个归档（按 run_id 时间戳降序），返回删除数。"""
    if not ARCHIVE_DIR.exists():
        return 0
    by_script: dict[str, list[Path]] = {}
    for p in ARCHIVE_DIR.glob("*.json"):
        m = _RUN_ID_RE.match(p.stem)
        s = m.group(1) if m else p.stem
        by_script.setdefault(s, []).append(p)
    if script is not None:
        by_script = {script: by_script.get(script, [])}
    deleted = 0
    for s, fl in by_script.items():
        fl.sort(key=lambda p: p.name, reverse=True)  # run_id 时间戳降序
        for p in fl[keep:]:
            p.unlink(missing_ok=True)
            p.with_suffix(".md").unlink(missing_ok=True)
            deleted += 1
    return deleted


def main() -> int:
    ap = argparse.ArgumentParser(description="eval_baseline 归档/diff/清理")
    ap.add_argument("--compare", nargs=2, metavar=("BASE", "CURR"), help="对比两个 run_id")
    ap.add_argument("--script", default="table_case_eval", help="脚本名")
    ap.add_argument("--prune-keep", type=int, default=0, help="保留最近 N 个归档（0=不清理）")
    args = ap.parse_args()

    if args.compare:
        diff = compare_runs(args.compare[0], args.compare[1], args.script)
        print(format_diff_report(diff))
    if args.prune_keep > 0:
        n = prune_archives(keep=args.prune_keep)
        print(f"已清理 {n} 个归档")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

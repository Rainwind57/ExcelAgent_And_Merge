from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path
from difflib import SequenceMatcher

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from agent.excel.locator.fuzzy_matcher import char_overlap, levenshtein_ratio  # noqa: E402
from rapidfuzz import fuzz as rfuzz  # noqa: E402

INDEX = ROOT / "server" / "agent" / "excel" / "_table_index.json"
OUT_JSON = ROOT / "bench" / "ppt_rapidfuzz_metrics.json"
OUT_MD = ROOT / "bench" / "ppt_rapidfuzz_metrics.md"

QUERIES = [
    ("活动类型", "活动类型"),
    ("目标类型", "任务目标类型"),
    ("对话内容", "对话内容"),
    ("选项内容", "选项内容"),
    ("神通描述", "神通描述"),
    ("灵兽名称", "灵兽名称"),
    ("物品编号", "物品编号"),
    ("品质", "品质"),
    ("技能等级", "技能等级"),
    ("坐标", "坐标"),
    ("模型id", "模板ID"),
    ("交互id", "交互id"),
    ("奖励id", "奖励ID"),
    ("任务id", "任务ID"),
]


def clean(s: str) -> str:
    return str(s or "").strip().lower().replace("（", "(").replace("）", ")")


def baseline_score(q: str, h: str) -> float:
    q, h = clean(q), clean(h)
    return SequenceMatcher(None, q, h).ratio() * 0.6 + char_overlap(q, h) * 0.4


def rapid_score(q: str, h: str) -> float:
    q, h = clean(q), clean(h)
    wr = rfuzz.WRatio(q, h)
    ts = rfuzz.token_set_ratio(q, h)
    pr = rfuzz.partial_ratio(q, h)
    return (wr * 0.5 + ts * 0.3 + pr * 0.2) / 100.0


def load_headers() -> list[str]:
    data = json.loads(INDEX.read_text(encoding="utf-8", errors="ignore"))
    headers = []
    for book in data:
        for sheet in book.get("sheets") or []:
            for h in sheet.get("header_names") or sheet.get("headers") or []:
                if h and str(h).strip():
                    headers.append(str(h).strip())
    return sorted(set(headers))


def bench_accuracy(fn, headers: list[str]) -> dict:
    rows = []
    times = []
    for q, expected in QUERIES:
        t0 = time.perf_counter()
        scored = [(fn(q, h), h) for h in headers]
        scored.sort(reverse=True)
        dt = (time.perf_counter() - t0) * 1000
        times.append(dt)
        top = [h for _, h in scored[:5]]
        hit_at = next((i + 1 for i, h in enumerate(top) if expected == h), None)
        rows.append({"query": q, "expected": expected, "top1": top[0] if top else "", "top5": top, "hit_at": hit_at, "ms": dt})
    top1 = sum(1 for r in rows if r["hit_at"] == 1)
    top5 = sum(1 for r in rows if r["hit_at"] is not None)
    return {
        "top1_hits": top1,
        "top5_hits": top5,
        "total": len(rows),
        "top1_rate": round(top1 / len(rows), 4),
        "top5_rate": round(top5 / len(rows), 4),
        "avg_ms": round(statistics.mean(times), 4),
        "rows": rows,
    }


def bench_edit_speed(headers: list[str], repeat: int = 20) -> dict:
    pairs = [(q, h) for q, _ in QUERIES for h in headers] * repeat
    t0 = time.perf_counter()
    for q, h in pairs:
        levenshtein_ratio(q, h)
    py_ms = (time.perf_counter() - t0) * 1000
    t0 = time.perf_counter()
    for q, h in pairs:
        rfuzz.ratio(q, h) / 100.0
    rf_ms = (time.perf_counter() - t0) * 1000
    return {
        "pairs": len(pairs),
        "python_levenshtein_ms": round(py_ms, 2),
        "rapidfuzz_ratio_ms": round(rf_ms, 2),
        "speedup": round(py_ms / rf_ms, 1),
    }


def main() -> None:
    headers = load_headers()
    base = bench_accuracy(baseline_score, headers)
    rapid = bench_accuracy(rapid_score, headers)
    speed = bench_edit_speed(headers)
    out = {
        "note": "PPT 补充模拟压测：真实 _table_index.json 列名集合；accuracy 为 TopK 命中，speed 为编辑距离算子微基准。",
        "header_count": len(headers),
        "query_count": len(QUERIES),
        "baseline_cosine_jaccard": base,
        "rapidfuzz_mixed": rapid,
        "edit_distance_speed": speed,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# rapidfuzz 混合指标模拟压测",
        "",
        "> 说明：真实 `_table_index.json` 列名集合；Accuracy 对比 TopK 命中，Speed 对比纯 Python Levenshtein 与 rapidfuzz C++ 算子。",
        "",
        "## 总览",
        "",
        "| 指标 | baseline | rapidfuzz 混合 | 变化 |",
        "|---|---:|---:|---:|",
        f"| Top1 命中 | {base['top1_hits']}/{base['total']} ({base['top1_rate']:.2%}) | {rapid['top1_hits']}/{rapid['total']} ({rapid['top1_rate']:.2%}) | {round((rapid['top1_rate'] - base['top1_rate']) * 100, 2)}pts |",
        f"| Top5 命中 | {base['top5_hits']}/{base['total']} ({base['top5_rate']:.2%}) | {rapid['top5_hits']}/{rapid['total']} ({rapid['top5_rate']:.2%}) | {round((rapid['top5_rate'] - base['top5_rate']) * 100, 2)}pts |",
        f"| 编辑距离算子 | {speed['python_levenshtein_ms']}ms | {speed['rapidfuzz_ratio_ms']}ms | {speed['speedup']}× |",
        "",
        "## rapidfuzz 公式",
        "",
        "`score = WRatio × 0.5 + token_set_ratio × 0.3 + partial_ratio × 0.2`",
        "",
        "## PPT 口径",
        "",
        "- 准确率收益来自混合指标增强排序，不单独保证每个 query 都优于 baseline。",
        "- 性能收益来自 rapidfuzz C++ 编辑距离算子，避免 Python O(n*m) 动态规划成为热点。",
    ]
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()

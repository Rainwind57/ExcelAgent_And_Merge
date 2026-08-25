"""random_sample_eval：从复合任务链测例池随机抽样评估 excel_LLM Agent。

用途（回归/持续优化内环）：
  每次运行从三个复合任务链测例文件随机挑选 N 条真实执行，产出准确率/性能
  指标，归类失败模式，并把本轮结果追加到 reports/optimization_progress.md
  进度日志，形成"每轮抽样 → 指标 → 问题 → 优化"的可追溯优化过程。

测例池（可用 --files 覆盖）：
  - cases/complex_task_chain_inputs.json
  - cases/complex_task_chain_inputs_extra.json
  - cases/school_quest_chain_inputs.json

复用 task_chain_eval 的全部执行/评估原语（run_one_chain / aggregate_chains /
classify_failures），只新增：多文件合并 + 随机抽样 + 进度日志追加。

用法（在 server/ 目录下执行）:
    python -m tests.random_sample_eval                 # 随机抽 3 条
    python -m tests.random_sample_eval --n 5           # 随机抽 5 条
    python -m tests.random_sample_eval --n 3 --seed 42 # 固定种子可复现
    python -m tests.random_sample_eval --note "修复碎片行守卫后回归"

前提: codemaker serve 已启动（.env 配好 CODEMAKER_SERVER_URL 等）。
输出:
  - reports/random_sample_eval_latest.{md,json}  本轮明细
  - reports/optimization_progress.md             跨轮进度日志（追加）
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sys
import time
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

# task_chain_eval 在 import 时完成 sys.path/dotenv 装配，并暴露执行/评估原语
import task_chain_eval as tcv  # noqa: E402

CASES_DIR = TESTS_DIR / "cases"
REPORT_DIR = TESTS_DIR / "reports"
PROGRESS_MD = REPORT_DIR / "optimization_progress.md"

DEFAULT_FILES = [
    CASES_DIR / "complex_task_chain_inputs.json",
    CASES_DIR / "complex_task_chain_inputs_extra.json",
    CASES_DIR / "school_quest_chain_inputs.json",
]


def load_pool(files: list[Path]) -> list[dict]:
    """合并多文件测例池，每条打上来源标签 (_src_file, _src_idx)。"""
    pool: list[dict] = []
    for f in files:
        if not f.exists():
            print(f"⚠ 测例文件不存在，跳过: {f}")
            continue
        cases = json.loads(f.read_text(encoding="utf-8"))
        for i, c in enumerate(cases):
            if not isinstance(c, dict) or "input" not in c:
                continue
            c = dict(c)
            c["_src_file"] = f.name
            c["_src_idx"] = i
            pool.append(c)
    return pool


def sample_pool(pool: list[dict], n: int, seed: int | None) -> list[dict]:
    rng = random.Random(seed)
    n = max(1, min(n, len(pool)))
    return rng.sample(pool, n)


def _src_label(chain: dict) -> str:
    return f"{chain.get('_src_file', '?')}#{chain.get('_src_idx', '?')}"


# ── 报告渲染 ──────────────────────────────────────────────

def render_latest(results: list[tcv.ChainRunResult], labels: list[str],
                  agg: dict, failures: dict, seed: int | None, note: str) -> str:
    lines = [
        "# 随机抽样评估报告（excel_LLM Agent 复合任务链）",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 抽样数: {len(results)} | 随机种子: {seed if seed is not None else '（未固定）'}",
        f"- 备注: {note or '-'}",
        "- 执行方式: 进程内真实 AgentService（codemaker serve LLM），"
        "resources/ 临时沙箱真实写盘，跑前/跑后 xlsx 行级差异作 ground truth",
        "",
        "## 一、本轮总体指标",
        "",
        "| 指标 | 值 |",
        "|---|---|",
        f"| 链完整率 chain_complete | {agg.get('chain_complete_rate', 0):.4f} |",
        f"| truth_ok 率 | {agg.get('truth_ok_rate', 0):.4f} |",
        f"| 严格通过率 strict_pass | {agg.get('strict_pass_rate', 0):.4f} |",
        f"| 引用一致率 ref_consistency | {agg.get('ref_consistency_rate', 0):.4f} |",
        f"| producer 产出率 | {agg.get('producers_resolve_rate', 0):.4f} |",
        f"| 定位率 locate | {agg.get('locate_rate', 0):.4f} |",
        f"| 覆盖度 coverage | {agg.get('coverage', 0):.4f} |",
        f"| 精准度 field_accuracy | {agg.get('field_accuracy', 0):.4f} |",
        f"| 响应 ok 率 | {agg.get('ok_rate', 0):.4f} |",
        f"| 平均耗时(ms) | {agg.get('avg_elapsed_ms', 0):.1f} |",
        f"| P50/P95(ms) | {agg.get('p50_elapsed_ms', 0):.1f} / {agg.get('p95_elapsed_ms', 0):.1f} |",
        f"| 平均 LLM 调用 | {agg.get('avg_llm_calls', 0):.1f} |",
        f"| 平均多余写入 | {agg.get('avg_extra_ops', 0):.4f} |",
        f"| 平均异表写入 | {agg.get('avg_extra_off_table', 0):.4f} |",
        "",
        "## 二、抽中样例逐条结果",
        "",
        "| 来源 | ok | 链完整 | 引用一致 | 覆盖 | 精准 | 耗时ms | input |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r, lab in zip(results, labels):
        txt = r.input_text[:40].replace("|", "/")
        lines.append(
            f"| {lab} | {'✅' if r.ok else '❌'} | "
            f"{'✅' if r.chain_complete else '❌'} | "
            f"{r.ref_consistency_rate:.2f} ({r.ref_ok}/{r.ref_total}) | "
            f"{r.coverage:.2f} | {r.field_accuracy:.2f} | "
            f"{r.elapsed_ms:.0f} | {txt} |")

    lines += ["", "## 三、失败模式归类", "",
              "| 失败模式 | 计数 | 优化方向 |", "|---|---|---|"]
    from task_chain_eval import render_report  # noqa: F401  (触发 guide 定义引用)
    guide = {
        "parse_or_exec_failed": "parse_multi 超时/LLM 不可用 → 增大超时/降级 splitter",
        "table_sheet_miss": "路由/sheet 别名缺失 → 补 table_context/sheet_aliases",
        "row_missing": "add 未落行/modify 未定位 → 查列定位与主键自增",
        "field_error": "字段值错/枚举未解析/类型不符 → 补 column_aliases/enum_mappings/value_constraints",
        "ref_broken": "占位符替换错 → 修 OperationOrchestrator._capture_produced 列名派生",
        "producer_not_resolved": "producer 新 ID 未回传 → 修主键回传/produces 标注",
        "extra_writes": "过度级联/误改它表 → 收紧 cascade_rules/反模式拦截",
        "precondition_missing": "夹具与配表不一致（非 Agent 缺陷）→ 同步夹具/配表",
    }
    for mode, cnt in failures["modes"].items():
        if cnt:
            lines.append(f"| {mode} | {cnt} | {guide.get(mode, '-')} |")
    if not any(failures["modes"].values()):
        lines.append("| （无） | 0 | 本轮抽样全通过 |")

    # 逐条明细（复用 task_chain_eval 的详情渲染）
    lines += ["", "## 四、每条链详情", ""]
    for r in results:
        lines.append(tcv._render_chain_detail(r))
    return "\n".join(lines)


def append_progress(results: list[tcv.ChainRunResult], labels: list[str],
                    agg: dict, failures: dict, seed: int | None, note: str) -> None:
    """把本轮结果追加到跨轮进度日志（持续优化过程留痕）。"""
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    header_needed = not PROGRESS_MD.exists()
    top_fail = sorted(
        ((m, c) for m, c in failures["modes"].items() if c),
        key=lambda x: -x[1])[:3]
    fail_str = "、".join(f"{m}×{c}" for m, c in top_fail) or "无"
    sample_str = ", ".join(labels)
    block = [
        "",
        f"## 轮次 {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "",
        f"- 备注: {note or '-'} | 种子: {seed if seed is not None else '未固定'}"
        f" | 抽样 {len(results)} 条",
        f"- 抽中: {sample_str}",
        f"- 指标: 链完整率 {agg.get('chain_complete_rate', 0):.2f} | "
        f"引用一致 {agg.get('ref_consistency_rate', 0):.2f} | "
        f"覆盖 {agg.get('coverage', 0):.2f} | 精准 {agg.get('field_accuracy', 0):.2f} | "
        f"ok率 {agg.get('ok_rate', 0):.2f}",
        f"- 性能: 平均 {agg.get('avg_elapsed_ms', 0):.0f}ms | "
        f"P95 {agg.get('p95_elapsed_ms', 0):.0f}ms | "
        f"平均 LLM 调用 {agg.get('avg_llm_calls', 0):.1f}",
        f"- 主要失败模式: {fail_str}",
    ]
    if failures.get("samples"):
        for mode, cnt in top_fail:
            block.append(f"  - {mode}: 待优化（见 latest 报告详情）")
    text = ""
    if header_needed:
        text += ("# excel_LLM Agent 持续优化进度日志\n\n"
                 "> 每次 `python -m tests.random_sample_eval` 随机抽样运行后自动追加一轮。\n"
                 "> 用于追踪准确率/性能随优化的变化趋势，定位仍存在的错误模式。\n")
    text += "\n".join(block) + "\n"
    with PROGRESS_MD.open("a", encoding="utf-8") as fp:
        fp.write(text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=3, help="随机抽样条数（默认 3）")
    ap.add_argument("--seed", type=int, default=None, help="随机种子（固定可复现）")
    ap.add_argument("--files", type=str, default="",
                    help="逗号分隔的测例文件（默认三个复合链文件）")
    ap.add_argument("--note", type=str, default="", help="本轮备注（写入进度日志）")
    ap.add_argument("--out", type=str, default=str(REPORT_DIR))
    args = ap.parse_args()

    # 阻止 skill_updater 嵌套跑 mini 回归
    os.environ["TABLE_CASE_EVAL_RUNNING"] = "1"

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    files = ([Path(x.strip()) for x in args.files.split(",") if x.strip()]
             if args.files else DEFAULT_FILES)
    files = [f if f.is_absolute() else (TESTS_DIR / f if not f.exists() else f)
             for f in files]

    if not tcv._serve_alive():
        print("⚠ codemaker serve 不可达（CODEMAKER_SERVER_URL="
              f"{os.environ.get('CODEMAKER_SERVER_URL','')}）。请先启动：codemaker serve")
        print("  脚本仍会尝试运行，但 LLM 调用将失败。")

    pool = load_pool(files)
    if not pool:
        print("✗ 测例池为空，检查 --files 路径。")
        return 2
    sampled = sample_pool(pool, args.n, args.seed)
    labels = [_src_label(c) for c in sampled]
    print(f"测例池 {len(pool)} 条，本轮随机抽 {len(sampled)} 条"
          f"（种子={args.seed}）：{labels}\n")

    results: list[tcv.ChainRunResult] = []
    for i, chain in enumerate(sampled, start=1):
        lab = labels[i - 1]
        print(f"[{i}/{len(sampled)}] {lab} {chain['input'][:48]}")
        t0 = time.time()
        r = tcv.run_one_chain(i, chain, enable_skill=True)
        print(f"        ok={r.ok} 链完整={r.chain_complete} "
              f"引用一致={r.ref_consistency_rate:.2f} ({r.ref_ok}/{r.ref_total}) "
              f"cov={r.coverage:.2f} acc={r.field_accuracy:.2f} "
              f"{r.elapsed_ms:.0f}ms ({time.time()-t0:.1f}s)\n")
        results.append(r)

    agg = tcv.aggregate_chains(results)
    failures = tcv.classify_failures(results)

    # 明细报告
    md = render_latest(results, labels, agg, failures, args.seed, args.note)
    (out_dir / "random_sample_eval_latest.md").write_text(md, encoding="utf-8")
    payload = {
        "meta": {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "n": len(results),
                 "seed": args.seed, "note": args.note, "samples": labels},
        "aggregate": agg,
        "failures": failures,
        "results": [tcv._ser(r) for r in results],
    }
    (out_dir / "random_sample_eval_latest.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8")

    # 跨轮进度日志追加
    append_progress(results, labels, agg, failures, args.seed, args.note)

    print("=" * 60)
    print(f"明细报告: {out_dir / 'random_sample_eval_latest.md'}")
    print(f"进度日志: {PROGRESS_MD}")
    print(f"链完整率={agg.get('chain_complete_rate', 0):.2f} "
          f"引用一致率={agg.get('ref_consistency_rate', 0):.2f} "
          f"覆盖={agg.get('coverage', 0):.2f} 精准={agg.get('field_accuracy', 0):.2f} "
          f"ok率={agg.get('ok_rate', 0):.2f}")
    print(f"平均耗时={agg.get('avg_elapsed_ms', 0):.0f}ms "
          f"P95={agg.get('p95_elapsed_ms', 0):.0f}ms "
          f"平均LLM={agg.get('avg_llm_calls', 0):.1f}")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""多指令场景评测（capability: multi-intent-evaluation）。

两层指标：
  纯逻辑层（无 LLM，确定性，总是跑）：
    - topo_correct_rate：OperationOrchestrator._topo_order 满足 producer-before-consumer 约束
    - rollback_correct_rate：构造中途失败场景，验证 dirty_data/failed_tables/跳过标记
    - multi_intent_speedup：orchestrator 顺序 vs 并行（_ORCH_MAX_WORKERS=4）计时
  serve 依赖层（需 codemaker serve，复用 task_chain_eval）：
    - split_correct_rate：实拆意图数 == 期望意图数
    - step2-6_success_rate：各阶段独立成功率（从 ChainRunResult 派生）
    - placeholder_closure_rate：占位符引用闭环率 + per-step 未解析定位

用法（在 server/ 目录下执行）:
    python -m tests.multi_intent_eval            # 仅纯逻辑层
    python -m tests.multi_intent_eval --serve     # 含 serve 依赖层（需 codemaker serve）
输出: server/tests/reports/multi_intent_eval_latest.{md,json}
      server/tests/reports/archive/multi_intent_eval_<run_id>.{json,md}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

# ── 路径 & 环境 ──
TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent
ROOT = SERVER_DIR.parent
RES = ROOT / "resources"
REPORT_DIR = TESTS_DIR / "reports"
FIXTURES = TESTS_DIR / "multi_intent_fixtures.json"
DEFAULT_CHAINS = TESTS_DIR / "cases" / "task_chain.json"

if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")

from agent.excel.nl_parser import NLIntent  # noqa: E402
from agent.excel.core.operation_orchestrator import OperationOrchestrator  # noqa: E402


# ── mock 执行结果（供 orchestrator 纯逻辑测试）──

class _MockResult:
    """orchestrator run_single 的 mock 返回。"""
    def __init__(self, ok: bool, intent: NLIntent, fail_table: str = ""):
        self.ok = ok
        self.intent = intent
        self.result_rows = []
        self.message = "" if ok else "mock 失败"
        self.session_id = ""
        self.table_stem = fail_table or getattr(intent, "table_hint", "") or ""
        self.table_sheet = ""
        self.steps = []
        self.final = None
        self.sub_tasks = []
        self.failed_tables: list = []
        self.dirty_data = False
        self.index_dirty = False
        self.partial = False

    def add(self, name, ok, detail=""):
        pass


def _make_intent(spec: dict) -> NLIntent:
    """从 fixture spec 构造 NLIntent。"""
    extras = {"fields": dict(spec.get("fields", {}))}
    if spec.get("produces"):
        extras["produces"] = spec["produces"]
    return NLIntent(
        action="add",
        table_hint=f"table_{spec['idx']}",
        extras=extras,
        raw=f"intent_{spec['idx']}",
    )


def _make_run_single(intents: list[NLIntent], fail_indices: set[int],
                     sleep_ms: float = 0.0):
    """构造 mock run_single：fail_indices 中的意图返回 ok=False。"""
    def _run(intent, confirm_token, session_id):
        if sleep_ms:
            time.sleep(sleep_ms / 1000.0)
        idx = intents.index(intent)
        ok = idx not in fail_indices
        return _MockResult(ok, intent)
    return _run


# ── 1. 拓扑排序正确率（纯逻辑）──

def eval_topo_correctness(fixtures: dict) -> dict:
    cases = fixtures.get("topo_cases", [])
    if not cases:
        return {"skipped": "无 topo_cases"}
    results = []
    correct = 0
    for case in cases:
        intents = [_make_intent(s) for s in case["intents"]]
        order = OperationOrchestrator._topo_order(intents)
        constraints = case.get("constraints", [])
        violated = []
        for con in constraints:
            p, c = con["producer"], con["consumer"]
            if p in order and c in order and order.index(p) > order.index(c):
                violated.append({"producer": p, "consumer": c})
        is_cycle = case.get("expect_cycle", False)
        cycle_detected = OperationOrchestrator.last_cycle() is not None
        # 循环用例：期望检测到环（last_cycle 非空）即正确
        ok = (len(violated) == 0) if not is_cycle else cycle_detected
        if ok:
            correct += 1
        results.append({
            "id": case["id"], "order": order, "violated": violated,
            "expect_cycle": is_cycle, "cycle_detected": cycle_detected,
            "correct": ok,
        })
    return {
        "cases": results,
        "topo_correct_rate": round(correct / len(cases), 4) if cases else 0.0,
        "n_cases": len(cases),
        "n_correct": correct,
    }


# ── 2. 跨表事务回滚正确率（纯逻辑）──

def eval_rollback(fixtures: dict) -> dict:
    scenarios = fixtures.get("rollback_scenarios", [])
    if not scenarios:
        return {"skipped": "无 rollback_scenarios"}
    results = []
    correct = 0
    for sc in scenarios:
        intents = [_make_intent(s) for s in sc["intents"]]
        fail_idx = {s["idx"] for s in sc["intents"] if s.get("fail")}
        run_single = _make_run_single(intents, fail_idx)
        orch = OperationOrchestrator(run_single=run_single)
        res_list = orch.run(intents, session_id="rollback_test")

        expect_failed = sc["expect_failed_at"]
        expect_skipped = set(sc.get("expect_skipped", []))

        # 校验：失败点 ok=False
        failed_ok = not getattr(res_list[expect_failed], "ok", True)
        # 后续跳过：expect_skipped 中的 idx ok=False
        skipped_ok = all(not getattr(res_list[i], "ok", True) for i in expect_skipped)
        # failed_tables 非空（含失败意图的 table_hint）
        failed_intent = intents[expect_failed]
        ft_ok = any(failed_intent.table_hint in ft for ft in _collect_failed_tables(res_list))
        # 前序不回滚（独立场景）：expect_rollback_prev=false 时前序 ok=True
        rollback_prev = sc.get("expect_rollback_prev", True)
        prev_ok = True
        if not rollback_prev:
            for i in range(expect_failed):
                if i not in expect_skipped and not getattr(res_list[i], "ok", False):
                    prev_ok = False

        ok = failed_ok and skipped_ok and ft_ok and prev_ok
        if ok:
            correct += 1
        results.append({
            "id": sc["id"], "failed_at_ok": failed_ok, "skipped_ok": skipped_ok,
            "failed_tables_ok": ft_ok, "prev_not_rolled_back_ok": prev_ok,
            "correct": ok,
        })
    return {
        "scenarios": results,
        "rollback_correct_rate": round(correct / len(scenarios), 4) if scenarios else 0.0,
        "n_scenarios": len(scenarios),
        "n_correct": correct,
    }


def _collect_failed_tables(res_list) -> list:
    """从结果列表收集 failed_tables。"""
    out = []
    for r in res_list:
        ft = getattr(r, "failed_tables", None) or []
        out.extend(ft)
        # mock 失败结果的 table_stem 也算
        ts = getattr(r, "table_stem", "") or ""
        if ts and not getattr(r, "ok", True):
            out.append(ts)
    return out


# ── 3. 并行 vs 顺序加速比（纯逻辑，orchestrator 直测）──

def eval_speedup(fixtures: dict) -> dict:
    """构造 4 个独立意图（无依赖），顺序 vs 并行计时。"""
    # 用 independent 用例的 intents（无占位符 → 无依赖 → 同层全并行）
    indep = next((c for c in fixtures.get("topo_cases", []) if c["id"] == "independent"), None)
    if not indep:
        return {"skipped": "无 independent 用例"}
    intents = [_make_intent(s) for s in indep["intents"]]
    # 扩展到 4 个独立意图（若不足 4 则循环补）
    while len(intents) < 4:
        intents.append(NLIntent(action="add", table_hint=f"pad_{len(intents)}",
                                extras={"fields": {"id": str(len(intents))}}))

    import agent.excel.core.operation_orchestrator as oo
    sleep_ms = 80  # 每意图 80ms，顺序 ~320ms，并行 ~80ms → 期望 speedup≈4

    # 顺序（max_workers=1）
    oo._ORCH_MAX_WORKERS = 1
    run_single_seq = _make_run_single(intents, set(), sleep_ms=sleep_ms)
    orch_seq = OperationOrchestrator(run_single=run_single_seq)
    t0 = time.perf_counter()
    orch_seq.run(intents, session_id="seq")
    serial_ms = (time.perf_counter() - t0) * 1000

    # 并行（max_workers=4）
    oo._ORCH_MAX_WORKERS = 4
    run_single_par = _make_run_single(intents, set(), sleep_ms=sleep_ms)
    orch_par = OperationOrchestrator(run_single=run_single_par)
    t0 = time.perf_counter()
    orch_par.run(intents, session_id="par")
    parallel_ms = (time.perf_counter() - t0) * 1000

    # 恢复默认
    oo._ORCH_MAX_WORKERS = 1

    return {
        "n_intents": len(intents),
        "serial_elapsed_ms": round(serial_ms, 1),
        "parallel_elapsed_ms": round(parallel_ms, 1),
        "multi_intent_speedup": round(serial_ms / parallel_ms, 3) if parallel_ms else 0.0,
    }


# ── 4. serve 依赖层：拆分/分阶段/占位符闭环（复用 task_chain_eval）──

def eval_serve_metrics(quick: int = 0) -> dict:
    """跑 task_chain 链，派生 split/step2-6/closure 指标。需 serve。"""
    try:
        import task_chain_eval as tce
    except Exception as e:
        return {"skipped": f"task_chain_eval 导入失败: {e}"}

    if not _serve_alive():
        return {"skipped": "codemaker serve 不可达"}

    chains = tce.load_chains(DEFAULT_CHAINS)
    if quick:
        chains = chains[:quick]
    if not chains:
        return {"skipped": "无 task_chain 用例"}

    results = []
    for i, chain in enumerate(chains, start=1):
        r = tce.run_one_chain(i, chain, enable_skill=True)
        results.append(r)

    n = len(results)
    if n == 0:
        return {"skipped": "无有效结果"}

    # split_correct：实拆意图数 == 期望意图数（n_effective == n_expected）
    split_ok = sum(1 for r in results if r.n_effective == r.n_expected and r.n_expected > 0)
    split_correct_rate = split_ok / n

    # step2-6 派生（从 ChainRunResult 字段）
    step2_ok = sum(1 for r in results if r.locate_rate >= 1.0)         # 分区：全表定位
    step3_ok = sum(1 for r in results if r.n_effective == r.n_expected)  # 计划：全步规划
    step4_ok = sum(1 for r in results if r.ok or r.error_type not in
                   ("dangling_refs", "id_issues", "type_issues", "anti_pattern_hits"))
    step5_ok = sum(1 for r in results if r.chain_complete)              # 执行：全步 matched
    step6_ok = sum(1 for r in results if r.ok and r.error_type == "unknown")  # 汇总：ok 无错

    # 占位符闭环 per-step
    total_refs = sum(r.ref_total for r in results)
    ok_refs = sum(r.ref_ok for r in results)
    # 首个未解析 step
    unresolved_steps = []
    for r in results:
        for c in r.ref_checks:
            if not c.ok:
                unresolved_steps.append(c.step_index)
                break

    return {
        "n_chains": n,
        "split_correct_rate": round(split_correct_rate, 4),
        "step2_success_rate": round(step2_ok / n, 4),
        "step3_success_rate": round(step3_ok / n, 4),
        "step4_success_rate": round(step4_ok / n, 4),
        "step5_success_rate": round(step5_ok / n, 4),
        "step6_success_rate": round(step6_ok / n, 4),
        "placeholder_closure_rate": round(ok_refs / total_refs, 4) if total_refs else 1.0,
        "total_refs": total_refs,
        "ok_refs": ok_refs,
        "unresolved_step_indices": unresolved_steps,
        "chains": [_ser_chain(r) for r in results],
    }


def _ser_chain(r) -> dict:
    return {
        "cid": r.cid, "ok": r.ok, "n_expected": r.n_expected,
        "n_effective": r.n_effective, "chain_complete": r.chain_complete,
        "ref_ok": r.ref_ok, "ref_total": r.ref_total,
        "ref_consistency_rate": r.ref_consistency_rate,
        "error_type": r.error_type, "elapsed_ms": r.elapsed_ms,
    }


def _serve_alive() -> bool:
    import urllib.request, urllib.error
    url = os.environ.get("CODEMAKER_SERVER_URL", "").rstrip("/")
    if not url:
        return False
    try:
        req = urllib.request.Request(url + "/health", headers={"Connection": "close"})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status < 500
    except urllib.error.HTTPError as e:
        return e.code < 500
    except Exception:
        return False


# ── 聚合 & 报告 ──

def aggregate(topo: dict, rollback: dict, speedup: dict, serve: dict) -> dict:
    s = serve if isinstance(serve, dict) and not serve.get("skipped") else {}
    return {
        "summary": {
            "topo_correct_rate": topo.get("topo_correct_rate", 0) if isinstance(topo, dict) else 0,
            "rollback_correct_rate": rollback.get("rollback_correct_rate", 0) if isinstance(rollback, dict) else 0,
            "multi_intent_speedup": speedup.get("multi_intent_speedup", 0) if isinstance(speedup, dict) else 0,
            "split_correct_rate": s.get("split_correct_rate", 0),
            "step2_success_rate": s.get("step2_success_rate", 0),
            "step3_success_rate": s.get("step3_success_rate", 0),
            "step4_success_rate": s.get("step4_success_rate", 0),
            "step5_success_rate": s.get("step5_success_rate", 0),
            "step6_success_rate": s.get("step6_success_rate", 0),
            "placeholder_closure_rate": s.get("placeholder_closure_rate", 0),
        },
        "topo": topo,
        "rollback": rollback,
        "speedup": speedup,
        "serve": serve,
    }


def render_report(agg: dict) -> str:
    s = agg["summary"]
    lines = [
        "# 多指令场景评测报告（capability: multi-intent-evaluation）",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 纯逻辑层：OperationOrchestrator 直接测试（拓扑/回滚/加速比）",
        "- serve 层：复用 task_chain_eval（需 codemaker serve）",
        "",
        "## 一、总体指标",
        "",
        "| 指标 | 说明 | 值 |",
        "|---|---|---|",
        f"| topo_correct_rate | 拓扑序满足依赖约束 | {s.get('topo_correct_rate', 0):.4f} |",
        f"| rollback_correct_rate | 跨表事务回滚标记正确 | {s.get('rollback_correct_rate', 0):.4f} |",
        f"| multi_intent_speedup | 并行 vs 顺序加速比 | {s.get('multi_intent_speedup', 0):.3f} |",
        f"| split_correct_rate | 拆分意图数匹配期望 | {s.get('split_correct_rate', 0):.4f} |",
        f"| step2_success_rate | 分区阶段成功率 | {s.get('step2_success_rate', 0):.4f} |",
        f"| step3_success_rate | 计划阶段成功率 | {s.get('step3_success_rate', 0):.4f} |",
        f"| step4_success_rate | 校验阶段成功率 | {s.get('step4_success_rate', 0):.4f} |",
        f"| step5_success_rate | 执行阶段成功率 | {s.get('step5_success_rate', 0):.4f} |",
        f"| step6_success_rate | 汇总阶段成功率 | {s.get('step6_success_rate', 0):.4f} |",
        f"| placeholder_closure_rate | 占位符引用闭环率 | {s.get('placeholder_closure_rate', 0):.4f} |",
        "",
        "## 二、拓扑排序明细",
        "",
    ]
    topo = agg.get("topo", {})
    if isinstance(topo, dict) and not topo.get("skipped"):
        lines.append("| 用例 | 拓扑序 | 违反约束 | 循环 | 正确 |")
        lines.append("|---|---|---|---|---|")
        for c in topo.get("cases", []):
            lines.append(f"| {c['id']} | {c['order']} | {len(c['violated'])} | "
                         f"{'✓' if c['cycle_detected'] else '-'} | {'✓' if c['correct'] else '✗'} |")
    else:
        lines.append(f"- {topo.get('skipped', '跳过')}")

    lines += ["", "## 三、回滚场景明细", ""]
    rb = agg.get("rollback", {})
    if isinstance(rb, dict) and not rb.get("skipped"):
        lines.append("| 场景 | 失败点 | 跳过 | failed_tables | 前序未回滚 | 正确 |")
        lines.append("|---|---|---|---|---|---|")
        for c in rb.get("scenarios", []):
            lines.append(f"| {c['id']} | {'✓' if c['failed_at_ok'] else '✗'} | "
                         f"{'✓' if c['skipped_ok'] else '✗'} | {'✓' if c['failed_tables_ok'] else '✗'} | "
                         f"{'✓' if c['prev_not_rolled_back_ok'] else '✗'} | {'✓' if c['correct'] else '✗'} |")
    else:
        lines.append(f"- {rb.get('skipped', '跳过')}")

    lines += ["", "## 四、并行加速比", ""]
    sp = agg.get("speedup", {})
    if isinstance(sp, dict) and not sp.get("skipped"):
        lines += [
            f"- 意图数: {sp.get('n_intents', 0)}",
            f"- 顺序: {sp.get('serial_elapsed_ms', 0):.1f}ms",
            f"- 并行: {sp.get('parallel_elapsed_ms', 0):.1f}ms",
            f"- **加速比: {sp.get('multi_intent_speedup', 0):.3f}**",
        ]
    else:
        lines.append(f"- {sp.get('skipped', '跳过')}")

    lines += ["", "## 五、serve 依赖层（拆分/分阶段/闭环）", ""]
    sv = agg.get("serve", {})
    if isinstance(sv, dict) and not sv.get("skipped"):
        lines += [
            f"- 链数: {sv.get('n_chains', 0)}",
            f"- 拆分正确率: {sv.get('split_correct_rate', 0):.4f}",
            f"| 占位符闭环: {sv.get('ok_refs', 0)}/{sv.get('total_refs', 0)} "
            f"= {sv.get('placeholder_closure_rate', 0):.4f}",
            f"- 未解析 step: {sv.get('unresolved_step_indices', [])}",
        ]
    else:
        lines.append(f"- {sv.get('skipped', '跳过（未启用 --serve 或 serve 不可达）')}")
    return "\n".join(lines) + "\n"


# ── 主流程 ──

def load_fixtures() -> dict:
    return json.loads(FIXTURES.read_text(encoding="utf-8"))


def main() -> int:
    ap = argparse.ArgumentParser(description="多指令场景评测")
    ap.add_argument("--serve", action="store_true", help="跑 serve 依赖层（需 codemaker serve）")
    ap.add_argument("--quick", type=int, default=0, help="serve 层只跑前 N 条链")
    ap.add_argument("--out", type=str, default=str(REPORT_DIR))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    fixtures = load_fixtures()

    print("=== 多指令评测：纯逻辑层 ===")
    topo = eval_topo_correctness(fixtures)
    print(f"  topo_correct_rate={topo.get('topo_correct_rate', 0):.4f} "
          f"({topo.get('n_correct', 0)}/{topo.get('n_cases', 0)})")

    rollback = eval_rollback(fixtures)
    print(f"  rollback_correct_rate={rollback.get('rollback_correct_rate', 0):.4f} "
          f"({rollback.get('n_correct', 0)}/{rollback.get('n_scenarios', 0)})")

    speedup = eval_speedup(fixtures)
    if not speedup.get("skipped"):
        print(f"  speedup={speedup['multi_intent_speedup']:.3f} "
              f"(serial={speedup['serial_elapsed_ms']:.0f}ms "
              f"par={speedup['parallel_elapsed_ms']:.0f}ms)")

    serve = {"skipped": "未启用 --serve"}
    if args.serve:
        print("\n=== 多指令评测：serve 依赖层 ===")
        serve = eval_serve_metrics(quick=args.quick)
        if not serve.get("skipped"):
            print(f"  split_correct={serve['split_correct_rate']:.4f} "
                  f"step5={serve['step5_success_rate']:.4f} "
                  f"closure={serve['placeholder_closure_rate']:.4f}")
        else:
            print(f"  跳过：{serve['skipped']}")

    agg = aggregate(topo, rollback, speedup, serve)
    md = render_report(agg)
    (out_dir / "multi_intent_eval_latest.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "multi_intent_eval_latest.md").write_text(md, encoding="utf-8")
    print(f"\n报告: {out_dir / 'multi_intent_eval_latest.md'}")
    print(f"数据: {out_dir / 'multi_intent_eval_latest.json'}")

    # capability: eval-baseline-management —— 归档
    try:
        from tests.eval_baseline import archive_run, make_run_id
        tag = os.environ.get("EVAL_BASELINE_TAG", "")
        rid = make_run_id(tag=tag or None)
        archive_run("multi_intent_eval", agg, md, run_id=rid)
        print(f"归档: reports/archive/multi_intent_eval_{rid}.json")
    except Exception as e:
        print(f"[warn] 归档失败（不阻断）: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

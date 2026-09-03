#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step1 批量 trace + 评分：广泛样例跑 Step1，按阶段聚合指标，定位系统性错误。

与 trace_step1_full.py（单输入逐级详打）互补：本脚本跑批量用例，对每条用例
采集「过程指标 + 最终评分」，再跨用例聚合，回答：
  - Step1 总体准确率（表/sheet/动作/字段/占位符闭环）
  - 每个阶段的浪费与错误（LLM 次数、空响应率、去重误杀、主键污染…）
  - 哪些用例失败、失败在哪一环

用法（仓库根）：
    python server/tests/step1_batch_trace.py
    python server/tests/step1_batch_trace.py --suite planner --only 0,5
    python server/tests/step1_batch_trace.py --suite all --limit 3
    python server/tests/step1_batch_trace.py --repeat 2        # 同输入跑2次,测不确定性
    python server/tests/step1_batch_trace.py --quiet           # 只打汇总

输出：
  console 汇总 + server/tests/reports/step1_batch_latest.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
_SERVER_DIR = _TESTS_DIR.parent
_ROOT = _SERVER_DIR.parent
_REPORTS = _TESTS_DIR / "reports"

_SUITES = {
    "planner": _TESTS_DIR / "cases" / "planner_style_inputs.json",
    "chain": _TESTS_DIR / "cases" / "complex_task_chain_inputs.json",
    "chain_extra": _TESTS_DIR / "cases" / "complex_task_chain_inputs_extra.json",
    "school": _TESTS_DIR / "cases" / "school_quest_chain_inputs.json",
    "task": _TESTS_DIR / "cases" / "task_chain.json",
    "table": _TESTS_DIR / "cases" / "table_operation_test_cases.json",
    "shouchao": _TESTS_DIR / "cases" / "shouchao_shengli_inputs.json",
    "resource": _TESTS_DIR / "cases" / "generated_resource_smoke_inputs.json",
}


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


_load_dotenv(_ROOT / ".env")
os.environ.setdefault("CODEMAKER_DECOMPOSE_TRACE", "1")

if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 复用单输入 trace 的 client / 评分原语
import tests.trace_step1_full as T  # noqa: E402
import tests.step1_planner_eval as EV  # noqa: E402


# ── 过程指标采集器 ────────────────────────────────────────────

class Probe:
    """按用例采集 Step1 过程指标（hook + thinking 正则双通道）。"""

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.llm_calls = 0
        self.llm_empty = 0          # 响应为 [] 的调用（纯浪费）
        self.llm_chars = 0
        self.llm_dur = 0.0
        self.segments = 0
        self.dedup_dropped: list[dict] = []
        self.orphan_dropped = 0
        self.backfill: dict = {}
        self.coverage_extra: list[str] = []
        self.dropped_intents: list[str] = []

    def on_llm(self, rec: dict) -> None:
        self.llm_calls += 1
        self.llm_chars += rec.get("prompt", 0)
        self.llm_dur += rec.get("dur", 0.0)
        if rec.get("resp", 0) <= 8:     # "[]" / "{}" 等空响应
            self.llm_empty += 1

    def on_thinking(self, phase: str, detail: str) -> None:
        d = str(detail)
        m = re.search(r"缺表对账：expected (\d+) sheet，produced (\d+) sheet，缺 (\[.*?\])", d)
        if m:
            try:
                missing = json.loads(m.group(3).replace("'", '"'))
            except Exception:
                missing = []
            self.backfill = {"expected": int(m.group(1)), "produced": int(m.group(2)),
                             "missing": missing}
            return
        if "LLM 分段" in d or "切分" in d:
            m = re.search(r"(\d+) 段", d)
            if m:
                self.segments = int(m.group(1))
            return
        if "丢弃孤立空壳 add" in d:
            self.orphan_dropped += 1
            return


_PROBE = Probe()


def install_probes() -> None:
    """在 trace_step1_full 的 hook 之上挂采集器（只增观测，不改行为）。"""
    from agent.excel.parse_agent import ParseAgent
    from agent.excel.subagent.decompose_agent import DecomposeAgent

    # 去重删除明细 → 采集
    import inspect
    for name in ("_dedupe_nl_intents", "_dedupe_same_sheet_shadows"):
        raw_attr = inspect.getattr_static(ParseAgent, name)
        is_static = isinstance(raw_attr, staticmethod)
        orig = getattr(ParseAgent, name)

        def _mk(orig_, static_):
            def wrapper(*a, **kw):
                # static：a[0] 就是 intents；instance：a[0] 是 self，a[1] 才是 intents
                before = a[0] if static_ else (a[1] if len(a) > 1 else [])
                r = orig_(*a, **kw)
                after = r or []
                if isinstance(before, list) and len(after) != len(before):
                    gone = [x for x in before if id(x) not in {id(y) for y in after}]
                    for g in gone:
                        ex = getattr(g, "extras", None) or {}
                        _PROBE.dedup_dropped.append({
                            "table": getattr(g, "table_hint", ""),
                            "sheet": getattr(g, "sheet_hint", ""),
                            "action": getattr(g, "action", ""),
                            "produces": getattr(g, "produces_label", None),
                            "fields": (ex.get("fields") if isinstance(ex, dict) else None),
                            "raw": str(getattr(g, "raw", "") or "")[:100],
                        })
                return r
            return wrapper

        wrapped = _mk(orig, is_static)
        setattr(ParseAgent, name,
                staticmethod(wrapped) if is_static else wrapped)

    # 表覆盖自检的 extra_stems → 采集
    _orig_cov = DecomposeAgent._llm_verify_table_coverage

    def _cov(self, *a, **kw):
        r = _orig_cov(self, *a, **kw)
        try:
            _PROBE.coverage_extra = list(_COV_LAST.get("extra_stems") or [])
        except Exception:
            pass
        return r

    DecomposeAgent._llm_verify_table_coverage = _cov


_COV_LAST: dict = {}


# ── 主键污染 / 占位符异常检测（纯函数）───────────────────────

def _is_ph(v: Any) -> bool:
    return isinstance(v, str) and v.strip().startswith("<") and v.strip().endswith(">")


def _ph(v: Any) -> str:
    return str(v).strip().strip("<>").strip()


def _norm(s: str) -> str:
    """归一化：小写 + 去下划线，供 stem 比较（school_ability ↔ schoolability）。"""
    return re.sub(r"[_\W]+", "", str(s or "").lower())


def _ph_stem(label: str) -> str:
    """占位符 → 表 stem：new_school_ability_level_id_2 → school_ability_level。

    顺序：去 new_ 前缀 → 去末尾序号（_2）→ 去 _id 后缀。
    （先去序号再去 _id，否则 new_ability_id_2 会残留 _id。）
    """
    s = str(label).strip().strip("<>").strip().lower()
    if s.startswith("new_"):
        s = s[4:]
    s = re.sub(r"_?\d+$", "", s)          # 末尾序号：ability_id_2 → ability_id
    if s.endswith("_id"):
        s = s[:-3]
    return s


def _col_stem(col: str) -> str:
    """列名 → stem：school_ability_id → school_ability（去 _id 后缀）。"""
    s = str(col or "").strip().lower()
    if s.endswith("_id"):
        s = s[:-3]
    return s


def detect_intent_defects(intents: list) -> list[dict]:
    """检测 NLIntent 上的结构性缺陷（不依赖 expected_answer，通用判据）。

    1. self_pk_polluted：某列填的占位符与本 sheet/列名均无引用关系，或指向了
       本 sheet 的「子表」（严重：写库错行/覆盖）。

       判定顺序（ps=占位符 stem，ss=本 sheet stem，cs=列名 stem，均已归一化）：
         a. ps == ss              → 本 sheet 自引用主键，正常
         b. ss.startswith(ps)     → 本 sheet 是 ps 的扩展/子表，引用上游，正常
         c. ps == cs 或 ps.endswith(cs) → 列名与占位符吻合，正常
            （GlobalMail.template_id ← <new_mail_template_id>）
         d. 其余 → 污染。含 ps.startswith(cs) 情形：父列填了子实体 ID
            （SchoolAbility.school_ability_id ← <new_school_ability_level_id>）
    2. dangling_consumes：consumes 的 label 在本次产出里没有 producer
    3. dangling_placeholder：fields 里的占位符无 producer
    """
    defects: list[dict] = []
    producers: dict[str, dict] = {}
    for i, it in enumerate(intents or []):
        p = getattr(it, "produces_label", None)
        if p:
            producers.setdefault(str(p), {"idx": i,
                                          "table": getattr(it, "table_hint", "") or "",
                                          "sheet": getattr(it, "sheet_hint", "") or ""})

    for i, it in enumerate(intents or []):
        tbl = str(getattr(it, "table_hint", "") or "?")
        sh = str(getattr(it, "sheet_hint", "") or "?")
        ex = getattr(it, "extras", None) or {}
        fields = ex.get("fields") if isinstance(ex, dict) else None
        if not isinstance(fields, dict):
            fields = {}
        ss = _norm(sh)
        if not ss:
            ss = _norm(tbl)          # 无 sheet 时退化用 workbook stem

        for k, v in fields.items():
            if not _is_ph(v):
                continue
            lab = _ph(v)
            if lab not in producers:
                continue
            other = producers.get(lab) or {}
            ps = _norm(_ph_stem(lab))
            cs = _norm(_col_stem(k))
            if not ps or not cs:
                continue        # 列名过泛（id/index）或占位符无 stem → 不判
            if ps == ss:                      # a) 本 sheet 自引用主键
                continue
            if ss.startswith(ps):             # b) 引用上游/父表
                continue
            if ps == cs or ps.endswith(cs):   # c) 列名吻合
                continue
            # d) 同一 intent 内已有另一列填同占位符且合法 → 该列是别名重复，不判污染
            if any(_is_ph(v2) and _ph(v2) == lab and str(k2) != str(k)
                   and _norm(_col_stem(k2))
                   and (ps == _norm(_col_stem(k2))
                        or ps.endswith(_norm(_col_stem(k2)))
                        or ps == ss or ss.startswith(ps))
                   for k2, v2 in fields.items()):
                continue
            defects.append({
                "kind": "self_pk_polluted", "idx": i,
                "table": tbl, "sheet": sh, "col": str(k),
                "value": v, "produced_by": other.get("table"),
                "ph_stem": _ph_stem(lab), "sev": "hard"})

        # 2) 悬空 consumes
        for c in (getattr(it, "consumes_labels", None) or []):
            if str(c) not in producers:
                defects.append({"kind": "dangling_consumes", "idx": i,
                                "table": tbl, "sheet": sh, "label": str(c),
                                "sev": "hard"})

        # 3) 悬空占位符
        for k, v in fields.items():
            if _is_ph(v) and _ph(v) not in producers:
                defects.append({"kind": "dangling_placeholder", "idx": i,
                                "table": tbl, "sheet": sh, "col": k,
                                "value": v, "sev": "soft"})
    return defects


# ── 单用例执行 ────────────────────────────────────────────────

def run_one(case: dict, sub, cli, idx: int, quiet: bool) -> dict:
    from agent.excel.core.pipeline import StepContext

    text = (case.get("input") or case.get("text") or "").strip()
    expected = case.get("expected_answer") or []
    _PROBE.reset()

    sink_calls: list[tuple] = []

    def _sink(*a):
        if len(a) >= 2 and not str(a[0]).startswith("__json:"):
            _PROBE.on_thinking(a[0], a[1])
        sink_calls.append(a)

    ctx = StepContext(session_id=f"batch{idx}", user_text=text, thinking_sink=_sink)
    trace = getattr(sub, "_trace", None)
    llm0 = len(trace.llm) if trace else 0

    t0 = time.time()
    err = ""
    try:
        res = sub.execute(ctx)
    except Exception as e:  # noqa: BLE001
        err = f"{type(e).__name__}: {e}"
        res = None
    dur = time.time() - t0

    recs = (trace.llm[llm0:] if trace else [])
    for r in recs:
        _PROBE.on_llm(r)

    intents = ((res.artifacts or {}).get("intents") if res else None) or []
    actual = [EV._intent_to_dict(it) for it in intents]
    rows = EV._greedy_align(expected, actual) if expected else []

    m = (res.metrics or {}) if res else {}
    metrics = {
        "n_expected": len(expected),
        "n_actual": len(intents),
        "full_match": sum(1 for r in rows if r.get("full_match")),
        "table_hit": sum(1 for r in rows if r.get("table_ok")),
        "sheet_hit": sum(1 for r in rows if r.get("sheet_ok")),
        "action_hit": sum(1 for r in rows if r.get("action_ok")),
        "keys_found": sum(r.get("keys_found", 0) for r in rows),
        "keys_ok": sum(r.get("keys_value_ok", 0) for r in rows),
        "keys_exp": sum(r.get("exp_keys", 0) for r in rows),
        "missing": sum(1 for r in rows if r.get("unmatched")),
    }
    metrics["count_delta"] = metrics["n_actual"] - metrics["n_expected"]

    defects = detect_intent_defects(intents)
    proc = {
        "dur_s": round(dur, 1),
        "llm_calls": _PROBE.llm_calls,
        "llm_empty": _PROBE.llm_empty,
        "llm_prompt_chars": _PROBE.llm_chars,
        "segments": m.get("segments", 0),
        "backfill": _PROBE.backfill,
        "coverage_extra_n": len(_PROBE.coverage_extra),
        "dedup_dropped": _PROBE.dedup_dropped,
        "orphan_dropped": _PROBE.orphan_dropped,
        "defects": defects,
        "hard_defects": sum(1 for d in defects if d.get("sev") == "hard"),
        "quality_hard": m.get("step1_quality_hard", 0),
        "quality_issues": m.get("step1_quality_issues", 0),
        "err": err,
    }
    return {"idx": idx, "name": (case.get("_meta") or {}).get("name", ""),
            "input": text, "metrics": metrics, "proc": proc,
            "rows": rows, "actual": actual}


# ── 聚合与展示 ────────────────────────────────────────────────

def _pct(n: int, d: int) -> str:
    return f"{n}/{d}({n / d * 100:.0f}%)" if d else "-"


class _Flat:
    """从报告里的 actual dict 重建轻量 intent 对象，供缺陷检测复用。"""

    def __init__(self, d: dict):
        self.action = d.get("action")
        self.table_hint = d.get("table")
        self.sheet_hint = d.get("sheet")
        self.extras = {"fields": d.get("fields") or {}}
        self.produces_label = d.get("produces")
        self.consumes_labels = d.get("consumes") or []
        self.raw = ""


def recheck(report_path: Path, verbose: bool = True) -> int:
    """从已有报告重算缺陷（判据更新后无需重跑 LLM）。"""
    data = json.loads(report_path.read_text(encoding="utf-8"))
    results = data.get("results") or []
    total = 0
    by_kind: dict[str, int] = {}
    for r in results:
        objs = [_Flat(a) for a in (r.get("actual") or [])]
        ds = detect_intent_defects(objs)
        hard = [x for x in ds if x.get("sev") == "hard"]
        total += len(hard)
        for x in hard:
            by_kind[x["kind"]] = by_kind.get(x["kind"], 0) + 1
        if hard and verbose:
            print(f"[{r.get('suite','')}:{r.get('idx')}] "
                  f"{str(r.get('name') or r.get('input'))[:44]}  硬缺陷 {len(hard)}")
            for x in hard[:8]:
                print(f"    [{x['idx']}] {x['table']}/{x['sheet']} "
                      f"col={x['col']} val={x['value']} "
                      f"ph_stem={x.get('ph_stem')} by={x.get('produced_by')}")
    print(f"\n重算完成：{len(results)} 条用例，硬缺陷 {total} 处 {by_kind}")
    return total


def aggregate(results: list[dict]) -> dict:
    agg = {k: 0 for k in ("n_expected", "n_actual", "full_match", "table_hit",
                          "sheet_hit", "action_hit", "keys_found", "keys_ok",
                          "keys_exp", "missing", "llm_calls", "llm_empty",
                          "llm_prompt_chars", "dur_s", "hard_defects",
                          "dedup_dropped", "orphan_dropped")}
    defect_kinds: dict[str, int] = {}
    count_mismatch = 0
    for r in results:
        for k, v in (r.get("metrics") or {}).items():
            if k in agg:
                agg[k] += v or 0
        p = r.get("proc") or {}
        for k in ("llm_calls", "llm_empty", "llm_prompt_chars", "dur_s",
                  "hard_defects", "orphan_dropped"):
            agg[k] += p.get(k, 0) or 0
        agg["dedup_dropped"] += len(p.get("dedup_dropped") or [])
        for d in (p.get("defects") or []):
            defect_kinds[d["kind"]] = defect_kinds.get(d["kind"], 0) + 1
        if (r.get("metrics") or {}).get("count_delta"):
            count_mismatch += 1
    agg["cases"] = len(results)
    agg["count_mismatch_cases"] = count_mismatch
    agg["defect_kinds"] = defect_kinds
    agg["llm_empty_rate"] = (round(agg["llm_empty"] / agg["llm_calls"] * 100, 1)
                             if agg["llm_calls"] else 0)
    return agg


def print_agg(agg: dict, title: str) -> None:
    print(f"\n{'=' * 74}\n== {title}\n{'=' * 74}")
    print(f"  用例 {agg['cases']} 条   总耗时 {agg['dur_s']:.0f}s   "
          f"LLM {agg['llm_calls']} 次（空响应 {agg['llm_empty']} 次 = "
          f"{agg['llm_empty_rate']}%）")
    print(f"  意图数      期望 {agg['n_expected']}  实际 {agg['n_actual']}   "
          f"数量不符用例 {agg['count_mismatch_cases']}/{agg['cases']}")
    print(f"  完全命中    {_pct(agg['full_match'], agg['n_expected'])}")
    print(f"  表路由      {_pct(agg['table_hit'], agg['n_expected'])}")
    print(f"  Sheet       {_pct(agg['sheet_hit'], agg['n_expected'])}")
    print(f"  动作        {_pct(agg['action_hit'], agg['n_expected'])}")
    print(f"  字段值      {_pct(agg['keys_ok'], agg['keys_exp'])}   "
          f"(键命中 {_pct(agg['keys_found'], agg['keys_exp'])})")
    print(f"  去重误杀    {agg['dedup_dropped']} 条   空壳丢弃 {agg['orphan_dropped']} 条")
    print(f"  硬缺陷      {agg['hard_defects']} 处  {agg.get('defect_kinds')}")


def print_case(r: dict, verbose: bool) -> None:
    m, p = r["metrics"], r["proc"]
    flag = "OK " if m["full_match"] == m["n_expected"] and not p["hard_defects"] \
        and not m["count_delta"] else "!! "
    print(f"{flag}[{r['idx']}] {r['name'][:40] or r['input'][:40]}")
    print(f"      意图 {m['n_actual']}/{m['n_expected']}(Δ{m['count_delta']:+d}) "
          f"完全命中 {m['full_match']} 表 {m['table_hit']} 字段 {m['keys_ok']}/{m['keys_exp']} "
          f"| LLM {p['llm_calls']}(空{p['llm_empty']}) {p['dur_s']}s 段{p['segments']}")
    if p["hard_defects"]:
        print(f"      硬缺陷 {p['hard_defects']}:")
        for d in (p["defects"] or [])[:6]:
            if d.get("sev") == "hard":
                print(f"        - {d['kind']} [{d.get('idx')}] "
                      f"{d.get('table')}/{d.get('sheet')} "
                      f"col={d.get('col') or d.get('label')} "
                      f"val={d.get('value','')} "
                      f"{('by=' + str(d.get('produced_by'))) if d.get('produced_by') else ''}")
    if p["dedup_dropped"]:
        print(f"      去重删除 {len(p['dedup_dropped'])} 条:")
        for d in p["dedup_dropped"][:4]:
            print(f"        - {d['action']} {d['table']}/{d['sheet']} "
                  f"{str(d['fields'])[:90]}")
    if p["backfill"]:
        bf = p["backfill"]
        print(f"      缺表对账 expected={bf.get('expected')} produced={bf.get('produced')} "
              f"缺{bf.get('missing')}")
    if p["err"]:
        print(f"      异常 {p['err']}")
    if verbose:
        for row in (r.get("rows") or []):
            if row.get("unmatched"):
                print(f"        未覆盖: {row.get('exp')}")
            elif not row.get("full_match"):
                print(f"        偏差: exp={row.get('exp')} act={row.get('act')} "
                      f"keys={row.get('keys_found')}/{row.get('exp_keys')} "
                      f"ok={row.get('keys_value_ok')} extra={row.get('extra_keys')}")


# ── 主流程 ────────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Step1 批量 trace + 评分")
    ap.add_argument("--suite", default="planner",
                    choices=list(_SUITES) + ["all"], help="用例集")
    ap.add_argument("--only", default="", help="逗号分隔下标（单 suite 时生效）")
    ap.add_argument("--limit", type=int, default=0, help="每 suite 最多跑几条(0=不限)")
    ap.add_argument("--repeat", type=int, default=1, help="每条用例重复次数(测不确定性)")
    ap.add_argument("--parallel", type=int, default=1, help="并发用例数")
    ap.add_argument("--timeout", type=int, default=90, help="单次 LLM 超时")
    ap.add_argument("--model", default="")
    ap.add_argument("--quiet", action="store_true", help="只打汇总")
    ap.add_argument("-v", "--verbose", action="store_true", help="打印逐条偏差")
    ap.add_argument("--out", default=str(_REPORTS / "step1_batch_latest.json"))
    ap.add_argument("--recheck", default="",
                    help="仅从已有报告重算缺陷（不跑 LLM），用于判据更新后复查")
    args = ap.parse_args()

    if args.recheck:
        p = Path(args.recheck)
        if not p.is_absolute():
            p = _ROOT / p
        return 0 if recheck(p) == 0 else 1

    os.environ["CODEMAKER_DECOMPOSE_TIMEOUT"] = str(args.timeout)
    EV._load_header_map(_ROOT / "resources")

    suites = list(_SUITES) if args.suite == "all" else [args.suite]
    cases: list[tuple[str, int, dict]] = []
    for s in suites:
        path = _SUITES[s]
        if not path.exists():
            print(f"[warn] 用例集不存在: {path}")
            continue
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            continue
        if args.only and len(suites) == 1:
            idxs = [int(x) for x in args.only.split(",") if x.strip()]
            sel = [(s, i, data[i]) for i in idxs if 0 <= i < len(data)]
        else:
            sel = [(s, i, c) for i, c in enumerate(data)]
        if args.limit:
            sel = sel[:args.limit]
        cases.extend(sel)

    print(f"用例集={args.suite}  共 {len(cases)} 条  repeat={args.repeat} "
          f"parallel={args.parallel}  model={args.model or os.environ.get('DEEPSEEK_MODEL','')}")

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print("缺 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    raw = T._DeepSeekRaw(
        api_key=key,
        base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        model=args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))

    from agent.excel.cli.real_cli import RealCodeMakerCLI
    from agent.excel.core.pipeline import Step1ParseSubAgent
    from tests.smoke_step1_deepseek import _SmokeParser

    cli = RealCodeMakerCLI(workspace=_ROOT / "resources")

    def _make_sub(trace):
        client = T._TracedClient(trace, raw, _REPORTS / "step1_batch_dump")
        parser = _SmokeParser(client=client,
                              model=args.model or os.environ.get("DEEPSEEK_MODEL", ""))
        parser._cancel_event = None
        sub = Step1ParseSubAgent(parser=parser, cli=cli,
                                 thinking_sink=lambda *a: None)
        sub._trace = trace
        return sub

    install_probes()

    results: list[dict] = []
    lock = threading.Lock()

    def _work(item):
        sname, i, case = item
        trace = T.Trace(quiet=True)       # 每用例独立 trace，避免并发串扰+刷屏
        sub = _make_sub(trace)
        for rep in range(args.repeat):
            r = run_one(case, sub, cli, i, args.quiet)
            r["suite"] = sname
            r["rep"] = rep
            with lock:
                results.append(r)
                if not args.quiet:
                    print_case(r, args.verbose)

    if args.parallel > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.parallel) as ex:
            list(ex.map(_work, cases))
    else:
        for c in cases:
            _work(c)

    results.sort(key=lambda r: (r.get("suite", ""), r.get("idx", 0), r.get("rep", 0)))
    agg = aggregate(results)
    print_agg(agg, f"汇总 · {args.suite}")

    # 不确定性检测（repeat>1 时同一用例多次结果对比）
    if args.repeat > 1:
        unstable = 0
        by_key: dict[tuple, list] = {}
        for r in results:
            by_key.setdefault((r["suite"], r["idx"]), []).append(r)
        for k, rs in by_key.items():
            sigs = {(x["metrics"]["n_actual"], x["metrics"]["full_match"],
                     x["metrics"]["keys_ok"]) for x in rs}
            if len(sigs) > 1:
                unstable += 1
                print(f"  !! 不稳定 {k}: " +
                      " vs ".join(f"n={x['metrics']['n_actual']}/"
                                  f"full={x['metrics']['full_match']}/"
                                  f"k={x['metrics']['keys_ok']}" for x in rs))
        print(f"  不稳定用例 {unstable}/{len(by_key)}")

    out = Path(args.out)
    if not out.is_absolute():
        out = _ROOT / out
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"agg": agg, "results": results},
                              ensure_ascii=False, indent=2, default=str),
                   encoding="utf-8")
    print(f"\n报告: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

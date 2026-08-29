from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent
ROOT = SERVER_DIR.parent
RESOURCES = ROOT / "resources"
REPORT_DIR = TESTS_DIR / "reports"
DEFAULT_CASES = TESTS_DIR / "cases" / "planner_style_inputs.json"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))
if str(TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(TESTS_DIR))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

import step1_planner_eval as spe
from step1_planner_eval import _fmt_pct, _load_header_map, run_case
from smoke_step1_deepseek import _DeepSeekClient, _PromptResult, _SessionResult, _SmokeParser


class _FallbackOnlyClient:
    """Parser client that records prompts but never calls an external model."""

    def __init__(self, *, model: str = "fallback-only"):
        self.model = model
        self.prompts: list[str] = []
        self.responses: list[str] = []

    def create_session(self, **_kwargs) -> _SessionResult:
        return _SessionResult(ok=True)

    def prompt(self, _session_id: str, prompt: str, **_kwargs) -> _PromptResult:
        self.prompts.append(prompt)
        msg = "ERROR fallback_only: external LLM disabled for deterministic smoke"
        self.responses.append(msg)
        return _PromptResult(
            ok=False,
            error="external LLM disabled for deterministic smoke",
            error_type="fallback_only",
        )


def _p(msg: str = "") -> None:
    print(msg, flush=True)


def _load_cases(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"cases file must contain a JSON list: {path}")
    return data


def _select_cases(cases: list[dict], only: str) -> list[tuple[int, dict]]:
    if not only:
        return list(enumerate(cases))
    idxs = {int(x) for x in only.split(",") if x.strip()}
    return [(i, c) for i, c in enumerate(cases) if i in idxs]


def _aggregate(results: list[dict]) -> dict:
    agg = {
        "cases": len(results), "expected": 0, "actual": 0,
        "table_hit": 0, "sheet_hit": 0, "action_hit": 0, "full": 0,
        "keys_exp": 0, "keys_found": 0, "keys_ok": 0,
        "ph_total": 0, "ph_ok": 0, "ph_unresolved": 0,
        "loc_total": 0, "loc_ok": 0,
    }
    for r in results:
        m = r["metrics"]
        agg["expected"] += r["n_expected"]
        agg["actual"] += r["n_actual"]
        agg["table_hit"] += m["table_hit"]
        agg["sheet_hit"] += m["sheet_hit"]
        agg["action_hit"] += m["action_hit"]
        agg["full"] += m["full_match"]
        agg["keys_exp"] += m["keys_exp"]
        agg["keys_found"] += m["keys_found"]
        agg["keys_ok"] += m["keys_ok"]
        agg["ph_total"] += m["ph_total"]
        agg["ph_ok"] += m["ph_ok"]
        agg["ph_unresolved"] += m["ph_unresolved"]
        agg["loc_total"] += m["loc_total"]
        agg["loc_ok"] += m["loc_ok"]
    return agg


def _prompt_summary(prompts: list[str]) -> dict:
    lengths = [len(p or "") for p in prompts]
    return {
        "count": len(prompts),
        "min_chars": min(lengths) if lengths else 0,
        "max_chars": max(lengths) if lengths else 0,
        "total_chars": sum(lengths),
        "has_few_shot": any("few-shot:" in (p or "") for p in prompts),
        "has_fill_rules": any("填表规则" in (p or "") or "fill" in (p or "").lower()
                              for p in prompts),
        "has_schema": any("schema" in (p or "").lower() for p in prompts),
        "snippets": [
            (p[:1200] + f"... <+{len(p) - 1200} chars>") if len(p) > 1200 else p
            for p in prompts[:2]
        ],
    }


def _load_expected_header_map(cli, cases: list[dict]) -> None:
    tables = {}
    try:
        tables = {p.stem.lower(): p for p in cli.list_tables()}
    except Exception:
        return
    wanted: set[tuple[str, str]] = set()
    for case in cases:
        for exp in case.get("expected_answer") or []:
            wanted.add((spe._stem_of(exp.get("table", "")), spe._norm_sheet(exp.get("sheet", ""))))
    for stem, sheet_l in wanted:
        p = tables.get(stem)
        if p is None or not sheet_l:
            continue
        try:
            sheets = cli.get_sheets(p) or []
        except Exception:
            sheets = []
        sheet = next((s for s in sheets if spe._norm_sheet(s) == sheet_l), "")
        if not sheet:
            continue
        try:
            header = cli.read_header(p, sheet) or []
            type_row = cli.read_type_row(p, sheet) or []
        except Exception:
            continue
        canon_map = {}
        type_map = {}
        row1totype = {}
        for a, b in zip(header, type_row):
            if not a:
                continue
            canon = spe._canon_key(a)
            if not canon:
                continue
            canon_map[canon.lower()] = a
            b_norm = str(b or "").split(":")[0].strip()
            if b_norm:
                type_map[spe._canon_key(b_norm).lower()] = a
                type_map[b_norm.lower()] = a
                row1totype[canon.lower()] = spe._canon_key(b_norm).lower()
        spe._HEADER_MAP[(stem, sheet_l)] = {
            "canon": canon_map,
            "type": type_map,
            "row1totype": row1totype,
        }


def main() -> int:
    ap = argparse.ArgumentParser(description="Run Step1 cases through parse/decompose smoke.")
    ap.add_argument("--mode", choices=["fallback", "deepseek"], default="fallback",
                    help="fallback is deterministic and never calls an external LLM.")
    ap.add_argument("--cases-file", default=str(DEFAULT_CASES))
    ap.add_argument("--only", default="", help="Comma-separated case indexes. Empty means all.")
    ap.add_argument("--timeout", type=int, default=90)
    ap.add_argument("--skip-header-map", action="store_true",
                    help="Skip full workbook header scan; faster but field-key matching is stricter.")
    ap.add_argument("--full-header-map", action="store_true",
                    help="Scan all workbook headers like step1_planner_eval.py.")
    args = ap.parse_args()

    os.environ.setdefault("CODEMAKER_DECOMPOSE_SINGLE_RETRY", "0")
    os.environ.setdefault("CODEMAKER_DECOMPOSE_RETRY", "0")
    os.environ["CODEMAKER_DECOMPOSE_TIMEOUT"] = str(max(1, args.timeout))
    os.environ["CODEMAKER_DECOMPOSE_CHAIN_TIMEOUT"] = str(max(1, args.timeout))
    os.environ.setdefault("CODEMAKER_STEP1_DEADLINE_S", str(max(args.timeout + 15, 60)))

    from agent.real_cli import RealCodeMakerCLI
    from agent.excel.locator.table_index import build_index, load_index
    from agent.excel.parse_agent import ParseAgent
    from agent.excel.subagent.decompose_agent import DecomposeAgent
    from agent.excel.subagent.locator_agent import LocatorAgent

    cases_file = Path(args.cases_file)
    selected = _select_cases(_load_cases(cases_file), args.only)

    if args.mode == "deepseek":
        api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not api_key:
            print("Set DEEPSEEK_API_KEY before running --mode deepseek.", file=sys.stderr)
            return 2
        client = _DeepSeekClient(
            api_key=api_key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash"),
        )
    else:
        client = _FallbackOnlyClient()
    parser = _SmokeParser(client=client, model=client.model)
    cli = RealCodeMakerCLI(workspace=RESOURCES)

    try:
        idx = load_index()
    except Exception:
        idx = []
    if len(idx) < 5 or any(getattr(t, "stem", "").startswith("_test") for t in idx):
        _p(f"[index] stale index ({len(idx)} tables), rebuilding from resources...")
        build_index(RESOURCES)

    la = LocatorAgent(parser=parser, cli=cli)
    da = DecomposeAgent(parser=parser, cli=cli)
    pa = ParseAgent(parser=parser, cli=cli, locator_agent=la, decompose_agent=da)
    selected_cases = [case for _, case in selected]
    if args.full_header_map and not args.skip_header_map:
        _load_header_map(RESOURCES)
    elif not args.skip_header_map:
        _load_expected_header_map(cli, selected_cases)

    _p(f"cases_file={cases_file} selected={len(selected)} mode={args.mode} model={client.model}")
    results: list[dict] = []
    for original_idx, case in selected:
        name = (case.get("_meta") or {}).get("name", "")
        _p(f"\n[{original_idx}] {name[:72]}")
        before_calls = len(client.prompts)
        before_resp = len(client.responses)
        r = run_case(case, pa, la, da)
        r["case_index"] = original_idx
        r["llm"] = {
            "prompt_count": len(client.prompts) - before_calls,
            "prompt_summary": _prompt_summary(client.prompts[before_calls:]),
            "responses": client.responses[before_resp:],
        }
        results.append(r)
        m = r["metrics"]
        _p(f"  input: {r['input'][:80]}...")
        _p(f"  parse: actual={r['n_actual']} expected={r['n_expected']} dur={r['dur_s']}s")
        _p(f"  route: table {_fmt_pct(m['table_hit'], r['n_expected'])}"
           f" / sheet {_fmt_pct(m['sheet_hit'], r['n_expected'])}"
           f" / action {_fmt_pct(m['action_hit'], r['n_expected'])}")
        _p(f"  fields: keys {_fmt_pct(m['keys_found'], m['keys_exp'])}"
           f" / values {_fmt_pct(m['keys_ok'], m['keys_exp'])}")
        _p(f"  placeholders: {_fmt_pct(m['ph_ok'], m['ph_total'])}"
           f" unresolved={m['ph_unresolved']} llm_prompts={r['llm']['prompt_count']}")

    summary = _aggregate(results)
    _p("\nSummary")
    _p(f"  intents: expected={summary['expected']} actual={summary['actual']}")
    _p(f"  route: table {_fmt_pct(summary['table_hit'], summary['expected'])}"
       f" / sheet {_fmt_pct(summary['sheet_hit'], summary['expected'])}"
       f" / action {_fmt_pct(summary['action_hit'], summary['expected'])}")
    _p(f"  fields: keys {_fmt_pct(summary['keys_found'], summary['keys_exp'])}"
       f" / values {_fmt_pct(summary['keys_ok'], summary['keys_exp'])}")
    _p(f"  placeholders: {_fmt_pct(summary['ph_ok'], summary['ph_total'])}"
       f" unresolved={summary['ph_unresolved']}")

    REPORT_DIR.mkdir(exist_ok=True)
    out = {
        "cases_file": str(cases_file),
        "mode": args.mode,
        "model": client.model,
        "results": results,
        "summary": summary,
    }
    out_path = REPORT_DIR / "step1_cases_deepseek_smoke_latest.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    _p(f"  report: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

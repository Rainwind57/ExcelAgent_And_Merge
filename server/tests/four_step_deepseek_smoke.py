from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agent.excel.cli.real_cli import RealCodeMakerCLI
from agent.excel.core.agent import TableAgent
from agent.excel.core.pipeline import (
    ExcelAgentPipeline,
    ExcelAgentServices,
    STEP1_PARSE,
    STEP2_VALIDATE,
    STEP3_EXECUTE,
    STEP4_CONCLUDE,
    Step1ParseSubAgent,
    Step2ValidateSubAgent,
    Step3ExecuteSubAgent,
    Step4ConcludeSubAgent,
    StepContext,
)
from tests.smoke_step1_deepseek import _DeepSeekClient, _SmokeParser


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"
REPORT_DIR = ROOT / "server" / "tests" / "reports"
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _read_cases(path: Path) -> list[dict[str, Any]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError(f"{path} must contain a JSON array")
    return [x for x in data if isinstance(x, dict)]


def _pick_indexes(total: int, only: str, limit: int) -> list[int]:
    if only:
        picked: list[int] = []
        for part in only.split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                left, right = part.split("-", 1)
                picked.extend(range(int(left), int(right) + 1))
            else:
                picked.append(int(part))
        return [i for i in picked if 0 <= i < total]
    indexes = list(range(total))
    return indexes[:limit] if limit > 0 else indexes


def _jsonable(value: Any, depth: int = 0) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return _CONTROL_RE.sub("", value) if isinstance(value, str) else value
    if depth > 5:
        return str(value)[:200]
    if isinstance(value, dict):
        return {str(k): _jsonable(v, depth + 1) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v, depth + 1) for v in value]
    if hasattr(value, "to_checkpoint_dict"):
        try:
            return _jsonable(value.to_checkpoint_dict(), depth + 1)
        except Exception:
            pass
    if hasattr(value, "to_dict"):
        try:
            return _jsonable(value.to_dict(), depth + 1)
        except Exception:
            pass
    if hasattr(value, "__dict__"):
        try:
            return _jsonable(value.__dict__, depth + 1)
        except Exception:
            pass
    return _CONTROL_RE.sub("", str(value)[:200])


def _install_deepseek_compat(client: _DeepSeekClient) -> None:
    if hasattr(client, "extract_json_from_response"):
        return

    def _extract_json_from_response(text: str) -> Any:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        try:
            return json.loads(cleaned)
        except Exception:
            pass
        start = min([p for p in [cleaned.find("["), cleaned.find("{")] if p >= 0],
                    default=-1)
        if start < 0:
            return None
        for end in range(len(cleaned), start, -1):
            frag = cleaned[start:end].strip()
            if not frag or frag[-1] not in "]}":
                continue
            try:
                return json.loads(frag)
            except Exception:
                continue
        return None

    client.extract_json_from_response = _extract_json_from_response  # type: ignore[attr-defined]


def _step_report(ctx: StepContext) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for sid in (STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE):
        result = ctx.get_result(sid)
        if result is None:
            out[sid] = {"present": False}
            continue
        artifacts = result.artifacts or {}
        entry = {
            "present": True,
            "ok": result.ok,
            "metrics": _jsonable(result.metrics),
            "warnings": list(result.warnings or []),
            "errors": [e.to_event() for e in result.errors or []],
        }
        if sid == STEP1_PARSE:
            entry["intent_count"] = len(artifacts.get("intents") or [])
            entry["intents"] = _jsonable(artifacts.get("intents") or [])
            entry["segments"] = _jsonable(artifacts.get("segments") or [])
        elif sid == STEP2_VALIDATE:
            entry["validated_count"] = len(artifacts.get("validated") or [])
        elif sid == STEP3_EXECUTE:
            subtasks = artifacts.get("subtasks") or []
            entry["subtasks"] = _jsonable(subtasks)
            entry["failures"] = _jsonable(artifacts.get("failures") or [])
            entry["result_rows"] = _jsonable(artifacts.get("results") or [])
            entry["partial_subtasks"] = [
                i + 1 for i, s in enumerate(subtasks)
                if isinstance(s, dict) and s.get("partial")
            ]
        elif sid == STEP4_CONCLUDE:
            entry["summary"] = artifacts.get("summary", "")
            entry["induced_count"] = artifacts.get("induced_count", 0)
        out[sid] = entry
    return out


def _match_expected(sandbox: Path, expected: list[dict[str, Any]]) -> dict[str, Any]:
    if not expected:
        return {"skipped": True, "reason": "no expected_answer"}
    try:
        from tests.table_case_eval import (
            RES,
            _build_eval_sheet_aliases,
            _validate_fixture,
            build_pristine_index,
            diff_sandbox,
            match_case,
        )
    except Exception as exc:  # noqa: BLE001
        return {"skipped": True, "reason": f"table_case_eval import failed: {exc}"}
    actual_ops = diff_sandbox(sandbox, RES)
    pristine_idx = build_pristine_index(expected)
    fixture_errors = _validate_fixture(expected, pristine_idx)
    sheet_alias_map = _build_eval_sheet_aliases()
    entries, extra_ops = match_case(
        expected, actual_ops, pristine_idx, sheet_alias_map=sheet_alias_map)
    effective = [r for r in entries if getattr(r, "status", "") != "precondition_missing"]
    matched = [r for r in effective if getattr(r, "status", "") == "matched"]
    return {
        "skipped": False,
        "expected_ops": len(expected),
        "actual_ops": len(actual_ops),
        "effective_expected": len(effective),
        "matched": len(matched),
        "extra_ops": len(extra_ops),
        "fixture_errors": fixture_errors,
        "entries": _jsonable(entries),
        "actual": _jsonable(actual_ops),
        "extra": _jsonable(extra_ops),
    }


def _build_pipeline(agent: TableAgent) -> ExcelAgentPipeline:
    from agent.excel.core.pipeline import ContractGate
    services = ExcelAgentServices(legacy_agent=agent)
    step1 = Step1ParseSubAgent(
        parser=agent.parser,
        thinking_sink=None,
        cli=agent.cli,
        locator_agent=agent._locator_agent,
        decompose_agent=agent._decompose_agent,
    )
    # Step1.5 契约校验层：与 run_v2 同构（schema_getter 从 cli 读表头，
    # call_llm_raw 复用 parser.client 通道），缺失会导致 orchestrator 拿到
    # None step → 'NoneType' object has no attribute 'execute' 软错误污染 Step4。
    _cli = agent.cli
    _table_index: dict = {}

    def _schema_getter(stem, sheet):
        if _cli is None:
            return [], []
        try:
            idx = _table_index
            if not idx:
                idx = {p.stem: p for p in _cli.list_tables()}
                _table_index.update(idx)
            p = idx.get(stem) or idx.get((stem or "").lower())
            if p is None:
                return [], []
            return list(_cli.read_header(p, sheet) or []), \
                list(_cli.read_type_row(p, sheet) or [])
        except Exception:
            return [], []

    _call_llm = None
    _parser = agent.parser
    if _parser is not None and getattr(_parser, "client", None) is not None:
        _client = _parser.client
        _dir = getattr(_parser, "directory", "") or ""
        _model = getattr(_parser, "model", "") or ""

        def _call_llm(prompt, timeout=30):
            try:
                sr = _client.create_session(_dir, _model)
                if not getattr(sr, "ok", False):
                    return None
                pr = _client.prompt(getattr(sr, "session_id", ""),
                                    prompt, timeout=timeout, model=_model)
                return getattr(pr, "response_text", "") or None
            except Exception:
                return None
    step1_5 = ContractGate(schema_getter=_schema_getter, call_llm_raw=_call_llm,
                           cli=_cli)
    return ExcelAgentPipeline(
        step1=step1,
        step1_5=step1_5,
        step2=Step2ValidateSubAgent(services=services),
        step3=Step3ExecuteSubAgent(services=services),
        step4=Step4ConcludeSubAgent(services=services),
    )


def run_case(case: dict[str, Any], idx: int, client: _DeepSeekClient,
             model: str, timeout: int) -> dict[str, Any]:
    text = str(case.get("input") or "")
    tmp_dir = Path(tempfile.mkdtemp(prefix=f"four_step_case_{idx}_"))
    sandbox = tmp_dir / "resources"
    shutil.copytree(RESOURCES, sandbox)
    before_prompt_count = len(client.prompts)
    before_response_count = len(client.responses)
    try:
        parser = _SmokeParser(client=client, model=model)
        cli = RealCodeMakerCLI(workspace=sandbox)
        agent = TableAgent(
            cli=cli,
            parser=parser,
            live_index=True,
            enable_skill=True,
            enable_verify_repair_loop=False,
            enable_skill_tools_recovery=False,
        )
        ctx = StepContext(session_id=f"four_step_deepseek_{idx}", user_text=text,
                          legacy_agent=agent)
        pipeline = _build_pipeline(agent)
        events = list(pipeline.run(ctx))
        expected = case.get("expected_answer") or []
        match = _match_expected(sandbox, expected if isinstance(expected, list) else [])
        prompts = client.prompts[before_prompt_count:]
        responses = client.responses[before_response_count:]
        return {
            "case_index": idx,
            "name": (case.get("_meta") or {}).get("name", ""),
            "ok": ctx.all_ok(),
            "input_len": len(text),
            "events": _jsonable(events),
            "steps": _step_report(ctx),
            "match": match,
            "llm": {
                "model": model,
                "prompt_count": len(prompts),
                "response_count": len(responses),
                "prompt_chars": [len(p) for p in prompts],
                "response_snippets": [
                    r[:1200] + (f"... <+{len(r) - 1200} chars>" if len(r) > 1200 else "")
                    for r in responses
                ],
            },
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "case_index": idx,
            "name": (case.get("_meta") or {}).get("name", ""),
            "ok": False,
            "input_len": len(text),
            "crash": f"{type(exc).__name__}: {exc}",
            "llm": {
                "model": model,
                "prompt_count": len(client.prompts) - before_prompt_count,
                "response_count": len(client.responses) - before_response_count,
            },
        }
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run the real 4-step ExcelAgent pipeline with DeepSeek LLM.")
    ap.add_argument("--cases-file", default=str(
        ROOT / "server" / "tests" / "cases" / "planner_style_inputs.json"))
    ap.add_argument("--only", default="", help="Comma/range case indexes, e.g. 1,2,4-6")
    ap.add_argument("--limit", type=int, default=3)
    ap.add_argument("--timeout", type=int, default=120)
    ap.add_argument("--report", default=str(
        REPORT_DIR / "four_step_deepseek_smoke_latest.json"))
    args = ap.parse_args()

    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print("Set DEEPSEEK_API_KEY before running this smoke test.", file=sys.stderr)
        return 2
    os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2", "1")
    os.environ.setdefault("CODEMAKER_DECOMPOSE_SINGLE_RETRY", "0")
    os.environ.setdefault("CODEMAKER_VERIFY_REPAIR_LOOP", "0")
    os.environ.setdefault("TABLE_CASE_EVAL_RUNNING", "1")
    os.environ.setdefault("DEEPSEEK_JSON_OBJECT", "1")
    os.environ.setdefault("DEEPSEEK_THINKING", "disabled")
    os.environ.setdefault("DEEPSEEK_MAX_TOKENS", "4096")

    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
    client = _DeepSeekClient(api_key=key, base_url=base_url, model=model)
    _install_deepseek_compat(client)
    cases_path = Path(args.cases_file)
    if not cases_path.is_absolute():
        cases_path = ROOT / cases_path
    cases = _read_cases(cases_path)
    indexes = _pick_indexes(len(cases), args.only, args.limit)

    results = []
    for idx in indexes:
        print(f"[case {idx}] running full 4-step pipeline with DeepSeek...", flush=True)
        result = run_case(cases[idx], idx, client, model, args.timeout)
        results.append(result)
        s1 = result.get("steps", {}).get(STEP1_PARSE, {})
        s3 = result.get("steps", {}).get(STEP3_EXECUTE, {})
        match = result.get("match", {})
        print(
            f"[case {idx}] ok={result.get('ok')} "
            f"s1_intents={s1.get('intent_count')} "
            f"s3_ok={s3.get('ok')} "
            f"matched={match.get('matched')}/{match.get('effective_expected')}",
            flush=True,
        )

    payload = {
        "cases_file": str(cases_path),
        "indexes": indexes,
        "model": model,
        "results": results,
        "summary": {
            "cases": len(results),
            "ok": sum(1 for r in results if r.get("ok")),
            "step1_ok": sum(1 for r in results
                            if r.get("steps", {}).get(STEP1_PARSE, {}).get("ok")),
            "step3_ok": sum(1 for r in results
                            if r.get("steps", {}).get(STEP3_EXECUTE, {}).get("ok")),
            "matched": sum((r.get("match") or {}).get("matched") or 0
                           for r in results),
            "effective_expected": sum((r.get("match") or {}).get("effective_expected") or 0
                                      for r in results),
            "prompt_count": len(client.prompts),
        },
    }
    report = Path(args.report)
    if not report.is_absolute():
        report = ROOT / report
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[report] {report}", flush=True)
    return 0 if all(r.get("ok") for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())

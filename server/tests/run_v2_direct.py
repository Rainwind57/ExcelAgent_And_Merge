#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""绕过 OrchestratorAgent router，直接调 agent.run_v2 跑焚天赤龙单条，
验证 P0 框架修复（①占位符豁免 + ②retries + ③hard 收口 + ⑤复合主键替换）
在净化候选下产出正确表/字段。

用法:
  python -m tests.run_v2_direct --cases-file <abs> --case-index 0
"""
import os
import sys
import json
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2", "1")
os.environ.setdefault("CODEMAKER_INTERACTIVE_REPAIR", "0")
os.environ.setdefault("CODEMAKER_AGENT_CHAIN", "1")
os.environ.setdefault("CODEMAKER_STEP5_TRACE", "1")
os.environ.setdefault("CODEMAKER_VERIFY_REPAIR_MAX_ROUNDS", "1")
os.environ.setdefault("TABLE_CASE_EVAL_RUNNING", "1")
os.environ.setdefault("CODEMAKER_PARSE_MULTI_TIMEOUT", "120")
# §deepseek 直连：在 import 任何 agent 模块前装 compat，把 CodemakerClient
# 的 LLM 方法替换为 DeepSeekClient，此后全栈 LLM 走 deepseek 官方 API。
# 由 .env DEEPSEEK_API_KEY/BASE_URL/MODEL 配置，不经 codemaker serve。
os.environ.setdefault("CODEMAKER_DECOMPOSE_TRACE", "1")
_dump_dir = os.environ.get("CODEMAKER_DECOMPOSE_DUMP_DIR", "")
if _dump_dir:
    os.makedirs(_dump_dir, exist_ok=True)

# §加载 .env：CodemakerClient 模块级读 CODEMAKER_USERNAME/PASSWORD（import 时固化），
# 必须在 import agent 之前加载 .env，否则 401。
_ROOT = Path(__file__).resolve().parents[2]
_env_file = _ROOT / ".env"
if _env_file.exists():
    with open(_env_file, encoding="utf-8") as _f:
        for _line in _f:
            _line = _line.strip()
            if not _line or _line.startswith("#") or "=" not in _line:
                continue
            _k, _, _v = _line.partition("=")
            _k = _k.strip()
            _v = _v.strip().strip('"').strip("'")
            os.environ.setdefault(_k, _v)

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# §deepseek compat 装载：必须在 import agent* 之前替换 CodemakerClient 方法。
# run_deepseek.py._install_deepseek_compat 已封装此逻辑，复用避免重复实现。
try:
    SERVER_DIR = Path(__file__).resolve().parents[1]
    if str(SERVER_DIR) not in sys.path:
        sys.path.insert(0, str(SERVER_DIR))
    from run_deepseek import _install_deepseek_compat
    _install_deepseek_compat()
except Exception as _e_ds:
    print(f"[warn] deepseek compat 装载失败，回退 codemaker serve: {_e_ds}",
          flush=True)

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _p(msg):
    print(msg, flush=True)


SEP = "=" * 70


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases-file", default=str(
        Path(__file__).resolve().parent / "cases" / "complex_task_chain_inputs.json"))
    ap.add_argument("--case-index", type=int, default=0)
    args = ap.parse_args()

    with open(args.cases_file, encoding="utf-8") as f:
        cases = json.load(f)
    if args.case_index >= len(cases):
        _p(f"case_index {args.case_index} 超出范围 (共 {len(cases)} 条)")
        return
    case = cases[args.case_index]
    text = case.get("input") or case.get("text") or ""
    _p(f"{SEP}\n# 用例: {Path(args.cases_file).name}[{args.case_index}]")
    _p(f"# input: {text[:80]}...")
    _p(f"# expected: {len(case.get('expected_answer') or [])} 条\n{SEP}")

    # sandbox copy
    res_dir = Path(os.environ.get("RES") or (ROOT / "resources"))
    sandbox = Path(tempfile.mkdtemp(prefix="run_v2_direct_"))
    sb_res = sandbox / "resources"
    shutil.copytree(res_dir, sb_res)
    os.environ["RES"] = str(sb_res)
    _p(f"[sandbox] {sb_res}")

    from services.agent_service import AgentService

    service = AgentService(resources_dir=sb_res, enable_skill=True)
    _p("[service] 构造 done")
    agent = getattr(service, "agent", None)
    if agent is None or not hasattr(agent, "run_v2"):
        _p("[err] agent 无 run_v2，退出")
        return
    _loc_ok = getattr(agent, "_locator_agent", None) is not None
    _dec_ok = getattr(agent, "_decompose_agent", None) is not None
    _val_ok = getattr(agent, "_validator_agent", None) is not None
    _p(f"[service] 三 agent: locator={_loc_ok} decompose={_dec_ok} validator={_val_ok}")

    # §诊断：monkeypatch decompose 路径，打印 raw LLM 响应 + 产出 fields，
    # 定位 reward 碎片 {42:'包也建一下',9:'800'} 究竟来自 LLM 产出还是兜底。
    _dec = getattr(agent, "_decompose_agent", None)
    if _dec is not None:
        _orig_sp = _dec._decompose_single_prompt
        _orig_par = _dec._decompose_parallel
        _orig_bl = _dec._splitter_baseline

        def _patched_sp(text, candidates, fk_block, per_to, column_signal=None):
            _p(f"\n[DDECOMP-SP] candidates={[c.stem for c in candidates]}")
            # patch 内部 create_session + prompt 抓 raw
            _client = getattr(_dec.parser, "client", None)
            _orig_cs = _orig_prompt = None
            if _client is not None:
                _orig_cs = _client.create_session
                _orig_prompt = _client.prompt
                def _wrapped_cs(directory="", model=""):
                    sr = _orig_cs(directory=directory, model=model)
                    _p(f"[DDECOMP-CS] ok={getattr(sr,'ok',None)} session_id={getattr(sr,'session_id','')!r} err={getattr(sr,'error','') or getattr(sr,'message','')}")
                    return sr
                def _wrapped_prompt(session_id, message, timeout=None, **kw):
                    resp = _orig_prompt(session_id, message, timeout=timeout, **kw)
                    _raw = getattr(resp, "response_text", "") or ""
                    _p(f"[DDECOMP-RAW] len={len(_raw)} ok={getattr(resp,'ok',None)} err={getattr(resp,'error','') or ''}")
                    _p(f"[DDECOMP-RAW] head={_raw[:400]!r}")
                    if len(_raw) > 400:
                        _p(f"[DDECOMP-RAW] tail={_raw[-300:]!r}")
                    return resp
                _client.create_session = _wrapped_cs
                _client.prompt = _wrapped_prompt
            res = _orig_sp(text, candidates, fk_block, per_to, column_signal=column_signal)
            if _orig_cs is not None and _client is not None:
                _client.create_session = _orig_cs
                _client.prompt = _orig_prompt
            _p(f"[DDECOMP-SP] 产出: {[(getattr(i,'table_hint',''), getattr(i,'fields',{})) for i in (res[0] if isinstance(res,tuple) else res)]}")
            return res

        def _patched_par(text, candidates, fk_block, per_to, column_signal=None):
            _p(f"\n[DDECOMP-PAR] candidates={[c.stem for c in candidates]}")
            res = _orig_par(text, candidates, fk_block, per_to, column_signal=column_signal)
            _p(f"[DDECOMP-PAR] 产出: {[(getattr(i,'table_hint',''), getattr(i,'fields',{})) for i in (res[0] if isinstance(res,tuple) else res)]}")
            return res

        def _patched_bl(text, candidates, fk_edges):
            _p(f"\n[DDECOMP-BL] baseline 兜底 candidates={[c.stem for c in candidates]}")
            res = _orig_bl(text, candidates, fk_edges)
            _p(f"[DDECOMP-BL] 产出: {[(getattr(i,'table_hint',''), getattr(i,'fields',{})) for i in res]}")
            return res

        _dec._decompose_single_prompt = _patched_sp
        _dec._decompose_parallel = _patched_par
        _dec._splitter_baseline = _patched_bl

    _p("[run_v2] 开始 ... (LLM 调用累积数分钟)")
    try:
        result = agent.run_v2(text, confirm_token=None, session_id="run_v2_direct")
    except Exception as e:
        _p(f"[run_v2] 异常: {e}")
        import traceback
        traceback.print_exc()
        return
    _p(f"{SEP}\n[run_v2] done")
    _p(f"  ok={getattr(result, 'ok', None)}")
    _p(f"  message={getattr(result, 'message', '')[:150]}")
    steps = getattr(result, "steps", None) or []
    _p(f"  steps: {len(steps)} 条")
    for i, s in enumerate(steps):
        _p(f"    [{i}] {getattr(s, 'name', '?')} ok={getattr(s, 'ok', '?')} "
            f"detail={(getattr(s, 'detail', '') or '')[:120]}")
        errs = getattr(s, "errors", None) or []
        for e in errs[:3]:
            _p(f"        err: {getattr(e, 'error_type', '?')} hard={getattr(e, 'is_hard', '?')} "
                f"msg={getattr(e, 'message', '')[:100]}")
    sub_tasks = getattr(result, "sub_tasks", None) or []
    _p(f"  sub_tasks: {len(sub_tasks)} 条")
    for st in sub_tasks[:8]:
        _p(f"    - {st.get('intent_action', '?')} ok={st.get('ok')} "
            f"tbl={st.get('table_stem', '')}/{st.get('table_sheet', '')} "
            f"msg={(st.get('message') or '')[:80]}")
    failures = getattr(result, "failures", None) or []
    _p(f"  failures: {len(failures)} 条")
    for f in failures[:10]:
        if isinstance(f, dict):
            _p(f"    - {f.get('type', '?')}: {f.get('root_cause', '')[:100]}")
    _p(f"{SEP}")


if __name__ == "__main__":
    main()

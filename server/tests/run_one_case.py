#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""单条跨表用例全链路打印调试脚本。

自动设置环境变量 + monkeypatch add_thinking + 4 断点 dump 结构体,
验证三 agent 链(Locator→Decompose→Validator)中间态,定位:
  bug1: decompose consumes 覆盖注入错误值 (decompose_agent.py:207-210)
  bug2: validator _suppress_over_produce 误删同表多行 (validator_agent.py:111-131)
  bug3: is_cross_table 触发偏窄漏 quest 表 (agent.py:3665)
  bug4: agent.py:3680 except 静默回退吞错 (默认 CODEMAKER_AGENT_CHAIN_RAISE=1 让异常上抛)

用法:
  python server/tests/run_one_case.py
  python server/tests/run_one_case.py --cases-file downloads/quest_npc_chain.json --case-index 0
  python server/tests/run_one_case.py --no-raise   # 降级回退,不抛异常

卡住排查: 每个 patch 进入前打印 "> entering",卡住时最后一条 ">" 即卡点。
单次 LLM 调用最多 90s (CODEMAKER_PARSE_MULTI_TIMEOUT),新链多次调用累积数分钟属正常。
"""
import os
import sys
import json
import copy
import shutil
import tempfile
import argparse
import traceback
from pathlib import Path

# ---- 环境变量必须在 import agent 之前设置 (agent.py:4000 模块级读 STEP5_TRACE) ----
os.environ.setdefault("CODEMAKER_AGENT_CHAIN", "1")
os.environ.setdefault("CODEMAKER_STEP5_TRACE", "1")
os.environ.setdefault("CODEMAKER_VERIFY_REPAIR_MAX_ROUNDS", "1")
os.environ.setdefault("CODEMAKER_AI_ASSIST", "1")
# eval 模式标记: 阻止 skill_updater 嵌套回归/写盘阻塞 (table_case_eval 也设)
os.environ.setdefault("TABLE_CASE_EVAL_RUNNING", "1")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # server/ → agent.* 命名空间（ROOT 仍为仓库根供文件路径用）

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _p(msg):
    """flush 打印, 卡住时仍能看到最后输出。"""
    print(msg, flush=True)


SEP = "=" * 70


def _j(obj):
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return repr(obj)


# ---- import (在此之后 agent 模块读环境变量) ----
from agent.excel.agent import TableAgent, AgentResult  # noqa: E402
from agent.excel.subagent.base import SubAgent  # noqa: E402
from agent.excel.subagent.locator_agent import LocatorAgent  # noqa: E402
from agent.excel.subagent.decompose_agent import DecomposeAgent  # noqa: E402
from agent.excel.subagent.validator_agent import ValidatorAgent  # noqa: E402
from agent.excel.operation_orchestrator import OperationOrchestrator  # noqa: E402
from agent.codemaker_client import CodemakerClient  # noqa: E402
from services.agent_service import AgentService  # noqa: E402
from tests.table_case_eval import (  # noqa: E402
    RES, diff_sandbox, match_case, build_pristine_index,
    _validate_fixture, _build_eval_sheet_aliases,
)

# ---- 启动时打印关键 env, 供确认 codemaker serve 可达 ----
_p(f"[env] CODEMAKER_SERVER_URL={os.environ.get('CODEMAKER_SERVER_URL', '<未设>')}")
_p(f"[env] CODEMAKER_AGENT_CHAIN={os.environ.get('CODEMAKER_AGENT_CHAIN')} "
   f"STEP5_TRACE={os.environ.get('CODEMAKER_STEP5_TRACE')} "
   f"TABLE_CASE_EVAL_RUNNING={os.environ.get('TABLE_CASE_EVAL_RUNNING')}")
_p(f"[env] CODEMAKER_PARSE_MULTI_TIMEOUT={os.environ.get('CODEMAKER_PARSE_MULTI_TIMEOUT', '90')}s/次")
_p(f"[env] RES={RES}")


# ============ 1. monkeypatch add_thinking: 同时 print ============
_orig_agent_add = AgentResult.add_thinking


def _patched_agent_add(self, phase, detail):
    _p(f"[{phase}] {detail}")
    return _orig_agent_add(self, phase, detail)


AgentResult.add_thinking = _patched_agent_add

_orig_sub_add = SubAgent.add_thinking


def _patched_sub_add(self, phase, detail):
    _p(f"[sub:{phase}] {detail}")
    return _orig_sub_add(self, phase, detail)


SubAgent.add_thinking = _patched_sub_add


# ============ 2. DUMP1: LocatorResult (进入前 print, 卡住时定位) ============
_orig_locate = LocatorAgent.locate


def _patched_locate(self, text):
    _p(f"\n> [DUMP1] entering LocatorAgent.locate ...  (text: {text[:60]}...)")
    res = _orig_locate(self, text)
    _p(f"{SEP}\n[DUMP1] LocatorResult")
    _p(f"  is_cross_table={res.is_cross_table}")
    _p(f"  candidates ({len(res.candidates)}):")
    for c in res.candidates:
        _p(f"    stem={c.stem!r} sheet={c.sheet!r} conf={c.confidence} "
           f"level={c.level!r} term={c.matched_term!r}")
    _p(f"  fk_edges ({len(res.fk_edges)}):")
    for e in res.fk_edges:
        _p(f"    {e.from_stem}/{e.from_sheet}.{e.from_column} -> "
           f"{e.to_stem}/{e.to_sheet}.{e.to_column}")
    if not res.is_cross_table:
        _p("  !!! is_cross_table=False -> 新链不触发,走单表/detect 闸门 (bug3 风险)")
    _p(SEP)
    return res


LocatorAgent.locate = _patched_locate


# ============ 3. DUMP2: DecomposeAgent split_intents ============
_orig_decompose = DecomposeAgent.decompose


def _patched_decompose(self, text, locator_result):
    _p(f"\n> [DUMP2] entering DecomposeAgent.decompose ... "
       f"(candidates={len(locator_result.candidates)})")
    res = _orig_decompose(self, text, locator_result)
    _p(f"{SEP}\n[DUMP2] DecomposeAgent split_intents ({len(res)} 条)")
    for i, si in enumerate(res):
        fields = getattr(si, "fields", {}) or {}
        _p(f"  [{i}] table={getattr(si,'table_hint',None)!r} "
           f"sheet={getattr(si,'sheet_hint',None)!r} "
           f"action={getattr(si,'action',None)!r} "
           f"produces={getattr(si,'produces',None)!r}")
        _p(f"      fields={_j(fields)}")
        for k, v in fields.items():
            if isinstance(v, str) and v.startswith("<") and v.endswith(">"):
                _p(f"      [bug1?] consumes 占位符 fields[{k!r}]={v!r} "
                    f"(若原是具体值如 item_id=10030 则被误改)")
    _p(SEP)
    return res


DecomposeAgent.decompose = _patched_decompose


# ============ 4. DUMP3: ValidatorAgent + 数量变化 ============
_orig_validate = ValidatorAgent.validate


def _patched_validate(self, intents, locator_result=None):
    _p(f"\n> [DUMP3] entering ValidatorAgent.validate ... (intents={len(intents)})")
    before_n = len(intents)
    before_snap = [copy.deepcopy(si) for si in intents]
    res = _orig_validate(self, intents, locator_result)
    after = res.get("intents", intents) if isinstance(res, dict) else intents
    after_n = len(after)
    _p(f"{SEP}\n[DUMP3] ValidatorAgent")
    if isinstance(res, dict):
        _p(f"  ok={res.get('ok')}")
        _p(f"  issues={_j(res.get('issues'))}")
        _p(f"  fixes={_j(res.get('fixes'))}")
    _p(f"  intents count: before={before_n} after={after_n}")
    if before_n != after_n:
        _p(f"  !!! 数量变化 ({before_n}->{after_n}) -> _suppress_over_produce 误删 (bug2 风险)")
        before_keys = {(getattr(s, 'table_hint', ''), getattr(s, 'sheet_hint', ''),
                        getattr(s, 'produces', '')) for s in before_snap}
        after_keys = {(getattr(s, 'table_hint', ''), getattr(s, 'sheet_hint', ''),
                       getattr(s, 'produces', '')) for s in after}
        for dropped in before_keys - after_keys:
            _p(f"      dropped: table={dropped[0]!r} sheet={dropped[1]!r} produces={dropped[2]!r}")
    for i, si in enumerate(after):
        _p(f"  [{i}] table={getattr(si,'table_hint',None)!r} "
           f"sheet={getattr(si,'sheet_hint',None)!r} "
           f"produces={getattr(si,'produces',None)!r} "
           f"fields={_j(getattr(si,'fields',{}) or {})}")
    _p(SEP)
    return res


ValidatorAgent.validate = _patched_validate


# ============ 5. DUMP4a: _split_intents_to_nl NLIntents ============
_orig_split_nl = TableAgent._split_intents_to_nl


def _patched_split_nl(self, split_intents, text):
    _p(f"\n> [DUMP4a] entering _split_intents_to_nl ... (split_intents={len(split_intents)})")
    res = _orig_split_nl(self, split_intents, text)
    _p(f"{SEP}\n[DUMP4a] _split_intents_to_nl -> NLIntents ({len(res)} 条)")
    for i, nl in enumerate(res):
        extras = getattr(nl, "extras", {}) or {}
        _p(f"  [{i}] action={getattr(nl,'action',None)!r} "
           f"table={getattr(nl,'table_hint',None)!r} "
           f"sheet={getattr(nl,'sheet_hint',None)!r} "
           f"locator={getattr(nl,'locator_value',None)!r}/{getattr(nl,'locator_field',None)!r}")
        _p(f"      extras={_j(extras)}")
    _p(SEP)
    return res


TableAgent._split_intents_to_nl = _patched_split_nl


# ============ 6. DUMP4b: _topo_order ============
_orig_topo = OperationOrchestrator._topo_order.__func__


def _patched_topo(cls, intents):
    _p(f"\n> [DUMP4b] entering _topo_order ... (intents={len(intents)})")
    order = _orig_topo(cls, intents)
    _p(f"[DUMP4b] _topo_order -> {order}")
    for idx in order:
        nl = intents[idx] if idx < len(intents) else None
        if nl is not None:
            _p(f"    #{idx} table={getattr(nl,'table_hint',None)!r} "
               f"sheet={getattr(nl,'sheet_hint',None)!r} "
               f"produces={(getattr(nl,'extras',{}) or {}).get('produces')}")
    return order


OperationOrchestrator._topo_order = classmethod(_patched_topo)


# ============ 7. DUMP4c: _capture_produced (produced 增量) ============
_orig_capture = OperationOrchestrator._capture_produced.__func__


def _patched_capture(cls, res, intent, produced, seq_counter):
    before_keys = set(produced.keys())
    _p(f"  > [DUMP4c] entering _capture_produced ... "
       f"table={getattr(intent,'table_hint',None)!r}/{getattr(intent,'sheet_hint',None)!r}")
    ret = _orig_capture(cls, res, intent, produced, seq_counter)
    new_keys = set(produced.keys()) - before_keys
    new_vals = {k: produced[k] for k in new_keys}
    ok = getattr(res, "ok", None) if res is not None else None
    _p(f"  [capture] table={getattr(intent,'table_hint',None)!r}/"
       f"{getattr(intent,'sheet_hint',None)!r} ok={ok} "
       f"produced_new={_j(new_vals)}")
    return ret


OperationOrchestrator._capture_produced = classmethod(_patched_capture)


# ============ 8. DUMP4d: _phase_execute 每条执行结果 ============
_orig_phase = TableAgent._phase_execute


def _patched_phase(self, intent, path, sheet, res, confirm_token):
    _p(f"\n  > [DUMP4d] entering _phase_execute ... "
       f"table={getattr(intent,'table_hint',None)!r} sheet={sheet!r} "
       f"action={getattr(intent,'action',None)!r} path={Path(path).name if path else None}")
    ret = _orig_phase(self, intent, path, sheet, res, confirm_token)
    ok = getattr(ret, "ok", None) if ret is not None else None
    needs_confirm = getattr(ret, "needs_confirm", False) if ret is not None else False
    _p(f"  [phase_exec <] ok={ok} needs_confirm={needs_confirm}")
    return ret


TableAgent._phase_execute = _patched_phase


# ============ 9. LLM 调用拦截: 每次 prompt 都 print (确认调了几次/卡哪次) ============
_orig_prompt = CodemakerClient.prompt


def _patched_prompt(self, session_id, message, timeout=None, model="", stage="", cancel_event=None):
    import time as _t
    _p(f"\n>>> [LLM] prompt  sid={str(session_id)[:16]}  timeout={timeout}  "
       f"stage={stage!r}  msg_len={len(message or '')}")
    _t0 = _t.time()
    try:
        ret = _orig_prompt(self, session_id, message, timeout=timeout, model=model,
                           stage=stage, cancel_event=cancel_event)
        _dt = _t.time() - _t0
        _p(f"<<< [LLM] done  {_dt:.1f}s  ok={getattr(ret,'ok',None)}  "
           f"err={getattr(ret,'error','')!r}")
        return ret
    except Exception as e:
        _dt = _t.time() - _t0
        _p(f"<<< [LLM] CRASH  {_dt:.1f}s  {type(e).__name__}: {e}")
        raise


CodemakerClient.prompt = _patched_prompt


# ============ 10. Pipeline 判定拦截 (确认走 Pipeline 还是 TableAgent.run) ============
try:
    import services.agent_service as _as_mod
    _orig_stp = _as_mod.should_trigger_pipeline

    def _patched_stp(text):
        r = _orig_stp(text)
        _p(f"[pipeline?] should_trigger_pipeline={r}  text={text[:50]!r}")
        return r

    _as_mod.should_trigger_pipeline = _patched_stp
except Exception as _e:
    _p(f"[patch] should_trigger_pipeline patch 失败: {_e}")


# ============ 主流程 ============
def run(case, cases_file, case_index, raise_on_err=True):
    os.environ["CODEMAKER_AGENT_CHAIN_RAISE"] = "1" if raise_on_err else "0"

    text = case["input"]
    expected = case.get("expected_answer", [])

    _p(f"\n{'#' * 70}")
    _p(f"# 用例: {Path(cases_file).name}[{case_index}]")
    _p(f"# raise_on_err={raise_on_err}  CODEMAKER_AGENT_CHAIN_RAISE={os.environ['CODEMAKER_AGENT_CHAIN_RAISE']}")
    _p(f"# input: {text}")
    _p(f"# expected_answer: {len(expected)} 条")
    _p(f"{'#' * 70}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="run_one_case_"))
    sandbox = tmp_dir / "resources"
    _p(f"\n[step1] copytree RES -> sandbox ({RES} -> {sandbox})")
    shutil.copytree(RES, sandbox)
    _p(f"[step1] copytree done")

    service = None
    try:
        _p(f"[step2] AgentService 构造 ... (可能连 codemaker serve)")
        service = AgentService(resources_dir=sandbox, enable_skill=True)
        _p(f"[step2] AgentService 构造 done")
        _ta = getattr(service, "agent", None)
        _loc_ok = getattr(_ta, "_locator_agent", None) is not None
        _dec_ok = getattr(_ta, "_decompose_agent", None) is not None
        _val_ok = getattr(_ta, "_validator_agent", None) is not None
        _p(f"[step2] 三 agent 状态: locator={_loc_ok} decompose={_dec_ok} validator={_val_ok}")
        if not _loc_ok:
            _p("[step2] !!! _locator_agent is None -> 新链不触发, 走 splitter 安全网 "
               "(bug1/2/3 全部无法验证, 需先修初始化)")
        session_id = f"run_one_case_{case_index}"
        _p(f"[step3] service.chat ... (首次 LLM 调用最多 90s/次, 新链多次累积数分钟)")
        resp = service.chat(text=text, session_id=session_id, dry_run=False)
        _p(f"[step3] service.chat done")
        if getattr(resp, "needs_confirm", False) and getattr(resp, "confirm_token", None):
            _p("\n[confirm] 二次确认提交...")
            resp = service.chat(text=text, session_id=session_id, dry_run=False,
                                confirm_token=resp.confirm_token, confirm_cascade=True)
            _p("[confirm] done")

        _p(f"\n{SEP}\n[FINAL] AgentService.chat 结果")
        _p(f"  ok={getattr(resp, 'ok', None)}")
        _p(f"  error={getattr(resp, 'error', '')!r}")
        _p(f"  reply_type={getattr(resp, 'reply_type', None)!r}")
        _p(f"  thinking_steps: {len(getattr(resp, 'thinking_steps', []) or [])} 条")
        steps = getattr(resp, "steps", []) or []
        _p(f"  steps: {len(steps)} 条")
        for s in steps:
            _p(f"    - {_j(s)}")
        _p(SEP)

        _p(f"\n[step4] diff_sandbox ...")
        actual_ops = diff_sandbox(sandbox, RES)
        _p(f"{SEP}\n[DIFF] 沙箱实际操作 ({len(actual_ops)} 条)")
        for i, op in enumerate(actual_ops):
            row = getattr(op, "row_content", None) or getattr(op, "row", None)
            _p(f"  [{i}] table={getattr(op,'table',None)!r} "
               f"sheet={getattr(op,'sheet',None)!r} "
               f"op={getattr(op,'operation',None)!r}")
            _p(f"      row={_j(row)}")
        _p(SEP)

        if expected:
            try:
                _p(f"\n[step5] match_case ...")
                pristine_idx = build_pristine_index(expected)
                fixture_errors = _validate_fixture(expected, pristine_idx)
                if fixture_errors:
                    _p(f"[fixture_errors] {_j(fixture_errors)}")
                sheet_alias_map = _build_eval_sheet_aliases()
                entries, extra_ops_list = match_case(expected, actual_ops, pristine_idx,
                                                     sheet_alias_map=sheet_alias_map)
                n_eff = len([r for r in entries if getattr(r, "status", "") != "precondition_missing"])
                matched = [r for r in entries if getattr(r, "status", "") == "matched"]
                _p(f"[MATCH] effective={n_eff} matched={len(matched)} "
                    f"extra_ops={len(extra_ops_list)}")
                for r in entries:
                    _p(f"    - {getattr(r,'status','?')} table={getattr(r,'table','?')} "
                       f"sheet={getattr(r,'sheet','?')} "
                       f"row_located={getattr(r,'row_located',None)} "
                       f"field_score={getattr(r,'field_score',None)}")
            except Exception as e:
                _p(f"\n[MATCH] 判分跳过: {type(e).__name__}: {e}")

        return resp
    finally:
        try:
            if service is not None and getattr(service, "_file_watcher", None) is not None:
                service._file_watcher.stop()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(description="单条跨表用例全链路打印调试")
    ap.add_argument("--cases-file",
                    default=str(ROOT / "downloads" / "quest_npc_double_option.json"),
                    help="测例 JSON 路径")
    ap.add_argument("--case-index", type=int, default=0, help="测例下标(0-based)")
    ap.add_argument("--no-raise", action="store_true",
                    help="不抛异常,降级回退 (CODEMAKER_AGENT_CHAIN_RAISE=0)")
    args = ap.parse_args()

    cases_file = Path(args.cases_file)
    if not cases_file.is_absolute():
        cases_file = ROOT / cases_file
    with open(cases_file, encoding="utf-8") as f:
        cases = json.load(f)
    if args.case_index >= len(cases):
        _p(f"case_index {args.case_index} 超出范围 (共 {len(cases)} 条)")
        sys.exit(1)
    case = cases[args.case_index]

    try:
        run(case, str(cases_file), args.case_index, raise_on_err=not args.no_raise)
    except Exception as e:
        _p(f"\n{'!' * 70}\n[CRASH] {type(e).__name__}: {e}\n{traceback.format_exc()}\n{'!' * 70}")
        sys.exit(2)


if __name__ == "__main__":
    main()

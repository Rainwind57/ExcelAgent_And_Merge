#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""PK 冲突真全链路验证（复现用户场景）。

链路：resources/reward.xlsx(含 99001, max 100602) + 真 LLM ParseAgent
+ 真 validate_two_layer(Step2) + 真 _phase_execute(Step3) + 真 chat_stream
+ reply_queue 模拟前端 /api/agent/reply accept。

指令：新增一个奖励包叫测试奖励包,reward_id 99001,每日限领 1 次,必给道具 10001 共 5 个

预期（改动后）：
  Step2 检测 99001 占用 -> ask(mode_hint=pk_conflict, suggested_id=100603)
  -> 前端 accept -> intent PK 改写 100603 -> Step3 干净写入 -> Step4 报成功
  无 pk_conflict / verify_repair_exhausted 落 Step3。

用法: python server/tests/run_pk_fullchain_real.py
"""
import os
import sys
import shutil
import tempfile
import asyncio
import queue as _queue
import threading
from pathlib import Path

os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2", "1")  # V2 默认 ON（原 CODEMAKER_4STEP_LOOP 已废弃）
os.environ.setdefault("CODEMAKER_AGENT_CHAIN", "1")
os.environ.setdefault("CODEMAKER_INTERACTIVE_REPAIR", "1")
os.environ.setdefault("TABLE_CASE_EVAL_RUNNING", "1")
os.environ.setdefault("CODEMAKER_VERIFY_REPAIR_MAX_ROUNDS", "1")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _p(msg):
    print(msg, flush=True)


SEP = "=" * 70
USER_CMD = os.environ.get("PK_CMD", (
    "新增一个奖励包叫测试奖励包,reward_id 99001,"
    "每日限领 1 次,必给道具 10001 共 5 个"))


def _read_reward_pks(sandbox_resources):
    from agent.excel.cli.real_cli import RealCodeMakerCLI
    cli = RealCodeMakerCLI(workspace=Path(sandbox_resources))
    rp = [x for x in cli.list_tables() if x.stem == "reward"]
    if not rp:
        return []
    rows = cli.read_sheet(rp[0], "Reward")
    pks = []
    for r in rows:
        if r and r[0] is not None:
            try:
                pks.append(int(r[0]))
            except (ValueError, TypeError):
                pks.append(r[0])
    return pks


def main():
    from services.agent_service import AgentService
    from agent.excel.subagent.validator_agent import ValidatorAgent

    # ---- 埋点：wrap validate_two_layer 看真路径 Core4 为何没触发 ----
    _orig_vtl = ValidatorAgent.validate_two_layer

    def _dbg_vtl(self, intents, schema_getter=None, locator_result=None,
                 data_getter=None):
        _p(f"\n[DBG-VTL] enter vtl intents={len(intents)} "
           f"data_getter={'有' if data_getter else '无'} "
           f"ask_cb={'有' if getattr(self,'_ask_callback',None) else '无'}")
        for it in intents:
            flds = (getattr(it, "extras", None) or {}).get("fields") or {}
            _p(f"  intent action={getattr(it,'action',None)!r} "
               f"table={getattr(it,'table_hint',None)!r} "
               f"sheet={getattr(it,'sheet_hint',None)!r} "
               f"fields_keys={list(flds.keys())}")
            if data_getter is not None and getattr(it, "action", "") == "add":
                try:
                    d = data_getter(it)
                    ev = (d or {}).get("existing_values") or {}
                    _p(f"    dg: path={d.get('path')} sheet={d.get('sheet')} "
                       f"ev_keys={list(ev.keys())[:6]}")
                    for k, v in ev.items():
                        if "id" in k.lower() and v:
                            _p(f"    dg col[{k}] has 99001: "
                               f"{99001 in v or '99001' in [str(x) for x in v]} "
                               f"(vals_n={len(v)})")
                except Exception as e:
                    _p(f"    dg EXC: {type(e).__name__}: {e}")
        res = _orig_vtl(self, intents, schema_getter=schema_getter,
                        locator_result=locator_result, data_getter=data_getter)
        _sk = [id(i) for i in intents
               if getattr(getattr(i, "validation", None), "skipped", False)]
        _p(f"[DBG-VTL] exit ok={res.get('ok')} tips={len(res.get('tips') or [])} "
           f"skipped={_sk}")
        return res

    ValidatorAgent.validate_two_layer = _dbg_vtl

    # 埋点：wrap _apply_ai_intent_check 看 Step1 是否发生 parse_multi 重拆
    _orig_aic = AgentService.__dict__.get("_apply_ai_intent_check")
    if _orig_aic is None:
        # 取 agent 实例方法
        from agent.excel.core.agent import TableAgent as _TA
        _orig_aic = _TA._apply_ai_intent_check

        def _dbg_aic(self, intents, text, _stream_res):
            _before = len(intents)
            out = _orig_aic(self, intents, text, _stream_res)
            _after = len(out)
            if _after != _before:
                _p(f"[DBG-AIC] _apply_ai_intent_check 重拆: {_before} -> {_after} 条"
                   f"（若 {_before}>1 且重拆后 PK 仍 99001，旧版会丢 Core4 改写）")
            # 打重拆后 reward intent 的 PK
            for it in (out if isinstance(out, list) else []):
                if getattr(it, "action", "") == "add":
                    flds = (getattr(it, "extras", None) or {}).get("fields") or {}
                    for k, v in flds.items():
                        if "id" in str(k).lower():
                            _p(f"[DBG-AIC] 重拆后 intent {k}={v} table={getattr(it,'table_hint',None)}")
            return out
        _TA._apply_ai_intent_check = _dbg_aic

    orig_pks = set(_read_reward_pks(ROOT / "resources"))
    _p(f"[pre] resources/reward.xlsx: {len(orig_pks)} 行, has 99001={99001 in orig_pks}, "
       f"max={max((x for x in orig_pks if isinstance(x,int)), default=None)}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="pk_fullchain_"))
    sandbox = tmp_dir / "resources"
    _p(f"[step1] copytree resources -> {sandbox}")
    shutil.copytree(ROOT / "resources", sandbox)

    reply_q: _queue.Queue = _queue.Queue()
    cancel_event = threading.Event()
    service = None
    try:
        _p(f"[step2] AgentService 构造（连 codemaker serve）...")
        service = AgentService(resources_dir=sandbox, enable_skill=True)
        _p(f"[step2] done. agent={type(service.agent).__name__}")
        session_id = "pk_fullchain_real"

        async def _drive():
            _p(f"[step3] chat_stream ... (真 LLM ParseAgent, 可能 1-3 分钟)")
            ask_seen = []
            done_result = [None]
            gen = service.chat_stream(
                text=USER_CMD, session_id=session_id, dry_run=False,
                cancel_event=cancel_event, reply_queue=reply_q,
            )
            async for etype, payload in gen:
                if etype == "thinking":
                    ph = (payload or {}).get("phase", "")
                    det = (payload or {}).get("detail", "")
                    if ph in ("校验", "修复", "汇总", "执行") or "核心4" in det or "pk_conflict" in det:
                        _p(f"  [{ph}] {det[:160]}")
                elif etype == "ask":
                    ask_seen.append(payload)
                    mh = (payload or {}).get("mode_hint")
                    sug = (payload or {}).get("suggested_id")
                    _p(f"\n{SEP}\n[ASK] mode_hint={mh} suggested_id={sug}\n"
                       f"  suggestion={payload.get('suggestion','')[:120]}\n{SEP}")
                    if mh == "pk_conflict":
                        _p("[reply] 前端 accept_suggest=True -> reply_queue.put")
                        reply_q.put({"mode": "field", "accept_suggest": True})
                    else:
                        _p("[reply] 非 pk_conflict ask，skip")
                        reply_q.put({"mode": "skip"})
                elif etype == "done":
                    done_result[0] = payload
                    _p(f"[done] 收到最终结果")
                elif etype == "error":
                    _p(f"[error] {payload}")
            return ask_seen, done_result[0]

        ask_seen, done = asyncio.run(_drive())

        _p(f"\n{SEP}\n[VERIFY] 结果校验\n{SEP}")
        # done 可能是 AgentChatResponse pydantic 对象或 dict，统一取值
        def _g(obj, key, default=None):
            if obj is None:
                return default
            if isinstance(obj, dict):
                return obj.get(key, default)
            return getattr(obj, key, default)

        ok = _g(done, "ok")
        failures = _g(done, "failures") or []
        steps = _g(done, "steps") or []
        step_names = [s.get("name") if isinstance(s, dict) else getattr(s, "name", "")
                      for s in steps]
        msg = _g(done, "message", "")

        _p(f"  ok={ok}")
        _p(f"  message={msg[:160]}")
        _p(f"  ask 次数={len(ask_seen)} (pk_conflict: "
           f"{sum(1 for a in ask_seen if a.get('mode_hint')=='pk_conflict')})")
        _p(f"  steps 含 pk_conflict: {'pk_conflict' in step_names}")
        _p(f"  failures: {len(failures)}")
        for f in failures[:5]:
            _p(f"    - {f}")

        # 通用 per-table 行数 diff（覆盖 reward + 跨表多表场景）
        from agent.excel.cli.real_cli import RealCodeMakerCLI as _RC
        _cli2 = _RC(workspace=sandbox)
        _p(f"  --- per-table 行数 diff（sandbox）---")
        new_tables = {}
        for tp in _cli2.list_tables():
            try:
                shs = _cli2.get_sheets(tp)
            except Exception:
                shs = []
            for sh in shs:
                try:
                    n = len(_cli2.read_sheet(tp, sh))
                except Exception:
                    n = -1
                new_tables[(tp.stem, sh)] = n
        # 原始 resources 行数
        _cli0 = _RC(workspace=ROOT / "resources")
        grown = []
        for tp in _cli0.list_tables():
            for sh in _cli0.get_sheets(tp):
                try:
                    o = len(_cli0.read_sheet(tp, sh))
                except Exception:
                    continue
                n = new_tables.get((tp.stem, sh), o)
                if n != o:
                    grown.append((tp.stem, sh, o, n))
        for stem, sh, o, n in grown:
            _p(f"    {stem}/{sh}: {o} -> {n} (+{n-o})")
        if not grown:
            _p("    （无表行数变化）")

        verdict = []
        v1 = any(a.get("mode_hint") == "pk_conflict" for a in ask_seen)
        verdict.append(("触发 pk_conflict ask（若有冲突）", v1 or not ask_seen))
        # 所有 pk_conflict ask 的 suggested_id 都非 None（真值非「ID 值 None」bug）
        v2 = all(a.get("suggested_id") is not None
                 for a in ask_seen if a.get("mode_hint") == "pk_conflict")
        verdict.append(("所有 pk_conflict ask 有真 suggested_id（非 None）", v2))
        v3 = ok is True
        verdict.append(("最终 ok=True", v3))
        v4 = "pk_conflict" not in step_names
        verdict.append(("Step3 steps 无 pk_conflict（冲突在 Step2 解）", v4))
        v5 = not any("verify_repair_exhausted" in str(f) for f in failures)
        verdict.append(("无 verify_repair_exhausted", v5))
        v6 = len(failures) == 0
        verdict.append((f"failures=0（实际 {len(failures)}）", v6))

        _p(f"\n{SEP}\n[VERDICT]")
        all_ok = True
        for name, v in verdict:
            _p(f"  [{'OK' if v else 'FAIL'}] {name}")
            all_ok = all_ok and v
        _p(f"\n{'PASS 全链路通过' if all_ok else 'FAIL 有未通过项'}\n{SEP}")
        return 0 if all_ok else 1

    finally:
        try:
            if service is not None and getattr(service, "_file_watcher", None) is not None:
                service._file_watcher.stop()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

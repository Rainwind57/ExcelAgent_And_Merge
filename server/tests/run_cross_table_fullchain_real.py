#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""跨表多意图冲突前置校验真 LLM 全链路验证（封印魔龙 8 表）。

验证 4 要求：
  A: PK 冲突/placeholder/dangling FK 在 Step2 ask（不在 Step3 _do_append/_run_verify_repair_loop）
  B: ask payload 含 user_friendly 大白话字段
  C: （前端折叠续跑 — 此脚本验后端续跑链路：reply 后 chat_stream 继续）
  D: 墙钟 < 180s（R7 health 门控生效时不卡 90-180s hang）

链路：copytree resources -> AgentService.chat_stream(真 LLM)
+ reply_queue 模拟前端 /api/agent/reply（pk_conflict accept / placeholder skip）
+ 验收 done.ok / failures / ask.user_friendly / per-table 行数无重复

用法:
  python server/tests/run_cross_table_fullchain_real.py
  PK_CMD="..." python server/tests/run_cross_table_fullchain_real.py  # 自定义指令
"""
import os
import sys
import shutil
import tempfile
import asyncio
import queue as _queue
import threading
import time
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

# 封印魔龙跨 8 表（bench_4step.py 样例 1）
USER_CMD = os.environ.get("PK_CMD", (
    '加一个新的主线任务叫"封印魔龙"，任务号 250600，归到任务组 600（组名"封魔录"，主线类型），'
    '限定条件填 hero_all。描述写"前往魔龙巢穴，击败魔龙 Razak 释放封印"。'
    '目标类型用 Combat，打赢战斗 25060001 即可，奖励包给 100600。'
    '战斗 25060001 也一起配上：在战场 10050 的坐标 (120,0,80) 打一只 PVE 怪，'
    '怪物名字叫"魔龙 Razak"，model_id 1099，技能列表 9001/9002/9003，AI 用 aggressive_ai，'
    '等级公式 50，气血斜率 30、气血基础 8000，物攻斜率 25、物攻基础 6000，'
    '打赢给奖励包 100600，输了和平局都不给。'
    '奖励包 100600 建一下：叫"封魔首通奖励"，每日限领 1 次，必给道具 10001 共 10 个，'
    '另有 100% 概率给经验、100% 给金币、金币公式填 500；额外再加一个概率掉落池，'
    'pool_id 2001，随机词条 StamCon 范围 1-50 权重 10、ManaCon 范围 1-50 权重 10，能力类型都填 1。'
    '再配一个任务引导 NPC 叫"封魔长老"，model_id 1015，放在战场 10050 坐标 (110,0,70)，'
    '玩家点击他弹出对话"封印即将松动，请前往巢穴击败魔龙"，给一个选项"我这就去"选完什么都不做结束对话。'
))

WALL_CLOCK_TARGET_S = 180  # 要求 D


def _read_table_pks(sandbox_resources, stemsheets=None):
    """读指定表 PK 列（首列）行数 + 重复检测。stemsheets 给定时只读这些表。"""
    from agent.excel.cli.real_cli import RealCodeMakerCLI
    cli = RealCodeMakerCLI(workspace=Path(sandbox_resources))
    result = {}
    # 先一次 list_tables，按 stem 过滤（避免对 83 表都 get_sheets/read_sheet）
    all_tables = cli.list_tables()
    if stemsheets:
        target_stems = {s.split("/")[0] for s in stemsheets} | set(stemsheets)
        tables = [tp for tp in all_tables if tp.stem in target_stems]
    else:
        tables = all_tables
    for tp in tables:
        stem = tp.stem
        for sh in (cli.get_sheets(tp) or []):
            key = f"{stem}/{sh}"
            if stemsheets and key not in stemsheets and stem not in stemsheets:
                continue
            try:
                rows = cli.read_sheet(tp, sh)
            except Exception:
                rows = []
            pks = []
            for r in rows:
                if r and r[0] is not None:
                    pks.append(r[0])
            result[key] = {"rows": len(rows), "pks": pks}
    return result


# 跨表封印魔龙涉及的目标表 stem（验收只读这些表，避免读全 83 表慢）
_TARGET_STEMS = {
    "quest", "quest_group", "combat", "pve_combat_npc", "reward",
    "entity_prefab", "interaction", "spawn_world_entity", "space",
    "gameplay_ability_choice_pool",
}


def main():
    from services.agent_service import AgentService

    _p(f"[pre] 指令长度 {len(USER_CMD)} 字符")
    _p(f"[pre] 目标墙钟 < {WALL_CLOCK_TARGET_S}s")

    orig_tables = _read_table_pks(ROOT / "resources", stemsheets=_TARGET_STEMS)
    _p(f"[pre] resources 目标表数: {len(orig_tables)}")

    tmp_dir = Path(tempfile.mkdtemp(prefix="cross_table_fullchain_"))
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
        session_id = "cross_table_fullchain_real"

        async def _drive():
            _p(f"[step3] chat_stream ... (真 LLM 跨 8 表，可能 2-5 分钟)")
            ask_seen = []
            done_result = [None]
            t0 = time.perf_counter()
            gen = service.chat_stream(
                text=USER_CMD, session_id=session_id, dry_run=False,
                cancel_event=cancel_event, reply_queue=reply_q,
            )
            async for etype, payload in gen:
                if etype == "thinking":
                    ph = (payload or {}).get("phase", "")
                    det = (payload or {}).get("detail", "")
                    if ph in ("校验", "修复", "汇总", "执行", "健康门控") \
                       or "核心4" in det or "pk_conflict" in det \
                       or "placeholder" in det or "forward_ref" in det:
                        _p(f"  [{ph}] {det[:160]}")
                elif etype == "ask":
                    ask_seen.append(payload)
                    mh = (payload or {}).get("mode_hint")
                    sug = (payload or {}).get("suggested_id")
                    uf = (payload or {}).get("user_friendly")
                    _p(f"\n{SEP}\n[ASK] mode_hint={mh} suggested_id={sug}")
                    _p(f"  user_friendly={uf}")
                    _p(f"  root_cause={(payload or {}).get('root_cause','')[:120]}")
                    _p(f"  suggestion={(payload or {}).get('suggestion','')[:120]}\n{SEP}")
                    # 模拟前端：pk_conflict accept，placeholder/dangling FK skip
                    if mh == "pk_conflict":
                        _p("[reply] pk_conflict -> accept_suggest=True")
                        reply_q.put({"mode": "field", "accept_suggest": True})
                    else:
                        _p("[reply] 非 pk_conflict -> skip（避免阻塞）")
                        reply_q.put({"mode": "skip"})
                elif etype == "done":
                    done_result[0] = payload
                    _p(f"[done] 收到最终结果")
                elif etype == "error":
                    _p(f"[error] {payload}")
            wall = time.perf_counter() - t0
            return ask_seen, done_result[0], wall

        ask_seen, done, wall = asyncio.run(_drive())
        _p(f"\n{SEP}\n[VERIFY] 墙钟={wall:.1f}s (目标<{WALL_CLOCK_TARGET_S}s)\n{SEP}")

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
        _p(f"  ask 次数={len(ask_seen)}")
        for i, a in enumerate(ask_seen):
            _p(f"    ask[{i}] mode_hint={a.get('mode_hint')} "
                f"has_user_friendly={'user_friendly' in a}")
        _p(f"  steps 含 pk_conflict: {'pk_conflict' in step_names}")
        _p(f"  failures: {len(failures)}")
        for f in failures[:8]:
            _p(f"    - {f}")

        # per-table 行数 diff + 重复 PK 检测（只读目标表）
        new_tables = _read_table_pks(sandbox, stemsheets=_TARGET_STEMS)
        _p(f"\n  --- per-table 行数 diff（sandbox）---")
        grown = []
        for key, ninfo in new_tables.items():
            oinfo = orig_tables.get(key, {"rows": 0, "pks": []})
            if ninfo["rows"] != oinfo["rows"]:
                grown.append((key, oinfo["rows"], ninfo["rows"], ninfo["pks"]))
        for key, o, n, pks in grown:
            _dup = len(pks) != len(set(pks)) if pks else False
            _p(f"    {key}: {o} -> {n} (+{n-o}) {'⚠️PK重复!' if _dup else ''}")
        if not grown:
            _p("    （无表行数变化）")

        verdict = []
        # 要求 A: 冲突在 Step2 ask，不在 Step3 steps
        v_a1 = "pk_conflict" not in step_names
        verdict.append(("A: Step3 steps 无 pk_conflict（冲突 Step2 解）", v_a1))
        # 要求 A: 无 verify_repair_exhausted 假失败
        v_a2 = not any("verify_repair_exhausted" in str(f) for f in failures)
        verdict.append(("A: 无 verify_repair_exhausted 假失败", v_a2))
        # 要求 A: 无重复 PK 行（reward 不 100605+100606）
        _dup_pks = []
        for key, o, n, pks in grown:
            if pks and len(pks) != len(set(pks)):
                _dup_pks.append(key)
        v_a3 = not _dup_pks
        verdict.append((f"A: 无重复 PK 行（重复表:{_dup_pks or '无'}）", v_a3))
        # 要求 B: 所有 ask 有 user_friendly 字段
        v_b = all("user_friendly" in a for a in ask_seen) if ask_seen else True
        verdict.append(("B: 所有 ask 含 user_friendly 大白话", v_b))
        # 要求 D: 墙钟 < 180s
        v_d = wall < WALL_CLOCK_TARGET_S
        verdict.append((f"D: 墙钟 {wall:.1f}s < {WALL_CLOCK_TARGET_S}s", v_d))
        # failures 只含真实未解决项（非 id_reallocate 自撞假失败）
        _fake_fails = [f for f in failures
                       if isinstance(f, dict)
                       and str(f.get("root_cause", "")) == "id_reallocate_self_hit"]
        v_f = not _fake_fails
        verdict.append((f"failures 无 id_reallocate 自撞假失败（假:{len(_fake_fails)}）", v_f))

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

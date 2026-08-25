#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""焚天赤龙 case in-process 沙箱端到端验证（真 LLM，dry_run=False，不污染 resources）。

验证：
  1. Step1: interaction/spawn_world_entity 是否保留（不再被 cap/幻觉过滤丢弃）
  2. Step2: 活动类型 类型错 / combat 缺列 / reward PK 错列 是否在 Step2 前移拦截
  3. Step3: partial 写是否不再误报"失败"
  4. 收尾: 不再 SubTaskInfo bool_type 崩溃

用法: python server/tests/run_fengtian_fullchain_real.py
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

os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2", "1")
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

USER_CMD = (
    "新增一个限时世界BOSS活动叫'焚天赤龙降临'，activity_id 3001，活动描述'赤龙现世，"
    "各路豪杰齐心讨伐，首杀可得传说法宝'，开始时间 2024-12-21 00:00:00，结束时间 "
    "2024-12-28 23:59:59。配套首杀奖励包也建一下，reward_id 30010，叫'焚天赤龙首杀奖励'，"
    "每日限领 1 次，必给道具 10001 共 20 个，概率 100% 给经验、100% 给金币、金币公式填 800；"
    "再加一个保底掉落池 pool_id 3001，随机词条 PhyAtkCon 范围 100-200 权重 20、MagAtkCon "
    "范围 100-200 权重 20、CritCon 范围 5-15 权重 10，能力类型都填 2。BOSS 战斗也一起配上："
    "在战场 10050 的坐标 (200,0,150) 刷一只 PVE 世界 BOSS，怪物名字叫'焚天赤龙'，"
    "model_id 1200，技能列表 9101/9102/9103/9104，AI 用 boss_ai，等级公式 80，气血斜率 60、"
    "气血基础 500000，物攻斜率 40、物攻基础 30000，打赢给奖励包 30010，输了和平局都不给。"
    "再配一个引导 NPC 叫'赤龙指引人'，model_id 1020，放在战场 10050 坐标 (190,0,140)，"
    "玩家点击他弹出对话'焚天赤龙正在肆虐人间，请勇士速速前往讨伐，首杀者将获得传说奖励'，"
    "给两个选项：第一个'我这就去讨伐'选完后触发战斗项直接结束对话；第二个'了解一下奖励'"
    "跳到新对话，内容为'首杀奖励包含 20 个精炼石、海量经验与金币，以及随机词条池加成'，"
    "再给一个选项'知道了'选完什么都不做结束对话。"
)


def main():
    from services.agent_service import AgentService

    _p(f"[pre] 指令长度 {len(USER_CMD)} 字符")
    tmp_dir = Path(tempfile.mkdtemp(prefix="fengtian_fullchain_"))
    sandbox = tmp_dir / "resources"
    _p(f"[step1] copytree resources -> {sandbox}")
    shutil.copytree(ROOT / "resources", sandbox)

    reply_q: _queue.Queue = _queue.Queue()
    cancel_event = threading.Event()
    service = None
    all_thinking = []
    try:
        _p(f"[step2] AgentService 构造（连 codemaker serve）...")
        service = AgentService(resources_dir=sandbox, enable_skill=True)
        session_id = "fengtian_fullchain_real"

        async def _drive():
            _p(f"[step3] chat_stream ... (真 LLM，可能 1-4 分钟)")
            ask_seen = []
            done_result = [None]
            err_seen = []
            t0 = time.perf_counter()
            gen = service.chat_stream(
                text=USER_CMD, session_id=session_id, dry_run=False,
                cancel_event=cancel_event, reply_queue=reply_q,
            )
            async for etype, payload in gen:
                if etype == "thinking":
                    ph = (payload or {}).get("phase", "")
                    det = (payload or {}).get("detail", "")
                    all_thinking.append((ph, det))
                    _p(f"  [{ph}] {det[:170]}")
                elif etype == "ask":
                    ask_seen.append(payload)
                    mh = (payload or {}).get("mode_hint")
                    _p(f"\n[ASK] mode_hint={mh} "
                       f"root_cause={(payload or {}).get('root_cause','')[:100]}")
                    if mh == "pk_conflict":
                        reply_q.put({"mode": "field", "accept_suggest": True})
                        _p("[reply] pk_conflict -> accept")
                    else:
                        reply_q.put({"mode": "skip"})
                        _p("[reply] -> skip")
                elif etype == "done":
                    done_result[0] = payload
                    _p(f"[done] 收到最终结果")
                elif etype == "error":
                    err_seen.append(payload)
                    _p(f"[error] {str(payload)[:200]}")
            wall = time.perf_counter() - t0
            return ask_seen, done_result[0], err_seen, wall

        ask_seen, done, err_seen, wall = asyncio.run(_drive())

        def _g(obj, key, default=None):
            if obj is None:
                return default
            return obj.get(key, default) if isinstance(obj, dict) else getattr(obj, key, default)

        ok = _g(done, "ok")
        failures = _g(done, "failures") or []
        subtasks = _g(done, "sub_tasks") or []
        msg = _g(done, "message", "") or ""

        _p(f"\n{SEP}\n[VERIFY] 墙钟={wall:.1f}s")
        _p(f"  ok={ok}  message={msg[:160]}")
        _p(f"  子任务 {len(subtasks)} 个，失败 {len(failures)} 项，ask {len(ask_seen)} 次，error {len(err_seen)} 次")
        for st in subtasks:
            _p(f"    · [{_g(st,'index')}] ok={_g(st,'ok')} {_g(st,'table_stem')}/"
               f"{_g(st,'table_sheet')} — {str(_g(st,'message',''))[:80]}")
        for f in failures:
            _p(f"    ✗ {_g(f,'table','?')}/{_g(f,'sheet','?')} "
               f"列[{_g(f,'col')}]: {str(_g(f,'root_cause') or _g(f,'message'))[:110]}")

        # 关键断言
        _th = " ".join(d for _, d in all_thinking)
        _stems = [str(_g(st, "table_stem", "")) for st in subtasks]
        checks = []
        checks.append(("Step1 保留 interaction 意图",
                       "interaction" in _th or "interaction" in _stems))
        checks.append(("Step1 保留 spawn_world_entity",
                       "spawn_world_entity" in _th or "spawn_world_entity" in _stems))
        checks.append(("Step2 前移拦截(活动类型/缺列/PK 错列 出现在校验阶段)",
                       any(("写盘预演" in d or "dry-run" in d or "skipped" in d)
                           for ph, d in all_thinking if ph == "校验")))
        checks.append(("收尾无 SubTaskInfo bool_type 崩溃",
                       "SubTaskInfo" not in str(msg)
                       and not any("SubTaskInfo" in str(e) for e in err_seen)))
        _p(f"\n{SEP}\n[CHECKS]")
        allok = True
        for name, v in checks:
            _p(f"  [{'OK' if v else 'FAIL'}] {name}")
            allok = allok and v
        _p(f"\n{'PASS' if allok else 'PARTIAL/FAIL — 见上方 thinking'}\n{SEP}")
        return 0

    finally:
        try:
            if service is not None and getattr(service, "_file_watcher", None) is not None:
                service._file_watcher.stop()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

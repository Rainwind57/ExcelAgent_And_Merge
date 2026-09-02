# -*- coding: utf-8 -*-
"""真 LLM 全链路探针：九尾天狐 + 进化链（验证 cross_ref_linker + 可观测性）。

用后即删。sandbox 复制 resources，不动真实文件。验证：
  1. pet.Pet 新增九尾天狐行；pet_evolve.PetEvolveData 新增进化行。
  2. 进化行的「宠物id」(源灵兽, 第3列) 被 LLM 判定/回填为新增的九尾天狐灵兽id
     （cross_ref_linker 生效，非硬编码）。
  3. 打印 Step1 可观测性 metrics（候选分层 + LLM prompt/dur/timeout）。
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

ROOT = Path(__file__).resolve().parents[2]
for _line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line:
        continue
    _k, _v = _line.split("=", 1)
    if _k.strip() and _k.strip() not in os.environ:
        os.environ[_k.strip()] = _v.strip()
os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2", "1")
os.environ.setdefault("CODEMAKER_VERIFY_REPAIR_MAX_ROUNDS", "1")
os.environ.setdefault("CODEMAKER_DECOMPOSE_TIMEOUT", "60")
# 可选：开启 schema_budget 灰度（本次默认关闭以验正确性；设 >0 可看 prompt 裁剪）
# os.environ["CODEMAKER_DECOMPOSE_SCHEMA_CHAR_BUDGET"] = "800"

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


def _p(m):
    print(m, flush=True)


USER_CMD = (
    '新增传说灵兽"九尾天狐"，model_id 1070，品质 3，元素 fire，蛋道具 28999，'
    '出战需 60 级，体力资质 6000、物攻资质 2000、法攻资质 2500、物防 1500、法防 1500；'
    '进化到 20999"九尾天狐·终焉"，消耗 10050×3、10051×2。'
)


def _read(sandbox, stem, sheet):
    from agent.excel.cli.real_cli import RealCodeMakerCLI
    cli = RealCodeMakerCLI(workspace=Path(sandbox))
    for tp in cli.list_tables():
        if tp.stem == stem:
            return cli.read_sheet(tp, sheet)
    return []


def main():
    from services.agent_service import AgentService

    tmp = Path(tempfile.mkdtemp(prefix="ninetail_probe_"))
    sandbox = tmp / "resources"
    shutil.copytree(ROOT / "resources", sandbox)
    _p(f"[setup] sandbox={sandbox}")

    pet_before = len(_read(sandbox, "pet", "Pet"))
    evo_before = len(_read(sandbox, "pet_evolve", "PetEvolveData"))
    _p(f"[pre] pet rows={pet_before}  pet_evolve rows={evo_before}")

    reply_q: _queue.Queue = _queue.Queue()
    cancel = threading.Event()
    service = None
    try:
        service = AgentService(resources_dir=sandbox, enable_skill=True)

        async def _drive():
            t0 = time.perf_counter()
            done = [None]
            step_metrics = {}
            gen = service.chat_stream(text=USER_CMD, session_id="ninetail_probe",
                                      dry_run=False, cancel_event=cancel,
                                      reply_queue=reply_q)
            async for etype, payload in gen:
                if etype == "thinking":
                    ph = (payload or {}).get("phase", "")
                    det = (payload or {}).get("detail", "")
                    if any(k in det for k in ("跨记录", "schema 预算", "候选", "LLM 候选")):
                        _p(f"  [{ph}] {det[:180]}")
                elif etype == "ask":
                    _p(f"  [ASK] {(payload or {}).get('mode_hint')} -> skip")
                    reply_q.put({"mode": "skip"})
                elif etype == "done":
                    done[0] = payload
            wall = time.perf_counter() - t0
            return done[0], wall

        done, wall = asyncio.run(_drive())
        _p(f"\n[run] wall={wall:.1f}s")

        def _g(o, k, d=None):
            return (o.get(k, d) if isinstance(o, dict) else getattr(o, k, d)) if o else d

        _p(f"[done] ok={_g(done,'ok')} msg={str(_g(done,'message',''))[:120]}")
        steps = _g(done, "steps") or []
        for s in steps:
            nm = s.get("name") if isinstance(s, dict) else getattr(s, "name", "")
            mt = s.get("metrics") if isinstance(s, dict) else getattr(s, "metrics", {})
            if mt and ("candidate_count" in mt or "step1_llm_prompt_chars" in mt):
                _p(f"  [metrics {nm}] "
                   + " ".join(f"{k}={mt[k]}" for k in (
                       "candidate_count", "candidate_required_count",
                       "candidate_dependency_count", "candidate_context_count",
                       "step1_llm_prompt_chars", "step1_llm_dur_ms",
                       "step1_llm_timeouts", "llm_calls") if k in mt))

        # 校验 pet_evolve 源灵兽外键
        pet_rows = _read(sandbox, "pet", "Pet")
        evo_rows = _read(sandbox, "pet_evolve", "PetEvolveData")
        _p(f"\n[post] pet rows={len(pet_rows)} (+{len(pet_rows)-pet_before})  "
           f"pet_evolve rows={len(evo_rows)} (+{len(evo_rows)-evo_before})")

        # 找九尾天狐 pet 行（名称列 index 2）+ 其灵兽id（index 0）
        new_pet_id = None
        for r in pet_rows:
            if len(r) > 2 and r[2] and "九尾天狐" in str(r[2]) and "终焉" not in str(r[2]):
                new_pet_id = r[0]
        # 找进化行（进化后名称 index 6 含九尾天狐·终焉）；宠物id = index 2
        evo_row = None
        for r in evo_rows:
            if len(r) > 6 and r[6] and "九尾天狐" in str(r[6]):
                evo_row = r
        _p(f"\n[VERIFY] 新增九尾天狐 灵兽id={new_pet_id}")
        if evo_row:
            src = evo_row[2] if len(evo_row) > 2 else None
            _p(f"[VERIFY] 进化行: 进化id={evo_row[0]} 宠物id(源)={src} "
               f"进化后ID={evo_row[5] if len(evo_row)>5 else None} "
               f"进化后名={evo_row[6] if len(evo_row)>6 else None}")
            ok_link = (src is not None and str(src).strip() != ""
                       and str(src) == str(new_pet_id))
            _p(f"\n{'PASS' if ok_link else 'FAIL'}: 进化链源灵兽id "
               f"{'== 新增九尾天狐 (cross_ref_linker 生效)' if ok_link else '未正确回填'}")
            return 0 if ok_link else 1
        else:
            _p("FAIL: 未找到九尾天狐进化行")
            return 1
    finally:
        try:
            if service is not None and getattr(service, "_file_watcher", None):
                service._file_watcher.stop()
        except Exception:
            pass
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

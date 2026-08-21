import os, sys, time
from pathlib import Path
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, "server")
from services.agent_service import AgentService

print(f"PIPELINE_MODE={os.environ.get('CODEMAKER_PIPELINE_MODE')} "
      f"AI_WHITELIST={os.environ.get('CODEMAKER_AI_WHITELIST_MODE')} "
      f"AI_TIMEOUT_SCALE={os.environ.get('CODEMAKER_AI_TIMEOUT_SCALE')}")
s = AgentService(resources_dir=Path("resources"), enable_skill=True)
print(f"[init] done\n")

ops = [
    ("get",    "查询perf_pet_10k表灵兽id为100005的物攻资质"),
    ("set",    "把perf_pet_10k表中灵兽id为100005的物攻资质改为9999"),
    ("insert", "在perf_pet_10k表中新增一个灵兽，名称压测兽X，物攻资质1800"),
    ("delete", "删除perf_pet_10k表中灵兽id为100005的行"),
]
for label, msg in ops:
    t0 = time.perf_counter()
    try:
        resp = s.chat(text=msg, session_id=f"diag4_{label}", dry_run=True,
                      table_hint="perf_pet_10k")
        if getattr(resp, "needs_confirm", False) and getattr(resp, "confirm_token", None):
            resp = s.chat(text=msg, session_id=f"diag4_{label}", dry_run=True,
                          table_hint="perf_pet_10k",
                          confirm_token=resp.confirm_token, confirm_cascade=True)
        ms = (time.perf_counter() - t0) * 1000
        err = getattr(resp, "error", "") or ""
        dp = getattr(resp, "diff_preview", None)
        dv = ""
        if dp and dp.changes:
            dv = f" diff_new={dp.changes[0].new_value!r}"
        print(f"[{label:7s}] {ms:7.0f}ms  ok={resp.ok}  intent={resp.intent!r}{dv}  "
              f"err={err[:60]!r}")
    except Exception as e:
        ms = (time.perf_counter() - t0) * 1000
        print(f"[{label:7s}] {ms:7.0f}ms  EXCEPTION  {type(e).__name__}: {e}")
print("\n[done]")

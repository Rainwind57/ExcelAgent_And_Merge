import os, sys, time
from pathlib import Path
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, "server")
from services.agent_service import AgentService

print(f"AI_WHITELIST_MODE={os.environ.get('CODEMAKER_AI_WHITELIST_MODE')}")
print(f"AI_TIMEOUT_SCALE={os.environ.get('CODEMAKER_AI_TIMEOUT_SCALE')}")
s = AgentService(resources_dir=Path("resources"), enable_skill=True)


def run(label, msg):
    print(f"\n{'='*60}\n[{label}] {msg}\n{'='*60}")
    t0 = time.perf_counter()
    resp = s.chat(text=msg, session_id=f"diag_{label}", dry_run=True,
                  table_hint="perf_pet_10k")
    if getattr(resp, "needs_confirm", False) and getattr(resp, "confirm_token", None):
        resp = s.chat(text=msg, session_id=f"diag_{label}", dry_run=True,
                      table_hint="perf_pet_10k",
                      confirm_token=resp.confirm_token, confirm_cascade=True)
    ms = (time.perf_counter() - t0) * 1000
    print(f"耗时: {ms:.0f}ms  ok={resp.ok}  intent={resp.intent!r}  reply_type={resp.reply_type!r}")
    print(f"message: {resp.message!r}")
    print(f"error: {getattr(resp, 'error', '')!r}")
    print(f"steps:")
    for st in (resp.steps or []):
        print(f"  - {st.name}: ok={st.ok}  {st.detail[:100] if st.detail else ''}")
    print(f"thinking_steps({len(resp.thinking_steps or [])}):")
    for ts in (resp.thinking_steps or [])[:8]:
        print(f"  - {ts.get('phase')}: {(ts.get('detail','') or '')[:100]}")
    dp = getattr(resp, "diff_preview", None)
    if dp:
        print(f"diff: file={dp.file} sheet={dp.sheet} row={dp.row} changes={dp.changes}")
    cnt = getattr(s.agent, "_llm_counter", None)
    if cnt:
        print(f"llm_counter: {cnt.as_dict()}")


run("get", "查询perf_pet_10k表灵兽id为100005的物攻资质")
run("set", "把perf_pet_10k表中灵兽id为100005的物攻资质改为9999")

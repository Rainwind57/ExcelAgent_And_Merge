import os, sys, traceback
from pathlib import Path
for line in open(".env", encoding="utf-8"):
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())
sys.path.insert(0, "server")

from agent.excel.nl_parser import NLIntent
_orig_init = NLIntent.__init__
def _spy_init(self, *args, **kwargs):
    if "fields" in kwargs:
        print("=" * 60)
        print("!! NLIntent 构造收到 fields= 关键字参数 !!")
        traceback.print_stack()
        print("=" * 60)
    return _orig_init(self, *args, **kwargs)
NLIntent.__init__ = _spy_init

from services.agent_service import AgentService
s = AgentService(resources_dir=Path("resources"), enable_skill=True)
print("\n>>> 跑 set 操作...")
resp = s.chat(text="把perf_pet_10k表中灵兽id为100005的物攻资质改为9999",
              session_id="diag_set2", dry_run=True, table_hint="perf_pet_10k")
print(f"\n结果: ok={resp.ok} intent={resp.intent!r} error={resp.error!r}")

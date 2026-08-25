# -*- coding: utf-8 -*-
import sys, os
from pathlib import Path
_ROOT = Path(__file__).resolve().parents[2]
for _line in (_ROOT / ".env").read_text(encoding="utf-8").splitlines():
    _line = _line.strip()
    if not _line or _line.startswith("#") or "=" not in _line: continue
    _k,_v = _line.split("=",1)
    if _k.strip() and _k.strip() not in os.environ: os.environ[_k.strip()] = _v.strip()
os.environ.setdefault("CODEMAKER_EXCEL_PIPELINE_V2","1")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agent.excel.subagent.locator_agent import LocatorAgent
TEXT = ("新建的长老叫守器长老·青 model_id 1025，放在 space_id 10030 坐标 (280,0,180)，"
        "点击后展开对话树：长老第一句话年轻人，给选项我愿一试。")
ag = LocatorAgent()
cs = ag._column_extractor.extract(TEXT) if ag._column_extractor else None
print("has_signal:", cs.has_signal if cs else None)
from collections import defaultdict
agg = defaultdict(list)
for h in (cs.hits if cs else []):
    agg[h.stem].append(h)
for stem, hs in agg.items():
    cols = [h.column for h in hs]
    fk_flags = [(c, ag._is_fk_reference_column(stem, None, c)) for c in cols]
    print(f"{stem}: cols={cols} fk={fk_flags}")

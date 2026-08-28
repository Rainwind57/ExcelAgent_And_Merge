# -*- coding: utf-8 -*-
"""诊断 school_quest_chain case0 的分段行为：split_multi_intent 切出几段。"""
import sys
import json
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))

cases = json.loads((HERE / "cases" / "school_quest_chain_inputs.json").read_text(encoding="utf-8"))
text = cases[0]["input"]

from server.agent.excel.parser.multi_intent_splitter import split_multi_intent

segs = split_multi_intent(text)
print(f"分段数: {len(segs)}")
for i, s in enumerate(segs):
    t = getattr(s, "text", s) if not isinstance(s, str) else s
    print(f"--- seg[{i}] ({len(t)} chars): {t[:80]}")

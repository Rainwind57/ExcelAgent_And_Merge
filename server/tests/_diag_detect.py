# -*- coding: utf-8 -*-
import sys, json
from pathlib import Path
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1]))
sys.path.insert(0, str(HERE))
cases = json.loads((HERE / "cases" / "school_quest_chain_inputs.json").read_text(encoding="utf-8"))
text = cases[0]["input"]
from server.agent.excel.core.cross_table_splitter import detect_cross_table_action
r = detect_cross_table_action(text)
print(f"detect_cross_table_action = {r!r}")
print("任务链 in text:", '任务链' in text)
print("最后 in text:", '最后' in text)
import re
print("指向上面新建:", bool(re.search(r'指向(?:上面|前面|刚才|该|此)?新建', text)))

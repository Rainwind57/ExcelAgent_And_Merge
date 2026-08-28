# -*- coding: utf-8 -*-
import json, sys
sys.stdout.reconfigure(encoding="utf-8")
d = json.load(open(r"c:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge\server\agent\excel\_table_index.json", encoding="utf-8"))
tabs = d if isinstance(d, list) else d.get("tables", [])
want = {"spawn_world_entity", "spawn_quest_entity", "entity_prefab", "interaction"}
for t in tabs:
    stem = t.get("stem", "")
    if stem in want:
        print(stem, "|", t.get("path"), "|", (t.get("sheets") or [])[:4])

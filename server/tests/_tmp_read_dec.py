# -*- coding: utf-8 -*-
import sys
sys.stdout.reconfigure(encoding="utf-8")
src = open(r"server/agent/excel/subagent/decompose_agent.py", encoding="utf-8").read()
print("occurrences of table:", src.count('"table"'))
i = src.find('"table"')
print("--- snippet 1950:2250 ---")
print(src[1950:2250])

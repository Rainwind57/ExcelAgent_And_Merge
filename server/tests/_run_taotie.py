# -*- coding: utf-8 -*-
import subprocess
import sys

text = "查看灵兽饕餮一阶的所有属性"
r = subprocess.run(
    [sys.executable, "-m", "tests.trace_step1_full",
     "--text", text, "--backend", "deepseek"],
    capture_output=True, text=True, encoding="utf-8",
    cwd=r"c:/Users/wuzhixian/Desktop/Excel-Agent-And-Merge/server")
with open("tests/reports/taotie_out.txt", "w", encoding="utf-8") as f:
    f.write(r.stdout or "")
    f.write(r.stderr or "")
print("EXIT", r.returncode)

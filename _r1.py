import json
d=json.load(open(r"C:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge\_dump_idmgr.json",encoding="utf-8"))
for k,v in d.items():
    print("=====",k,"rows:",len(v))

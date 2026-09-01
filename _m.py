import openpyxl, os
base = r"C:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge\merge\_seed_data\trunk"
wb = openpyxl.load_workbook(os.path.join(base,"model_prefab.xlsx"), data_only=True)
for sh in ["MetaData","Model"]:
    ws = wb[sh]
    print("===",sh)
    for i,row in enumerate(ws.iter_rows(values_only=True)):
        if i<2: continue
        if row and any(c is not None for c in row):
            print(i, row[0], row[2])

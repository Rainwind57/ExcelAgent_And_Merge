import openpyxl, os
base = r"C:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge\merge\_seed_data\trunk"
wb = openpyxl.load_workbook(os.path.join(base,"space.xlsx"), data_only=True)
ws = wb["space_data"]
for i,row in enumerate(ws.iter_rows(values_only=True)):
    if i<3: continue
    if row and any(c is not None for c in row):
        print(i, row[0], row[2])

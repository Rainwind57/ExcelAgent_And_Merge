import openpyxl, os
base = r"C:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge\merge\_seed_data\trunk"
def dump(name, sheet, nrows=3):
    p = os.path.join(base,name)
    wb = openpyxl.load_workbook(p, data_only=True)
    ws = wb[sheet]
    print("\n==== %s :: %s ====" % (name,sheet))
    for i,row in enumerate(ws.iter_rows(values_only=True)):
        if i>=nrows: break
        print(i, row)
dump("interaction.xlsx","Interaction",3)
dump("interaction.xlsx","InteractionConv",4)
dump("interaction.xlsx","InteractionConvOption",4)

import openpyxl, json, os
base = r"C:\Users\wuzhixian\Desktop\Excel-Agent-And-Merge\merge\_seed_data\trunk"
for name in ["interaction.xlsx","entity_prefab.xlsx","spawn_world_entity.xlsx","reward.xlsx"]:
    p = os.path.join(base,name)
    wb = openpyxl.load_workbook(p, data_only=True)
    print("==== "+name+" sheets:", wb.sheetnames)

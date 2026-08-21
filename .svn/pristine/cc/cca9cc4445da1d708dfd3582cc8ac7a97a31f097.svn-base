"""补充 item 行17/20 + reward 冲突，凑够 15+ 冲突。"""
import subprocess
from pathlib import Path
from openpyxl import load_workbook

WC = Path(__file__).resolve().parent.parent / "svn" / "demo_svn" / "wc"
DEV1 = WC / "branches" / "dev1"
DEV2 = WC / "branches" / "dev2"


def _set_commit(path: Path, sheet: str, edits: list, wc: Path, msg: str):
    wb = load_workbook(path)
    ws = wb[sheet]
    n = 0
    for r, c, v in edits:
        if ws.cell(r, c).value != v:
            ws.cell(r, c).value = v
            n += 1
    if n:
        wb.save(path)
        subprocess.run(["svn", "commit", "-m", msg, str(wc)],
                       capture_output=True, text=True, encoding="utf-8")
        print(f"  {msg} ({n} cells)")
    else:
        print(f"  skip {msg} (no change)")
    wb.close()


# absorb: dev1/dev2 item 行17/20 冲突
_set_commit(DEV1 / "item" / "item.xlsx", "ItemBase",
           [(17, 2, "dev1_i17"), (20, 2, "dev1_i20")], DEV1, "dev1 item r17/20")
_set_commit(DEV2 / "item" / "item.xlsx", "ItemBase",
           [(17, 2, "dev2_i17"), (20, 2, "dev2_i20")], DEV2, "dev2 item r17/20")

# reward 双方改行5 name
rw1 = DEV1 / "reward.xlsx"
rw2 = DEV2 / "reward.xlsx"
if rw1.exists() and rw2.exists():
    # 找 reward sheet 名
    wb = load_workbook(rw1)
    sn = wb.sheetnames[0] if wb.sheetnames else None
    wb.close()
    if sn:
        _set_commit(rw1, sn, [(5, 2, "dev1_rw5"), (7, 2, "dev1_rw7")], DEV1, "dev1 reward 冲突")
        _set_commit(rw2, sn, [(5, 2, "dev2_rw5"), (7, 2, "dev2_rw7")], DEV2, "dev2 reward 冲突")

print("done")

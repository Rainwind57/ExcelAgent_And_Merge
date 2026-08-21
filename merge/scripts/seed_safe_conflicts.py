"""补充冲突到 15+，仅用小表（item/reward/ability），避免大表 openpyxl 丢行。

absorb: dev1/dev2 item 行5/8/11/14/17/20 + reward 行5/7 = 8
merge_back: dev1/trunk item 行7/10/13 + reward 行3/5 = 5
目录合并: subdev_1/trunk ability CONFIG 行 + item_drop 行 = 3
"""
import subprocess
from pathlib import Path
from openpyxl import load_workbook

WC = Path(__file__).resolve().parent.parent / "svn" / "demo_svn" / "wc"
DEV1 = WC / "branches" / "dev1"
DEV2 = WC / "branches" / "dev2"
TRUNK = WC / "trunk"
SUBDEV1 = TRUNK / "subdev_1"


def _set_commit(path: Path, sheet: str, edits, wc: Path, msg: str):
    wb = load_workbook(path)
    if sheet not in wb.sheetnames:
        print(f"  skip {msg}: sheet {sheet} not found"); return
    ws = wb[sheet]
    n = 0
    for r, c, v in edits:
        if ws.cell(r, c).value != v:
            ws.cell(r, c).value = v; n += 1
    if n:
        wb.save(path); wb.close()
        subprocess.run(["svn", "commit", "-m", msg, str(wc)],
                       capture_output=True, text=True, encoding="utf-8")
        print(f"  [OK] {msg} ({n} cells)")
    else:
        wb.close(); print(f"  skip {msg} (no change)")


# merge_back: dev1 与 trunk item 行7/10/13 + reward 行3/5 冲突
_set_commit(DEV1 / "item" / "item.xlsx", "ItemBase",
           [(7, 2, "dev1_i7"), (10, 2, "dev1_i10"), (13, 2, "dev1_i13")], DEV1, "dev1 merge_back item")
_set_commit(TRUNK / "item" / "item.xlsx", "ItemBase",
           [(7, 2, "trunk_i7"), (10, 2, "trunk_i10"), (13, 2, "trunk_i13")], TRUNK, "trunk merge_back item")
# reward merge_back
rw1 = DEV1 / "reward.xlsx"; rwt = TRUNK / "reward.xlsx"
if rw1.exists() and rwt.exists():
    wb = load_workbook(rw1); sn = wb.sheetnames[0]; wb.close()
    _set_commit(rw1, sn, [(3, 2, "dev1_rw3"), (5, 2, "dev1_rw5")], DEV1, "dev1 merge_back reward")
    _set_commit(rwt, sn, [(3, 2, "trunk_rw3"), (5, 2, "trunk_rw5")], TRUNK, "trunk merge_back reward")

# 目录合并: subdev_1 vs trunk ability + item_drop
ab_sub = SUBDEV1 / "ability.xlsx"; ab_tr = TRUNK / "ability.xlsx"
if ab_sub.exists() and ab_tr.exists():
    # ability CONFIG sheet 行1 B 列（subdev_1 已改 r1，trunk 保持 → 单向；让 trunk 也改制造冲突）
    _set_commit(ab_sub, "CONFIG", [(1, 2, "sub_ab1b"), (2, 2, "sub_ab2b")], SUBDEV1, "subdev_1 ability CONFIG")
    _set_commit(ab_tr, "CONFIG", [(1, 2, "trunk_ab1b"), (2, 2, "trunk_ab2b")], TRUNK, "trunk ability CONFIG")
# item_drop
id_sub = SUBDEV1 / "item_drop.xlsx"; id_tr = TRUNK / "item_drop.xlsx"
if id_sub.exists() and id_tr.exists():
    wb = load_workbook(id_sub); sn = wb.sheetnames[0]; wb.close()
    _set_commit(id_sub, sn, [(5, 2, "sub_id5"), (8, 2, "sub_id8")], SUBDEV1, "subdev_1 item_drop")
    _set_commit(id_tr, sn, [(5, 2, "trunk_id5"), (8, 2, "trunk_id8")], TRUNK, "trunk item_drop")

print("\n冲突补充完成。")

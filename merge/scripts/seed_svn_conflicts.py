"""向 merge/svn/demo_svn 真实 SVN 仓库提交冲突数据，供三种合并模式验证。

制造冲突点：
  absorb (dev1 → dev2)：item.xlsx 行5/6 name 双方改不同值 → 真冲突
  merge_back (dev1 → trunk)：trunk 前进改 item 行7，与 dev1 改动冲突
  目录合并 (trunk/subdev_1 → trunk)：subdev_1 改 ability.xlsx 制造冲突

结构增删：
  dev1 新增 sheet "NewSheet_Dev1" 到 item.xlsx
  dev2 新增表格 new_table_dev2.xlsx
  subdev_1 新增 sheet 到 ability.xlsx

幂等：重复执行只产生一次提交（检测目标 cell 已是目标值则跳过）。
"""
import subprocess
import sys
from pathlib import Path
from openpyxl import load_workbook

WC_ROOT = Path(__file__).resolve().parent.parent / "svn" / "demo_svn" / "wc"
TRUNK = WC_ROOT / "trunk"
DEV1 = WC_ROOT / "branches" / "dev1"
DEV2 = WC_ROOT / "branches" / "dev2"
SUBDEV1 = TRUNK / "subdev_1"


def _svn_commit(wc_path: Path, msg: str) -> None:
    r = subprocess.run(
        ["svn", "commit", "-m", msg, str(wc_path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if r.returncode != 0:
        print(f"  svn commit 失败: {r.stderr.strip()}", file=sys.stderr)
    else:
        print(f"  committed: {msg}")


def _set_cell(xlsx: Path, sheet: str, row: int, col: int, value) -> bool:
    """若 cell 当前值 != value 则改并返回 True（需提交），否则 False（跳过）。"""
    wb = load_workbook(xlsx)
    if sheet not in wb.sheetnames:
        wb.close()
        return False
    ws = wb[sheet]
    if ws.cell(row, col).value == value:
        wb.close()
        return False
    ws.cell(row, col).value = value
    wb.save(xlsx)
    wb.close()
    return True


def _add_sheet(xlsx: Path, sheet: str, header: list, row: list) -> bool:
    wb = load_workbook(xlsx)
    if sheet in wb.sheetnames:
        wb.close()
        return False
    ws = wb.create_sheet(sheet)
    ws.append(header)
    ws.append(row)
    wb.save(xlsx)
    wb.close()
    return True


def _new_table(dir_path: Path, name: str) -> bool:
    fp = dir_path / f"{name}.xlsx"
    if fp.exists():
        return False
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    ws.append(["id", "name"])
    ws.append([1, "新表测试"])
    wb.save(fp)
    return True


def _svn_add(path: Path) -> None:
    subprocess.run(
        ["svn", "add", str(path), "--force"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def main() -> None:
    # ── dev1: item 行5 name 改 "dev1_改row5"，新增 sheet NewSheet_Dev1 ──
    item_d1 = DEV1 / "item" / "item.xlsx"
    changed = []
    if _set_cell(item_d1, "ItemBase", 5, 2, "dev1_改row5"):
        changed.append("item 行5 name")
    if _add_sheet(item_d1, "NewSheet_Dev1", ["id", "info"], [1, "dev1新增sheet"]):
        changed.append("item 新增 sheet NewSheet_Dev1")
    if changed:
        print(f"dev1 改动: {changed}")
        _svn_commit(DEV1, f"dev1: 制造冲突 {', '.join(changed)}")

    # ── dev2: item 行5 name 改不同值（冲突），新增表格 new_table_dev2 ──
    item_d2 = DEV2 / "item" / "item.xlsx"
    changed = []
    if _set_cell(item_d2, "ItemBase", 5, 2, "dev2_改row5"):
        changed.append("item 行5 name（与 dev1 冲突）")
    if _set_cell(item_d2, "ItemBase", 6, 2, "dev2_改row6"):
        changed.append("item 行6 name（单向）")
    if _new_table(DEV2, "new_table_dev2"):
        _svn_add(DEV2 / "new_table_dev2.xlsx")
        changed.append("新增表格 new_table_dev2.xlsx（结构增删）")
    if changed:
        print(f"dev2 改动: {changed}")
        _svn_commit(DEV2, f"dev2: 制造冲突 {', '.join(changed)}")

    # ── trunk: item 行7 name 改（merge_back 时与 dev1 冲突）──
    item_tr = TRUNK / "item" / "item.xlsx"
    if _set_cell(item_tr, "ItemBase", 7, 2, "trunk_改row7"):
        print("trunk 改动: item 行7 name（merge_back 冲突点）")
        _svn_commit(TRUNK, "trunk: 前进改 item 行7（merge_back 冲突源）")

    # ── subdev_1: ability 改 cell 制造目录合并冲突，新增 sheet ──
    ab_sub = SUBDEV1 / "ability.xlsx"
    changed = []
    if _set_cell(ab_sub, "CONFIG", 1, 2, "subdev1_改"):
        changed.append("ability 行1 B 列")
    if _add_sheet(ab_sub, "SubSheet", ["k", "v"], [1, "subdev新增"]):
        changed.append("ability 新增 sheet SubSheet（结构增删）")
    if changed:
        print(f"subdev_1 改动: {changed}")
        _svn_commit(SUBDEV1, f"subdev_1: 制造目录合并冲突 {', '.join(changed)}")

    print("\n完成。冲突数据已提交到 demo_svn 仓库。")


if __name__ == "__main__":
    main()

"""扩充 demo_svn 冲突数据，三模式各 5-7 个冲突点。

absorb (dev1 vs dev2)：item 行5/8/11/14 + monster 行3/6 → 6冲突
merge_back (dev1 vs trunk)：item 行7/10/13 + monster 行5/9 + skill_level 行4 → 5冲突
目录合并 (subdev_1 vs trunk)：subdev_1 改 monster 行2/5 + item_drop 行3 → 2冲突

幂等：cell 已是目标值则跳过。
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


def _commit(wc: Path, msg: str) -> None:
    r = subprocess.run(["svn", "commit", "-m", msg, str(wc)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    print(f"  [{'OK' if r.returncode==0 else 'FAIL'}] {msg}")


def _set(xlsx: Path, sheet: str, row: int, col: int, value) -> bool:
    wb = load_workbook(xlsx)
    if sheet not in wb.sheetnames:
        wb.close(); return False
    ws = wb[sheet]
    if ws.cell(row, col).value == value:
        wb.close(); return False
    ws.cell(row, col).value = value
    wb.save(xlsx); wb.close()
    return True


def _batch_set(xlsx: Path, sheet: str, edits: list) -> list:
    """edits: [(row, col, value)]，批量改，返回实际改动的 (row,col,value)。"""
    wb = load_workbook(xlsx)
    if sheet not in wb.sheetnames:
        wb.close(); return []
    ws = wb[sheet]
    changed = []
    for row, col, value in edits:
        if ws.cell(row, col).value != value:
            ws.cell(row, col).value = value
            changed.append((row, col, value))
    if changed:
        wb.save(xlsx)
    wb.close()
    return changed


def main() -> None:
    # ── absorb: dev1 vs dev2 在 item + monster 制造冲突 ──
    item_d1 = DEV1 / "item" / "item.xlsx"
    item_d2 = DEV2 / "item" / "item.xlsx"
    mon_d1 = DEV1 / "monster.xlsx"
    mon_d2 = DEV2 / "monster.xlsx"

    # dev1: item 行5/8/11/14 name 改 dev1_前缀
    c1 = _batch_set(item_d1, "ItemBase", [
        (5, 2, "dev1_item_r5"), (8, 2, "dev1_item_r8"),
        (11, 2, "dev1_item_r11"), (14, 2, "dev1_item_r14")])
    # dev2: 同行改不同值 → 冲突
    c2 = _batch_set(item_d2, "ItemBase", [
        (5, 2, "dev2_item_r5"), (8, 2, "dev2_item_r8"),
        (11, 2, "dev2_item_r11"), (14, 2, "dev2_item_r14")])
    # dev1/dev2 monster 行5/8 name 冲突（数据从行4开始：100001=行4, 100002=行5...）
    c3 = _batch_set(mon_d1, "Monster", [
        (5, 3, "dev1_mon_5"), (8, 3, "dev1_mon_8")]) if mon_d1.exists() else []
    c4 = _batch_set(mon_d2, "Monster", [
        (5, 3, "dev2_mon_5"), (8, 3, "dev2_mon_8")]) if mon_d2.exists() else []
    if c1 or c3:
        _commit(DEV1, f"dev1: absorb冲突扩充 item{len(c1)} monster{len(c3)}")
    if c2 or c4:
        _commit(DEV2, f"dev2: absorb冲突扩充 item{len(c2)} monster{len(c4)}")

    # ── merge_back: dev1 vs trunk 在 item + monster + skill_level 制造冲突 ──
    item_tr = TRUNK / "item" / "item.xlsx"
    mon_tr = TRUNK / "monster.xlsx"

    # dev1: item 行7/10/13 + monster 行5/9 改 dev1_前缀（行7 已有，检测跳过）
    c5 = _batch_set(item_d1, "ItemBase", [
        (7, 2, "dev1_item_r7"), (10, 2, "dev1_item_r10"), (13, 2, "dev1_item_r13")])
    c6 = _batch_set(mon_d1, "Monster", [
        (6, 3, "dev1_mon_6"), (9, 3, "dev1_mon_9")]) if mon_d1.exists() else []
    # trunk: 同行改不同值 → 冲突
    c7 = _batch_set(item_tr, "ItemBase", [
        (7, 2, "trunk_item_r7"), (10, 2, "trunk_item_r10"), (13, 2, "trunk_item_r13")])
    c8 = _batch_set(mon_tr, "Monster", [
        (6, 3, "trunk_mon_6"), (9, 3, "trunk_mon_9")]) if mon_tr.exists() else []
    if c5 or c6:
        _commit(DEV1, f"dev1: merge_back冲突扩充 item{len(c5)} monster{len(c6)}")
    if c7 or c8:
        _commit(TRUNK, f"trunk: merge_back冲突扩充 item{len(c7)} monster{len(c8)}")

    # ── 目录合并: subdev_1 改 monster + ability 多行 ──
    mon_sub = SUBDEV1 / "monster.xlsx"
    ab_sub = SUBDEV1 / "ability.xlsx"
    c10 = _batch_set(mon_sub, "Monster", [
        (5, 3, "sub_mon_5"), (8, 3, "sub_mon_8"), (11, 3, "sub_mon_11")]) if mon_sub.exists() else []
    # ability CONFIG 是配置表（行少），改 DATA sheet 的多行——ability 实际数据在 Ability sheet
    c11 = _batch_set(ab_sub, "Ability", [
        (2, 2, "sub_ab_2"), (3, 2, "sub_ab_3")]) if ab_sub.exists() else []
    if c10 or c11:
        _commit(SUBDEV1, f"subdev_1: 目录合并冲突扩充 monster{len(c10)} ability{len(c11)}")

    print("\n冲突扩充完成。")


if __name__ == "__main__":
    main()

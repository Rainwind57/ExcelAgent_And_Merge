"""compare_sheet 端到端：多分支同 id 新增行应各自独立成行 + 后到者主键自增。

场景：
  base: id 1,2
  item_1: 新增 id 10500 (name A)
  item_2: 新增 id 10500 (name B)
预期：2 条 inserted 行 —— 10500(A) + 10501(B, 重映射, id_remapped=True, original_pk=10500)

n+n 场景：
  item_1 新增 10500,10501；item_2 新增 10500,10501
预期：4 条 inserted 行，2 条重映射（10502,10503）

用法： cd server && python tests/test_compare_idconflict.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.compare import compare_sheet

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


def _sheet(headers, rows):
    return [headers] + rows


# ── 场景1：两分支同 id 单行 ──
base = _sheet(["id", "name"], [[1, "x"], [2, "y"]])
v1 = _sheet(["id", "name"], [[1, "x"], [2, "y"], [10500, "A"]])
v2 = _sheet(["id", "name"], [[1, "x"], [2, "y"], [10500, "B"]])
file_sheets = {"item.xlsx": {"Sheet1": base}, "item_1.xlsx": {"Sheet1": v1}, "item_2.xlsx": {"Sheet1": v2}}
r = compare_sheet(file_sheets, "item.xlsx", "Sheet1")

inserted = [row for row in r["rows"] if row["row_type"] == "inserted"]
check("S1 inserted=2条", len(inserted) == 2, f"got {len(inserted)}")
keys = sorted(row["key"] for row in inserted)
check("S1 主键10500+10501", keys == ["10500", "10501"], str(keys))
remapped = [row for row in inserted if row.get("id_remapped")]
check("S1 重映射1条", len(remapped) == 1, str(len(remapped)))
check("S1 original_pk=10500",
      remapped and remapped[0].get("original_pk") == "10500",
      str(remapped[0].get("original_pk") if remapped else None))
check("S1 重映射行name=B",
      remapped and remapped[0]["cells"][1].get("value") == "B",
      str(remapped[0]["cells"][1].get("value") if remapped else None))
# 不应出现 name 列冲突（旧 bug：合并成一条 + name 冲突）
name_conflicts = [row for row in inserted
                  for c in row["cells"] if c.get("col") == 1 and c.get("conflict")]
check("S1 无name冲突", len(name_conflicts) == 0, str(len(name_conflicts)))
check("S1 stats.inserted=2", r["stats"]["inserted"] == 2, str(r["stats"]))

# ── 场景2：n+n（各新增2行，id 全冲突）──
v1b = _sheet(["id", "name"], [[1, "x"], [2, "y"], [10500, "A0"], [10501, "A1"]])
v2b = _sheet(["id", "name"], [[1, "x"], [2, "y"], [10500, "B0"], [10501, "B1"]])
file_sheets2 = {"item.xlsx": {"Sheet1": base}, "item_1.xlsx": {"Sheet1": v1b}, "item_2.xlsx": {"Sheet1": v2b}}
r2 = compare_sheet(file_sheets2, "item.xlsx", "Sheet1")
ins2 = [row for row in r2["rows"] if row["row_type"] == "inserted"]
check("S2 inserted=4条(2n)", len(ins2) == 4, f"got {len(ins2)}")
rem2 = [row for row in ins2 if row.get("id_remapped")]
check("S2 重映射2条(n)", len(rem2) == 2, str(len(rem2)))
check("S2 重映射原编号都是10500/10501",
      sorted(row.get("original_pk") for row in rem2) == ["10500", "10501"],
      str([row.get("original_pk") for row in rem2]))
check("S2 无重复主键",
      len(set(row["key"] for row in ins2)) == 4,
      str([row["key"] for row in ins2]))

# ── 场景3：matched 行不受影响（base 有 + 衍生改）──
v1c = _sheet(["id", "name"], [[1, "x_changed"], [2, "y"], [10500, "A"]])
v2c = _sheet(["id", "name"], [[1, "x_changed2"], [2, "y"], [10500, "B"]])
file_sheets3 = {"item.xlsx": {"Sheet1": base}, "item_1.xlsx": {"Sheet1": v1c}, "item_2.xlsx": {"Sheet1": v2c}}
r3 = compare_sheet(file_sheets3, "item.xlsx", "Sheet1")
matched1 = next(row for row in r3["rows"] if row["key"] == "1")
check("S3 matched行id=1", matched1["row_type"] == "matched", matched1["row_type"])
# id=1 的 name 两分支改了不同值 → 真冲突（这条是 matched 内容冲突，不是 inserted id 冲突）
name_cell = matched1["cells"][1]
check("S3 matched name冲突", name_cell.get("conflict") is True, str(name_cell.get("conflict")))

if __name__ == "__main__":
    passed = sum(1 for _, c, _ in results if c)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)

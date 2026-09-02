"""ID 冲突重映射 + 引用完整性校验 单元测试。

验证：
  1. 多分支同主键 inserted 行拆分 + 先到先得重映射
  2. ID 重映射后外键引用同步更新（带分支标记，避免跨分支误读）
  3. 悬空引用检测
  4. 跨表主键集合校验

用法： cd server && python tests/test_id_ref.py
"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.id_resolver import resolve_id_conflicts
from engine.ref_integrity import validate_sheet_references, collect_cross_sheet_pks

# 技能id=主键(base_priority), 被动id=外键(base_priority, 见 merge_strategies.yaml ability/Ability)
HEADERS = ["技能id", "名称", "被动id"]
BASE = "base.xlsx"
FILES = ["base.xlsx", "A.xlsx", "B.xlsx"]


def _cell(col, value, versions):
    return {"col": col, "col_letter": "A", "value": value,
            "versions": versions, "conflict": False, "diff_type": ""}


def _row(key, row_type, cells):
    return {"key": key, "row_type": row_type, "cells": cells}


results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


# ── Test 1: ID 冲突拆分 + 重映射 ──
rows = [
    _row("99", "inserted", [
        _cell(0, 99, {"A.xlsx": 99, "B.xlsx": 99}),
        _cell(1, "a", {"A.xlsx": "a", "B.xlsx": "b"}),  # 内容不同 → 触发拆分
        _cell(2, 5, {"A.xlsx": 5, "B.xlsx": 5}),
    ]),
]
r = resolve_id_conflicts(rows, HEADERS, BASE, FILES)
check("T1 拆分2行", len(r["resolved_rows"]) == 2, f"got {len(r['resolved_rows'])}")
check("T1 主键99+100", set(x["key"] for x in r["resolved_rows"]) == {"99", "100"},
      str([x["key"] for x in r["resolved_rows"]]))
check("T1 B重映射100",
      r["id_mapping"] == [{"file": "B.xlsx", "old_pk": "99", "new_pk": "100", "reason": "多分支新增同 ID 冲突"}],
      str(r["id_mapping"]))
check("T1 rows_split=1", r["stats"]["rows_split"] == 1, str(r["stats"]))

# ── Test 2: 引用重映射同步（B 分支外键 99→100，带分支标记）──
rows2 = [
    _row("1", "matched", [
        _cell(0, 1, {"base.xlsx": 1}),
        _cell(1, "n", {"base.xlsx": "n"}),
        _cell(2, 99, {"B.xlsx": 99}),  # B 分支引用自己的旧 99
    ]),
    _row("100", "inserted", [
        _cell(0, 100, {"B.xlsx": 100}),
    ]),
]
id_map = [{"file": "B.xlsx", "old_pk": "99", "new_pk": "100"}]
r2 = validate_sheet_references(rows2, HEADERS, "ability", "Ability", id_map, {})
check("T2 外键同步100",
      str(rows2[0]["cells"][2]["value"]) == "100",
      str(rows2[0]["cells"][2].get("value")))
check("T2 remapped_refs=1", r2["remapped_refs"] == 1, str(r2))
check("T2 不悬空", len(r2["dangling"]) == 0, str(r2["dangling"]))

# ── Test 2b: A 分支引用 99 不应被误改（分支标记隔离）──
rows2b = [
    _row("1", "matched", [
        _cell(0, 1, {"base.xlsx": 1}),
        _cell(1, "n", {"base.xlsx": "n"}),
        _cell(2, 99, {"A.xlsx": 99}),  # A 分支引用 A 的 99（A 保留 99 未重映射）
    ]),
    _row("99", "inserted", [_cell(0, 99, {"A.xlsx": 99})]),  # A 的 99 仍在
]
r2b = validate_sheet_references(rows2b, HEADERS, "ability", "Ability", id_map, {})
check("T2b A引用99不动", str(rows2b[0]["cells"][2]["value"]) == "99",
      str(rows2b[0]["cells"][2].get("value")))
check("T2b A引用99不悬空", len(r2b["dangling"]) == 0, str(r2b["dangling"]))

# ── Test 3: 悬空引用检测 ──
rows3 = [
    _row("2", "matched", [
        _cell(0, 2, {"base.xlsx": 2}),
        _cell(1, "n", {"base.xlsx": "n"}),
        _cell(2, 999, {"base.xlsx": 999}),  # 悬空
    ]),
]
r3 = validate_sheet_references(rows3, HEADERS, "ability", "Ability", [], {})
check("T3 悬空1条", len(r3["dangling"]) == 1, str(r3["dangling"]))
check("T3 悬空值999",
      r3["dangling"] and str(r3["dangling"][0]["value"]) == "999",
      str(r3["dangling"]))

# ── Test 4: 跨表主键集合校验 ──
rows4 = [
    _row("3", "matched", [
        _cell(0, 3, {"base.xlsx": 3}),
        _cell(1, "n", {"base.xlsx": "n"}),
        _cell(2, 50, {"base.xlsx": 50}),  # 本表无 50，跨表有
    ]),
]
cross = {"被动id": {"50", "51"}}
r4 = validate_sheet_references(rows4, HEADERS, "ability", "Ability", [], cross)
check("T4 跨表命中不悬空", len(r4["dangling"]) == 0, str(r4["dangling"]))

# ── Test 5: collect_cross_sheet_pks ──
sheets = [{"name": "S", "headers": HEADERS,
           "rows": [_row("7", "matched", [_cell(0, 7, {"base.xlsx": 7})])]}]
cpks = collect_cross_sheet_pks(sheets)
check("T5 跨表主键集合", cpks.get("技能id") == {"7"}, str(cpks))

# ── Test 6: inserted 撞 matched 不由 merge 处理（归表格处理 _do_append 校验）──
rows6 = [
    _row("5", "matched", [_cell(0, 5, {"base.xlsx": 5})]),
    _row("5", "inserted", [_cell(0, 5, {"A.xlsx": 5})]),
]
r6 = resolve_id_conflicts(rows6, HEADERS, BASE, FILES)
check("T6 撞matched不重映射", r6["conflicts"] == [], str(r6["conflicts"]))
check("T6 主键不变", set(x["key"] for x in r6["resolved_rows"]) == {"5"},
      str([x["key"] for x in r6["resolved_rows"]]))

# ── Test 7: 多分支同主键冲突 reason 标记 ──
rows7 = [
    _row("99", "inserted", [
        _cell(0, 99, {"A.xlsx": 99, "B.xlsx": 99}),
        _cell(1, "a", {"A.xlsx": "a", "B.xlsx": "b"}),
    ]),
]
r7 = resolve_id_conflicts(rows7, HEADERS, BASE, FILES)
check("T7 多分支冲突reason",
      any(c.get("reason") == "多分支新增同 ID 冲突" for c in r7["conflicts"]),
      str(r7["conflicts"]))

# ── Test 8: conflict 模式 — 视为同一行冲突，不拆分不重映射 ──
# 场景：两分支各新增 id=15071，name=test1/test2，用户选"视为同一行冲突"
rows8 = [
    _row("15071", "inserted", [
        _cell(0, 15071, {"A.xlsx": 15071, "B.xlsx": 15071}),
        _cell(1, "test1", {"A.xlsx": "test1", "B.xlsx": "test2"}),
    ]),
]
r8 = resolve_id_conflicts(rows8, HEADERS, BASE, FILES, mode="conflict")
check("T8 不拆分保留1行", len(r8["resolved_rows"]) == 1, f"got {len(r8['resolved_rows'])}")
check("T8 不重映射", r8["id_mapping"] == [], str(r8["id_mapping"]))
check("T8 pk_conflicts1条", len(r8["pk_conflicts"]) == 1, str(r8["pk_conflicts"]))
check("T8 冲突pk=15071",
      r8["pk_conflicts"] and str(r8["pk_conflicts"][0]["pk"]) == "15071",
      str(r8["pk_conflicts"]))
check("T8 行标记_pk_conflict",
      r8["resolved_rows"] and r8["resolved_rows"][0].get("_pk_conflict") is True,
      str(r8["resolved_rows"][0].get("_pk_conflict")))

# ── Test 9: split 模式（同数据）— 拆分 + B 重映射 ──
r9 = resolve_id_conflicts(rows8, HEADERS, BASE, FILES, mode="split")
check("T9 拆分2行", len(r9["resolved_rows"]) == 2, f"got {len(r9['resolved_rows'])}")
check("T9 主键15071+15072",
      set(x["key"] for x in r9["resolved_rows"]) == {"15071", "15072"},
      str([x["key"] for x in r9["resolved_rows"]]))
check("T9 B重映射15072",
      any(m["new_pk"] == "15072" and m["file"] == "B.xlsx" for m in r9["id_mapping"]),
      str(r9["id_mapping"]))

# ── Test 10: 重映射行带 original_pk / id_remapped 标记（供导出写批注）──
remapped = [r for r in r9["resolved_rows"] if r.get("id_remapped")]
check("T10 重映射行1条", len(remapped) == 1, str(len(remapped)))
check("T10 original_pk=15071",
      remapped and remapped[0].get("original_pk") == "15071",
      str(remapped[0].get("original_pk") if remapped else None))
check("T10 新主键15072",
      remapped and remapped[0].get("key") == "15072",
      str(remapped[0].get("key") if remapped else None))
non_remapped = [r for r in r9["resolved_rows"] if not r.get("id_remapped")]
check("T10 未重映射行无标记",
      all(not r.get("original_pk") for r in non_remapped),
      str([r.get("original_pk") for r in non_remapped]))

# ── Test 11: 跨 sheet 外键同步（同一张表文件内，聚合两个 sheet 的 id_mapping）──
# 场景：item.xlsx 内 ItemBase(sheet A) 主键被重映射 99999→100000，
# Equipment(sheet B) 有一行"分解获得道具id"（base_priority 外键）引用了旧 99999。
# _build_group 修复前只把"自己 sheet 的 id_mapping"传给 validate_sheet_references，
# Equipment 的重映射记录来自 ItemBase（另一个 sheet），会被漏掉——必须聚合两个
# sheet 的 id_mapping 后一起传，才能命中同步。
# table_stem 用叶子名"item"（对应 merge_strategies.yaml 的 tables.item 键），
# 不能直接传嵌套 group_name"item/item"（另一处已修复的 bug，见 _build_group）。
ITEM_HEADERS = ["物品编号", "分解获得道具id"]
id_mapping_from_itembase_sheet = [
    {"file": "item_dev2.xlsx", "old_pk": "99999", "new_pk": "100000"},
]
equipment_rows = [
    _row("1", "matched", [
        _cell(0, 1, {"base.xlsx": 1}),
        _cell(1, 99999, {"item_dev2.xlsx": 99999}),  # 引用同文件另一 sheet 即将被重映射的旧 PK
    ]),
]
# 聚合后传入（修复后 _build_group 的行为）：应命中同步
r11 = validate_sheet_references(
    list(equipment_rows), ITEM_HEADERS, "item", "Equipment",
    id_mapping_from_itembase_sheet, {},
)
check("T11 跨sheet聚合后同步", str(r11 is not None), "")
check("T11 跨sheet外键同步为100000",
      str(equipment_rows[0]["cells"][1]["value"]) == "100000",
      str(equipment_rows[0]["cells"][1].get("value")))
check("T11 remapped_refs=1", r11["remapped_refs"] == 1, str(r11))

# 对照：修复前的行为（只传 Equipment 自己的 id_mapping=[]）应该同步不到
equipment_rows_unfixed = [
    _row("1", "matched", [
        _cell(0, 1, {"base.xlsx": 1}),
        _cell(1, 99999, {"item_dev2.xlsx": 99999}),
    ]),
]
r11b = validate_sheet_references(
    equipment_rows_unfixed, ITEM_HEADERS, "item", "Equipment", [], {},
)
check("T11b 未聚合时不同步（复现修复前的漏检）",
      str(equipment_rows_unfixed[0]["cells"][1]["value"]) == "99999",
      str(equipment_rows_unfixed[0]["cells"][1].get("value")))
check("T11b 未聚合时悬空（99999 不在本表主键集合）",
      len(r11b["dangling"]) == 1, str(r11b["dangling"]))


if __name__ == "__main__":
    passed = sum(1 for _, c, _ in results if c)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)

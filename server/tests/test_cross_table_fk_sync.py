"""跨表（不同 xlsx 文件之间）外键同步 单元测试。

验证 engine/cross_table_fk.py::sync_cross_table_refs：
  1. 命中规则时同步外键值（cell.versions[fn] + 单值时同步 cell.value）
  2. 更新对应 sheet 的 ref_integrity（remapped_refs 累加）
  3. 源表/目标表未同时出现在本次 groups 里时，规则整体跳过（不误报不同步）
  4. 悬空引用检测（目标表主键集合里没有对应值）

用法： cd server && python tests/test_cross_table_fk_sync.py
"""
from __future__ import annotations
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.models import FileGroup, SheetDiff, RowData, CellData
from engine.cross_table_fk import sync_cross_table_refs, CrossTableFKRule

results = []


def check(name, cond, detail=""):
    results.append((name, cond, detail))
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail and not cond else ""))


RULE = CrossTableFKRule(
    src_table="item/item", src_sheet="Chest", src_col="reward ID",
    dst_table="reward", dst_sheet="Reward", dst_pk_col="reward_id",
)


def _cell(col, value, versions):
    return CellData(col=col, col_letter="A", value=value, versions=versions)


def _row(key, row_type, cells):
    return RowData(key=key, row_type=row_type, cells=cells)


def _make_groups(remap_reward=True, include_reward=True):
    # reward.xlsx：reward_id=88888 在 src/tgt 分支各自插入一条内容不同的新奖励 → 冲突，
    # tgt 那条被重映射为 88889（模拟 id_resolver 产出的 id_mapping）。
    # 文件名用真实的 "{flat_group_name}_{src|tgt}.xlsx" 命名（见
    # merge_branch.py::_compare_one_table），故意让 item 表和 reward 表的派生文件名
    # 前缀不同（"item__item_tgt.xlsx" vs "reward_tgt.xlsx"）——验证跨表匹配走的是
    # _file_role 提取的"分支角色"而非原始文件名（两者前缀不同，直接比较永远不等）。
    reward_rows = [
        _row("88888", "matched", [_cell(0, 88888, {"base.xlsx": 88888})]),
        _row("88889" if remap_reward else "88888", "inserted",
             [_cell(0, 88889 if remap_reward else 88888,
                    {"reward_tgt.xlsx": 88889 if remap_reward else 88888})]),
    ]
    reward_sheet = SheetDiff(
        name="Reward", headers=["reward_id", "名称"], rows=reward_rows,
        id_resolution={
            "id_mapping": ([{"file": "reward_tgt.xlsx", "old_pk": "88888", "new_pk": "88889"}]
                           if remap_reward else []),
        },
    )
    reward_group = FileGroup(group_name="reward", sheets={"Reward": reward_sheet})

    # item.xlsx·Chest：tgt 分支一行 "reward ID" 引用 88888（reward.xlsx 里即将/已经
    # 被重映射的那一条）。derived 文件名前缀是 "item__item"（不同于 reward 表的
    # "reward"），同一分支在两张表里的派生名并不相等。
    chest_rows = [
        _row("1", "matched", [
            _cell(0, 1, {"base.xlsx": 1}),
            _cell(1, 88888, {"item__item_tgt.xlsx": 88888}),
        ]),
    ]
    chest_sheet = SheetDiff(name="Chest", headers=["物品编号", "reward ID"], rows=chest_rows)
    item_group = FileGroup(group_name="item/item", sheets={"Chest": chest_sheet})

    groups = {"item/item": item_group}
    if include_reward:
        groups["reward"] = reward_group
    return groups


# ── Test 1: 命中同步 ──
groups1 = _make_groups(remap_reward=True)
report1 = sync_cross_table_refs(groups1, rules=[RULE])
chest_cell = groups1["item/item"].sheets["Chest"].rows[0].cells[1]
check("T1 跨表外键值同步为88889", chest_cell.value == "88889" or chest_cell.value == 88889,
      str(chest_cell.value))
check("T1 versions同步", chest_cell.versions.get("item__item_tgt.xlsx") == "88889",
      str(chest_cell.versions))
check("T1 report remapped_refs=1", report1["item/item::Chest"]["remapped_refs"] == 1, str(report1))
ref_res = groups1["item/item"].sheets["Chest"].ref_integrity
check("T1 ref_integrity同步", ref_res is not None and ref_res.get("remapped_refs") == 1, str(ref_res))
check("T1 不悬空", len(report1["item/item::Chest"]["dangling"]) == 0,
      str(report1["item/item::Chest"]["dangling"]))

# ── Test 2: 目标表未被本次 compare 到 → 整条规则跳过，不误改不误报 ──
groups2 = _make_groups(remap_reward=True, include_reward=False)
report2 = sync_cross_table_refs(groups2, rules=[RULE])
chest_cell2 = groups2["item/item"].sheets["Chest"].rows[0].cells[1]
check("T2 目标表缺失时不改动原值", chest_cell2.value == 88888, str(chest_cell2.value))
check("T2 目标表缺失时不产出report", "item/item::Chest" not in report2, str(report2))

# ── Test 3: 悬空引用检测（reward.xlsx 里没有 88888 也没有重映射记录）──
groups3 = _make_groups(remap_reward=False)
# 制造悬空：把 reward.xlsx 里僅有的 88888 那条也删掉
groups3["reward"].sheets["Reward"].rows = [
    _row("88889", "matched", [_cell(0, 88889, {"base.xlsx": 88889})]),
]
report3 = sync_cross_table_refs(groups3, rules=[RULE])
check("T3 悬空1条", len(report3["item/item::Chest"]["dangling"]) == 1,
      str(report3["item/item::Chest"]["dangling"]))
check("T3 悬空值88888",
      report3["item/item::Chest"]["dangling"] and
      report3["item/item::Chest"]["dangling"][0]["value"] == "88888",
      str(report3["item/item::Chest"]["dangling"]))

# ── Test 4: 列名对不上（headers 里找不到 src_col）→ 该规则静默跳过 ──
groups4 = _make_groups(remap_reward=True)
groups4["item/item"].sheets["Chest"].headers = ["物品编号", "不存在的列"]
report4 = sync_cross_table_refs(groups4, rules=[RULE])
check("T4 列名不匹配时跳过", report4 == {}, str(report4))

if __name__ == "__main__":
    passed = sum(1 for _, c, _ in results if c)
    print(f"\n{passed}/{len(results)} passed")
    sys.exit(0 if passed == len(results) else 1)

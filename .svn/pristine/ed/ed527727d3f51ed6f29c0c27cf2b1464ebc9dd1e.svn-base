"""验证目录合并场景 big_data（10w 行）三方比对：冲突/单向变更/新增行统计。"""
import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent.parent / "server"
sys.path.insert(0, str(SERVER))

from routers.merge_subdir import subdir_compare, SubdirCompareRequest  # noqa: E402


def stats(resp, group: str):
    g = resp.groups[group]
    total_conflicts = 0
    changed = inserted = deleted = 0
    for sname, sheet in g.sheets.items():
        total_conflicts += sheet.stats["conflicts"]
        changed += sheet.stats["changed"]
        inserted += sheet.stats["inserted"]
        deleted += sheet.stats["deleted"]
    return total_conflicts, changed, inserted, deleted


def main():
    ok = True
    for src in ("trunk_r4/subdir_copied", "trunk_r4/subdir_new"):
        resp = subdir_compare(SubdirCompareRequest(source_dir=src, target_dir="trunk_r4",
                                                   group_names=["big_data"]))
        c, ch, ins, dele = stats(resp, "big_data")
        sheet = resp.groups["big_data"].sheets["BigData"]
        keys = {r.key for r in sheet.rows if any(cl.conflict for cl in r.cells)}
        print(f"{src}: conflicts={c} changed={ch} inserted={ins} deleted={dele}")
        print(f"  冲突行 keys={sorted(keys)}")
        expected_conflict = {"10001", "25000", "50000", "99999"}
        ok &= (c == 4 and keys == expected_conflict and ins >= 1 and ch >= 1)
    print("结论:", "目录合并 big_data 三方比对正确（4 冲突 + 变更 + 新增）✓" if ok else "校验失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

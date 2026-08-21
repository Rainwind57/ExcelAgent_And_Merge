"""fast_apply 快路径验证：真实 big_data 文件 → XML 直改 → openpyxl 校验内容。"""
import json
import shutil
import sys
import time
import urllib.request
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent.parent / "server"
sys.path.insert(0, str(SERVER))

BASE = "http://127.0.0.1:8000"
DEMO = Path(__file__).resolve().parent.parent / "demo"


def post(path, body):
    data = json.dumps(body).encode()
    req = urllib.request.Request(BASE + path, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read())


def resolve_all_conflicts(tables):
    """冲突统一采纳 src 版本（与文件 B_r2 不同 → 验证快路径确实写入）。"""
    for tb in tables:
        for s in tb["sheets"]:
            for r in s["rows"]:
                for c in r["cells"]:
                    if c.get("conflict"):
                        c["value"] = c.get("versions", {}).get("big_data_src.xlsx") or c.get("value")
                        c["conflict"] = False
                        c["changed"] = False
                        c["resolved"] = True
                        c["resolvedBy"] = "big_data_src.xlsx"
                        c["diff_type"] = ""
                        c["formula_resolved"] = False


def main():
    resp = post("/api/merge/branch/compare", {"direction": "absorb", "source_branch": "A_r2", "target_branch": "B_r2"})
    g = resp["groups"]["big_data"]
    tables = [{"group_name": "big_data", "sheets": [
        {"name": s["name"], "headers": s["headers"], "rows": s["rows"]} for s in g["sheets"].values()]}]
    resolve_all_conflicts(tables)

    from routers.merge_branch import BranchApplyRequest
    from engine.models import MergeRequest as _MR
    from engine.fast_apply import fast_apply_xml, collect_disk_sheet_pks, _extract_edits

    req = BranchApplyRequest(direction="absorb", source_branch="A_r2", target_branch="B_r2",
                             tables=tables, apply_mode="new_version")
    mr = _MR(group_name="big_data", sheets=req.tables[0].sheets)

    edits = _extract_edits(mr)
    print("updates pk:", sorted(edits["BigData"]["updates"].keys()))
    print("inserts pk:", [i["pk"] for i in edits["BigData"]["inserts"]])

    ours = DEMO / "B_r2" / "big_data.xlsx"
    dest = Path(__file__).resolve().parent / "_tmp_fast_apply.xlsx"
    s = time.perf_counter()
    info = fast_apply_xml(ours, dest, mr)
    print(f"fast_apply_xml: {time.perf_counter()-s:.2f}s info={info}")
    assert info is not None, "快路径未命中"
    assert dest.exists() and dest.stat().st_size > 1_000_000

    # 校验内容
    from python_calamine import CalamineWorkbook
    wb = CalamineWorkbook.from_path(str(dest))
    ws = wb.get_sheet_by_name("BigData")
    rows = ws.to_python()
    print("sheet rows:", len(rows))
    by_pk = {}
    for r in rows[1:]:
        by_pk[str(int(r[0]))] = r
    # 冲突行已按 src 版本解决（行 501 name 等，分支场景冲突行）
    assert by_pk["501"][1] != "data_501", "501 name 未被写入"
    assert by_pk["23456"][1] != "data_23456", "23456 列未被写入"
    # 新增行存在
    assert "100001" in by_pk or "100002" in by_pk, "inserted 行缺失"
    print("501 name:", by_pk["501"][1], "（已写为 src 版本值）")
    print("inserted pk 存在:", [p for p in ("100001", "100002") if p in by_pk])
    # 尾行仍在（未破坏）
    print("last pk:", by_pk[max(by_pk, key=int)][0])
    dest.unlink(missing_ok=True)

    pks = collect_disk_sheet_pks(ours)
    print("disk pks BigData:", len(pks.get("BigData", set())))
    print("结论: fast_apply 快路径正确 ✓")


if __name__ == "__main__":
    main()

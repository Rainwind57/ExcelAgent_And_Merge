"""端到端验证：用 build 产的真实 reward 样本跑 stage2 三方合并。

fork=mergebase/devbranch1_reward.xlsx。手造 trunk_mod(ours 改 ID1) + inter_mod(theirs 改 ID2)，
三方比对 + 导出，验证 ID1 keep ours、ID2 take theirs。
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

from openpyxl import load_workbook

SERVER = Path(__file__).resolve().parent.parent.parent / "server"
sys.path.insert(0, str(SERVER))

from engine.parser import read_excel  # noqa: E402
from engine.compare import compare_sheet  # noqa: E402
from routers.diff import _apply_edits_to_workbook  # noqa: E402

MERGE = Path(__file__).resolve().parent.parent
TMP = MERGE / "_verify_tmp"
FORK = MERGE / "mergebase" / "devbranch1_reward.xlsx"
SHEET = "Reward"


def _wrap_rows(rows):
    out = []
    for r in rows:
        cells = [
            SimpleNamespace(
                col=c["col"], value=c["value"], versions=c["versions"],
                diff_type=c.get("diff_type", ""), conflict=c.get("conflict"), changed=c.get("changed"),
            ) for c in r["cells"]
        ]
        out.append(SimpleNamespace(key=r["key"], row_type=r["row_type"], cells=cells))
    return out


def _first_int_pk_rows(ws, n=2):
    """前 n 个第一列可解析为整数的数据行 [(row, pk_int), ...]。"""
    out = []
    for r in range(2, ws.max_row + 1):
        v = ws.cell(r, 1).value
        try:
            out.append((r, int(str(v).strip())))
            if len(out) >= n:
                return out
        except (ValueError, TypeError):
            continue
    return out


def main():
    TMP.mkdir(exist_ok=True)
    wb = load_workbook(FORK, data_only=True)
    ws = wb[SHEET]
    pk_rows = _first_int_pk_rows(ws, 2)
    wb.close()
    if len(pk_rows) < 2:
        print(f"reward 无足够整型主键行（仅 {len(pk_rows)}），跳过")
        return 1
    r1, id1 = pk_rows[0]
    r2, id2 = pk_rows[1]
    wb = load_workbook(FORK, data_only=True)
    orig1 = wb[SHEET].cell(r1, 2).value
    orig2 = wb[SHEET].cell(r2, 2).value
    wb.close()

    # trunk_mod: fork + 改 ID1 col2（ours 改，trunk 他人改动）
    trunk_mod = TMP / "trunk_mod.xlsx"
    shutil.copy2(FORK, trunk_mod)
    wb = load_workbook(trunk_mod)
    wb[SHEET].cell(r1, 2).value = "OURS_MOD"
    wb.save(trunk_mod)
    wb.close()

    # inter_mod: fork + 改 ID2 col2（theirs 改，生产者中间版本改动）
    inter_mod = TMP / "inter_mod.xlsx"
    shutil.copy2(FORK, inter_mod)
    wb = load_workbook(inter_mod)
    wb[SHEET].cell(r2, 2).value = "THEIRS_MOD"
    wb.save(inter_mod)
    wb.close()

    fs = {
        FORK.name: read_excel(str(FORK)),
        trunk_mod.name: read_excel(str(trunk_mod)),
        inter_mod.name: read_excel(str(inter_mod)),
    }
    merged = compare_sheet(fs, FORK.name, SHEET, merge_base_file=FORK.name)
    rows = merged["rows"]

    def cell_of(pk):
        for r in rows:
            if str(r["key"]) == str(pk):
                for c in r["cells"]:
                    if c["col"] == 1:  # col2 (0-based 1)
                        return c
        return None

    c1 = cell_of(id1)
    c2 = cell_of(id2)
    print(f"ID1={id1} row{r1} (ours改, 原={orig1}): changed={c1['changed']} "
          f"conflict={c1['conflict']} cell.value={c1['value']} 期望 OURS_MOD")
    print(f"ID2={id2} row{r2} (theirs改, 原={orig2}): changed={c2['changed']} "
          f"conflict={c2['conflict']} cell.value={c2['value']} 期望 THEIRS_MOD")

    # apply 写回 trunk_mod（ours 基准，模拟 stage2 apply）
    req = SimpleNamespace(
        base_file=trunk_mod.name,
        sheets=[SimpleNamespace(name=SHEET, rows=_wrap_rows(rows))],
    )
    wb = load_workbook(trunk_mod, data_only=False)
    _apply_edits_to_workbook(wb, req)
    out = TMP / "out_e2e.xlsx"
    wb.save(out)
    wb.close()

    wb2 = load_workbook(out, data_only=True)
    out1 = wb2[SHEET].cell(r1, 2).value
    out2 = wb2[SHEET].cell(r2, 2).value
    wb2.close()
    print(f"导出: ID1 col2={out1} (期望 OURS_MOD)  ID2 col2={out2} (期望 THEIRS_MOD)")

    ok = out1 == "OURS_MOD" and out2 == "THEIRS_MOD"
    print("结论:", "端到端三方合并正确 —— ID1 keep ours、ID2 take theirs ✓" if ok else "失败 ❌")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

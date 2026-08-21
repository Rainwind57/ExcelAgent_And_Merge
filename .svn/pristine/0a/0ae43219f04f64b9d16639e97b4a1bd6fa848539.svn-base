"""公式引用位移端到端测试。

全部用自构 xlsx（不依赖 resources/qa_test 样本）——样本结构可能被历史测试
改动而脆弱。自构表更稳健，且更好证明泛化：位移逻辑不硬编码任何表名，
任意结构含公式的 xlsx 增删行列都自动适用。

文本断言为基线（不依赖 libreoffice）；重算值断言需 libreoffice，不可用则跳过。
经 libreoffice 重算后公式文本可能被规范化（如 #REF!→#ref!），断言用小写比较。
"""
import shutil
import tempfile
import openpyxl
from pathlib import Path

from agent.excel.cli_interface import StubCodeMakerCLI
from agent.excel.formula_cache_validator import FormulaCacheValidator

ROOT = Path(__file__).resolve().parents[2]
FS = ROOT / "resources/qa_test/formula_samples"


def _build_sum_sample(d: Path) -> Path:
    """数据行 F3:F12 各 =SUM(B:E)，汇总 F13=SUM(F3:F12)（不含自身，无循环）。"""
    p = d / "sum.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "S"
    ws.append(["id", "a", "b", "c", "d", "sum"])
    ws.append(["int"] * 6)
    for i in range(1, 11):
        r = 2 + i
        ws.append([i, i, i, i, i, f"=SUM(B{r}:E{r})"])
    ws.append([99, "", "", "", "", "=SUM(F3:F12)"])  # 汇总 r=13
    wb.save(p)
    wb.close()
    return p


def _build_mul_sample(d: Path) -> Path:
    """E 列 =C*D，数据行 3-7。"""
    p = d / "mul.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "M"
    ws.append(["id", "a", "b", "c", "prod"])
    ws.append(["int"] * 5)
    for i in range(1, 6):
        r = 2 + i
        ws.append([i, i * 10, i * 10 + 1, i * 10 + 2, f"=C{r}*D{r}"])
    wb.save(p)
    wb.close()
    return p


def _build_cross_sample(d: Path, name: str) -> Path:
    """主表 B列=VLOOKUP(A,Ref!A:B,2,FALSE)，查表 Ref A:key B:val。"""
    p = d / name
    wb = openpyxl.Workbook()
    main = wb.active
    main.title = "Main"
    ref = wb.create_sheet("Ref")
    ref.append(["key", "val"])
    ref.append(["int", "int"])
    for i in range(1, 6):
        ref.append([i, i * 100])
    main.append(["id", "lookup"])
    main.append(["int", "int"])
    for i in range(1, 6):
        r = 2 + i
        main.append([i, f"=VLOOKUP(A{r},Ref!A:B,2,FALSE)"])
    wb.save(p)
    wb.close()
    return p


def _load_formulas(path: Path, sheet: str) -> dict:
    wb = openpyxl.load_workbook(path, data_only=False)
    ws = wb[sheet]
    out = {}
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value.startswith("="):
                out[c.coordinate] = c.value
    wb.close()
    return out


def test_e2e_delete_row_range_shrink():
    """删中间行 → 末行汇总范围收缩，数据行公式行号位移。"""
    print("\n=== E2E 删行：范围收缩 ===")
    with tempfile.TemporaryDirectory() as d:
        p = _build_sum_sample(Path(d))
        cli = StubCodeMakerCLI(d)
        r = cli.delete_row(p, "S", 5)
        print(f"  ok={r.ok} needs_manual_fix={r.needs_manual_fix}")
        assert r.ok
        formulas = _load_formulas(p, "S")
        # 汇总原 F13 上移到 F12，范围 F3:F12 → F3:F11
        assert formulas.get("F12", "").lower() == "=sum(f3:f11)", f"F12={formulas.get('F12')!r}"
        # 原 F6(=SUM(B6:E6)) 上移到 F5，行号 6→5
        assert formulas.get("F5", "").lower() == "=sum(b5:e5)", f"F5={formulas.get('F5')!r}"
        # 原 F4 行号<5 不受影响
        assert formulas.get("F4", "").lower() == "=sum(b4:e4)", f"F4={formulas.get('F4')!r}"
        print("  PASS：汇总范围收缩 + 数据行公式行号位移")


def test_e2e_delete_col():
    """删 C 列 → D 引用变 C，C 引用变 #REF!，needs_manual_fix 阻断。"""
    print("\n=== E2E 删列 ===")
    with tempfile.TemporaryDirectory() as d:
        p = _build_mul_sample(Path(d))
        cli = StubCodeMakerCLI(d)
        r = cli.delete_column(p, "M", 3)
        print(f"  ok={r.ok} needs_manual_fix={r.needs_manual_fix}")
        print(f"  cache_message: {r.cache_message}")
        assert r.ok
        # 原 E3=C3*D3 现 D3 位：C3 被删 #REF!，D3→C3 → =#REF!*C3
        formulas = _load_formulas(p, "M")
        assert formulas.get("D3", "").lower() == "=#ref!*c3", f"D3={formulas.get('D3')!r}"
        assert r.needs_manual_fix, "产生#REF!应标记需人工"
        print("  PASS：删列后引用位移 + #REF! 阻断")


def test_e2e_insert_col_shift():
    """中间插列 → 右侧列引用位移。"""
    print("\n=== E2E 插列 ===")
    with tempfile.TemporaryDirectory() as d:
        p = _build_mul_sample(Path(d))
        cli = StubCodeMakerCLI(d)
        # 在 C 列(第3)后插新列 → 新列第4位
        r = cli.insert_column(p, "M", "新列", after=3)
        print(f"  ok={r.ok} needs_manual_fix={r.needs_manual_fix}")
        assert r.ok
        # 原 E3=C3*D3 现 F3 位：C3(col3<4)不变，D3(col4>=4)→E3 → =C3*E3
        formulas = _load_formulas(p, "M")
        assert formulas.get("F3", "").lower() == "=c3*e3", f"F3={formulas.get('F3')!r}"
        print("  PASS：插列后右侧引用位移")


def test_e2e_delete_row_recalc():
    """删行后 libreoffice 重算，汇总值应 = 原值 - 被删行值。"""
    print("\n=== E2E 删行：重算值正确 ===")
    validator = FormulaCacheValidator()
    if not validator.libreoffice_available:
        print("  SKIP：libreoffice 不可用")
        return
    with tempfile.TemporaryDirectory() as d:
        p = _build_sum_sample(Path(d))
        validator._runner.recalculate(p)
        wb_c = openpyxl.load_workbook(p, data_only=True)
        orig_total = wb_c["S"]["F13"].value      # 原汇总 = 4*55 = 220
        deleted_f5 = wb_c["S"]["F5"].value       # 被删 F5 = 4*3 = 12
        wb_c.close()
        print(f"  原汇总={orig_total} 被删F5={deleted_f5}")
        cli = StubCodeMakerCLI(d)
        cli.delete_row(p, "S", 5)
        # 删第5行后汇总 F13→F12，范围 F3:F12→F3:F11
        wb_f = openpyxl.load_workbook(p, data_only=False)
        assert wb_f["S"]["F12"].value.lower() == "=sum(f3:f11)", wb_f["S"]["F12"].value
        wb_f.close()
        wb_c = openpyxl.load_workbook(p, data_only=True)
        new_total = wb_c["S"]["F12"].value
        wb_c.close()
        expected = orig_total - deleted_f5
        print(f"  新汇总={new_total} 期望={expected}")
        assert new_total == expected, f"重算值应={expected} 实际={new_total}"
        print("  PASS：重算值正确")


def test_e2e_cross_sheet():
    """跨 sheet 引用：删主表行只动主表引用，删查表行主表引用不动。"""
    print("\n=== E2E 跨 sheet 引用 ===")
    with tempfile.TemporaryDirectory() as d:
        cli = StubCodeMakerCLI(d)
        # 场景1：删主表行 → 主表无前缀引用位移，查表引用 Ref!A:B 不动
        p1 = _build_cross_sample(Path(d), "c1.xlsx")
        cli.delete_row(p1, "Main", 4)
        f1 = _load_formulas(p1, "Main")
        # 原 B5=VLOOKUP(A5,Ref!A:B,...) 上移 B4，A5→A4(数据上移)，Ref!A:B 不动
        assert f1.get("B4", "").lower() == "=vlookup(a4,ref!a:b,2,false)", f"B4={f1.get('B4')!r}"
        # 场景2：删查表行 → 主表引用全不动(A3 无前缀属 Main≠Ref；Ref!A:B 整列删行不动)
        p2 = _build_cross_sample(Path(d), "c2.xlsx")
        cli.delete_row(p2, "Ref", 3)
        f2 = _load_formulas(p2, "Main")
        assert f2.get("B3", "").lower() == "=vlookup(a3,ref!a:b,2,false)", f"B3={f2.get('B3')!r}"
        print("  PASS：跨 sheet 引用按目标 sheet 精准过滤")


def test_e2e_real_formula_sum_delete_row():
    """真实 formula_sum.xlsx（规范结构）删行验证——样本表也通用。

    formula_sum.xlsx 结构：行1表头/行2类型/行3-10数据/行11汇总(F11==SUM(F3:F10))。
    删行5后：F11→F10，公式范围 F3:F10 → F3:F9（行5删除，行6-10上移为5-9）。
    """
    print("\n=== E2E 真实 formula_sum 删行 ===")
    src = FS / "formula_sum.xlsx"
    if not src.exists():
        print("  SKIP：formula_sum.xlsx 不存在")
        return
    p = src.parent / f"_real_{src.name}"
    shutil.copy2(src, p)
    try:
        cli = StubCodeMakerCLI("resources")
        cli.delete_row(p, "SeasonStat", 5)
        formulas = _load_formulas(p, "SeasonStat")
        # 汇总原 F11 上移 F10，范围 F3:F10 → F3:F9
        assert formulas.get("F10", "").lower() == "=sum(f3:f9)", f"F10={formulas.get('F10')!r}"
        print("  PASS：真实 formula_sum 删行后汇总范围正确收缩")
    finally:
        try:
            p.unlink()
        except PermissionError:
            pass


if __name__ == "__main__":
    test_e2e_delete_row_range_shrink()
    test_e2e_delete_col()
    test_e2e_insert_col_shift()
    test_e2e_delete_row_recalc()
    test_e2e_cross_sheet()
    test_e2e_real_formula_sum_delete_row()
    print("\n=== 全部 E2E 测试完成 ===")

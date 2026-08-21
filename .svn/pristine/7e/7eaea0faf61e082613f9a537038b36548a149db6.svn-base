"""merge_eval 种子生成器（capability: merge-evaluation）。

生成小种子（eval_seed 系列，验证正确性指标）+ 大表种子（big_data 系列，
--bigdata 计时用；单元测试仅需文件存在，故生成 modest 规模保速度）。

种子结构（compare_sheet 约定）：
  - 行 0 = 表头，行 1+ = 数据行，第一列 = 主键(PK)
  - TestData sheet 列：id, value, desc, name, flag

正确性预期（ground_truth）：
  - id=1 value = 10/20/30（三版本不同 → 真冲突）
  - id=2 value = 100/"100.0"/"1e2"（语义等价 → #24 归一后非冲突，false_conflict）
"""
from __future__ import annotations

from pathlib import Path

try:
    import openpyxl
except ImportError:  # pragma: no cover
    openpyxl = None

TESTS_DIR = Path(__file__).resolve().parent.parent
FIXTURE_DIR = TESTS_DIR / "merge_fixtures"
SEED_DIR = FIXTURE_DIR / "seeds"
BIGDATA_DIR = FIXTURE_DIR / "bigdata"

_HEADERS = ["id", "value", "desc", "name", "flag", "calc"]


def _write_xlsx(path: Path, sheet: str, rows: list[list]) -> None:
    """写单 sheet xlsx，rows[0]=表头，rows[1+]=数据。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet
    for r in rows:
        ws.append(r)
    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(path)
    wb.close()


def gen_seeds() -> None:
    """生成 eval_seed.xlsx / eval_seed_1.xlsx / eval_seed_2.xlsx + ground_truth.json。

    id=1 value 三版本不同（10/20/30 → 真冲突）；
    id=2 value 语义等价（100/"100.0"/"1e2" → #24 归一后非冲突）。
    其余列三版本一致，避免额外冲突。
    """
    if openpyxl is None:
        raise RuntimeError("openpyxl 不可用，无法生成 merge 种子")

    # (id, value, desc, name, flag, calc) per file
    # flag 列被 _EVAL_STRATEGIES 标 base_priority → 视为 FK 引用 id，值须 ∈ 存在的 id
    # id=1 value 三版本不同（10/20/30 → 真冲突）；id=2 value 语义等价（→ 非冲突）
    # id=2000 双分支插入同 PK（d1+d2 各一条，内容不同）→ resolve_id_conflicts 重映射后到者
    # calc 公式列：触发 compare_sheet inline 路径（formulas_active）以执行 resolve_id_conflicts；
    #   公式文本同 → diff_type='formula' 不产生 conflict 单元格
    base_rows = [_HEADERS,
                 [1, 10, "d1", "n1", 2, "=B2*2"],     # flag=2 引用 id=2
                 [2, 100, "d2", "n2", 1, "=B3*2"]]    # flag=1 引用 id=1
    d1_rows = [_HEADERS,
               [1, 20, "d1", "n1", 2, "=B2*2"],
               [2, "100.0", "d2", "n2", 1, "=B3*2"],
               [2000, 999, "new_d1", "n2000", 1, "=B4*2"]]
    d2_rows = [_HEADERS,
               [1, 30, "d1", "n1", 2, "=B2*2"],
               [2, "1e2", "d2", "n2", 1, "=B3*2"],
               [2000, 888, "new_d2", "n2000b", 1, "=B4*2"]]

    _write_xlsx(SEED_DIR / "eval_seed.xlsx", "TestData", base_rows)
    _write_xlsx(SEED_DIR / "eval_seed_1.xlsx", "TestData", d1_rows)
    _write_xlsx(SEED_DIR / "eval_seed_2.xlsx", "TestData", d2_rows)

    # ground_truth: id=2/value 是「语义等价→若无 #24 会误报」的 false_conflict cell
    import json
    gt = {"sheets": {"TestData": {"false_conflict_cells": [[2, "value"]]}}}
    (SEED_DIR / "ground_truth.json").write_text(
        json.dumps(gt, ensure_ascii=False, indent=2), encoding="utf-8")


def gen_bigdata(rows_per_sheet: int = 500, sheets: int = 4) -> None:
    """生成 big_data.xlsx / _1 / _2（4 sheet × N 行，--bigdata 计时用）。

    单元测试仅需文件存在；生成 modest 规模保速度。衍生文件含少量改动供 compare 工作。
    """
    if openpyxl is None:
        raise RuntimeError("openpyxl 不可用，无法生成 bigdata 种子")

    sheet_names = [f"Big{i}" for i in range(1, sheets + 1)]
    hdr = ["id", "value", "desc"]

    def build_rows(derived: int) -> dict[str, list[list]]:
        out = {}
        for sn in sheet_names:
            rows = [hdr]
            for i in range(1, rows_per_sheet + 1):
                # 衍生文件：偶数 id 的 value 改动（制造 changed/conflict）
                if derived == 0:
                    v = i
                elif derived == 1:
                    v = i + (10 if i % 2 == 0 else 0)
                else:
                    v = i + (20 if i % 2 == 0 else 0)
                rows.append([i, v, f"{sn}_d{i}"])
            out[sn] = rows
        return out

    for derived, suffix in [(0, ""), (1, "_1"), (2, "_2")]:
        path = BIGDATA_DIR / f"big_data{suffix}.xlsx"
        wb = openpyxl.Workbook()
        # 删除默认 sheet，按 sheet_names 依次建
        wb.remove(wb.active)
        for sn, rows in build_rows(derived).items():
            ws = wb.create_sheet(sn)
            for r in rows:
                ws.append(r)
        BIGDATA_DIR.mkdir(parents=True, exist_ok=True)
        wb.save(path)
        wb.close()


if __name__ == "__main__":
    gen_seeds()
    gen_bigdata()
    print(f"seeds -> {SEED_DIR}")
    print(f"bigdata -> {BIGDATA_DIR}")

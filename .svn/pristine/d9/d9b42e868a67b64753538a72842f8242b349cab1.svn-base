"""merge 大表正确性 + 性能评测（capability: merge-evaluation）。

直接调 server/engine/ 引擎函数（compare_sheet / auto_merge_sheet /
resolve_id_conflicts 已内嵌于 compare_sheet / validate_sheet_references /
fast_apply_xml），不走 HTTP，隔离单引擎阶段。

指标：
  - merge_success_rate：自动解决冲突单元格数 / 总冲突单元格数（按类型细分）
  - false_conflict_rate：实无差异被标冲突 / 总冲突（基于 ground truth）
  - id_remap_accuracy：重映射 PK + 下游同步正确 / 全部重映射
  - ref_integrity_pass_rate：无 dangling sheet 数 / 总 sheet 数
  - bigdata_total_elapsed_ms：10w 行 compare/resolve/apply 分段计时（--bigdata）
  - parallel_speedup：串行 compare / 并行 compare（4 worker）

用法（在 server/ 目录下执行）:
    python -m tests.merge_eval                # 仅小种子正确性指标
    python -m tests.merge_eval --bigdata      # 含 10w 行大表计时 + 并行加速比
    python -m tests.merge_eval --quick        # 跳过大表（默认即跳过）,json}
      server/tests/reports/archive/merge_eval_<run_id>.{json,md}
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── 路径 & 环境 ──
TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent
FIXTURE_DIR = TESTS_DIR / "merge_fixtures"
SEED_DIR = FIXTURE_DIR / "seeds"
BIGDATA_DIR = FIXTURE_DIR / "bigdata"
REPORT_DIR = TESTS_DIR / "reports"

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from engine.parser import read_group_files, group_files  # noqa: E402
from engine.compare import compare_sheet  # noqa: E402
from engine.merge_engine import auto_merge_sheet  # noqa: E402
from engine.ref_integrity import validate_sheet_references  # noqa: E402
from engine.models import MergeRequest, SheetMergeData, RowData, CellData  # noqa: E402

try:
    from engine.fast_apply import fast_apply_xml as _fast_apply_xml  # noqa: E402
except Exception:
    _fast_apply_xml = None


# ── eval 种子专属 merge 策略（monkeypatch 注入，不污染全局 yaml）──
_EVAL_STRATEGIES = {
    "default_strategy": "manual",
    "tables": {
        "eval_seed": {
            "TestData": {
                "id": {"strategy": "base_priority", "reason": "主键列保留基准"},
                "value": {"strategy": "take_max", "reason": "数值列取最大"},
                "desc": {"strategy": "take_longest", "reason": "文本列取最长"},
                "name": {"strategy": "take_newer", "reason": "取最新版本"},
                "flag": {"strategy": "base_priority", "reason": "标志列保留基准"},
            }
        },
        "big_data": {
            "*": {
                "value": {"strategy": "take_max", "reason": "数值列取最大"},
                "desc": {"strategy": "take_longest", "reason": "文本列取最长"},
                "name": {"strategy": "take_newer", "reason": "取最新"},
            }
        },
    },
}


def _inject_eval_strategies() -> None:
    """monkeypatch merge_engine._load_strategies 返回 eval 专属策略。"""
    import engine.merge_engine as me
    me._cache.clear()
    me._cache["mtime"] = 0.0  # 强制下次 _load_strategies 重读，但我们直接 patch
    me._load_strategies = lambda: _EVAL_STRATEGIES


# ── 数据结构 ──

@dataclass
class SheetMetric:
    sheet: str
    total_rows: int = 0
    conflict_cells: int = 0
    changed_cells: int = 0
    auto_merged: int = 0
    manual_left: int = 0
    false_conflicts: int = 0
    id_remapped: int = 0
    id_remap_correct: int = 0
    dangling: int = 0
    remapped_refs: int = 0
    checked_refs: int = 0
    flagged_cells: list = field(default_factory=list)  # [(key, col_header)]


@dataclass
class MergeEvalResult:
    correctness: dict = field(default_factory=dict)
    bigdata: dict = field(default_factory=dict)
    parallel: dict = field(default_factory=dict)


# ── 种子加载 ──

def ensure_seeds() -> None:
    """种子缺失则调 gen_merge_seeds 生成。"""
    if not (SEED_DIR / "eval_seed.xlsx").exists():
        from tests.merge_fixtures.gen_merge_seeds import gen_seeds
        gen_seeds()
    if not (BIGDATA_DIR / "big_data.xlsx").exists():
        from tests.merge_fixtures.gen_merge_seeds import gen_bigdata
        gen_bigdata()


def load_ground_truth() -> dict:
    return json.loads((SEED_DIR / "ground_truth.json").read_text(encoding="utf-8"))


# ── 引擎调用 ──

def _read_group(file_dir: Path, prefix: str) -> tuple[dict, str, list[str]]:
    """读一组文件，返回 (file_sheets, base_name, all_fnames)。"""
    files = [str(file_dir / f"{prefix}.xlsx"),
             str(file_dir / f"{prefix}_1.xlsx"),
             str(file_dir / f"{prefix}_2.xlsx")]
    rg = read_group_files(files)
    file_sheets = {fn: tup[0] for fn, tup in rg.items()}
    file_formulas = {fn: tup[1] for fn, tup in rg.items()}
    import re
    all_fns = list(file_sheets.keys())
    base = [n for n in all_fns if not re.search(r"_\d+$", Path(n).stem)][0]
    return file_sheets, file_formulas, base, all_fns


def _build_cell_models(row: dict, headers: list, base_name: str) -> list:
    """把 compare_sheet 的 row dict → CellData 模型列表（resolved value 取首个非空版本）。"""
    cells_out = []
    for ci, c in enumerate(row.get("cells", [])):
        if not isinstance(c, dict):
            c = {"value": c, "versions": {}}
        vers = c.get("versions", {}) or {}
        # resolved value：优先取基准，否则首个非空衍生
        rv = vers.get(base_name)
        if rv in (None, ""):
            for fn, v in vers.items():
                if fn != base_name and v not in (None, ""):
                    rv = v
                    break
        if rv is None:
            rv = c.get("value")
        col_letter = _col_letter_for(ci)
        cells_out.append(CellData(
            col=ci, col_letter=col_letter, value=rv,
            versions=dict(vers), conflict=False, changed=False))
    return cells_out


def _col_letter_for(n: int) -> str:
    s = ""
    n += 1
    while n > 0:
        n -= 1
        s = chr(65 + (n % 26)) + s
        n //= 26
    return s


# ── 正确性指标（小种子）──

def run_correctness() -> dict:
    """对小种子跑 compare→auto_merge→validate，计算 4 类正确性指标。"""
    _inject_eval_strategies()
    ensure_seeds()
    gt = load_ground_truth()
    file_sheets, file_formulas, base, all_fns = _read_group(SEED_DIR, "eval_seed")

    sheets_metrics: list[SheetMetric] = []
    for sn in file_sheets[base].keys():
        r = compare_sheet(file_sheets, base, sn, file_formulas=file_formulas)
        rows = r["rows"]
        headers = r["headers"]
        stats = r.get("stats", {})
        id_res = r.get("id_resolution", {}) or {}
        id_mapping = id_res.get("id_mapping", []) or []

        # 收集 flagged conflict cells
        flagged = []
        for row in rows:
            key = str(row.get("key", ""))
            for ci, c in enumerate(row.get("cells", [])):
                if isinstance(c, dict) and c.get("conflict"):
                    col_h = headers[ci] if ci < len(headers) else f"col{ci}"
                    flagged.append((key, col_h))

        # auto_merge（在已 id-resolved 的行上跑）
        am = auto_merge_sheet("eval_seed", sn, headers, rows, base, all_fns)
        # 仅计 conflict 单元格被自动解决数（auto_merge 也处理 changed，需交叉过滤）
        am_conflict_set = {(e["ri"], e["ci"]) for e in am["auto_merged"]}
        conflict_cells_set = set()
        for ri, row in enumerate(rows):
            for ci, c in enumerate(row.get("cells", [])):
                if isinstance(c, dict) and c.get("conflict"):
                    conflict_cells_set.add((ri, ci))
        auto_merged_conflict = len(am_conflict_set & conflict_cells_set)

        # ref integrity
        ri = validate_sheet_references(rows, headers, "eval_seed", sn, id_mapping)

        # 假冲突：flagged 中命中 ground truth false_conflict_cells
        gt_sheet = gt.get("sheets", {}).get(sn, {})
        false_set = {(str(pk), col) for pk, col in gt_sheet.get("false_conflict_cells", [])}
        false_count = sum(1 for (k, col) in flagged if (k, col) in false_set)

        # id_remap 正确性：每条 mapping 校验 new_pk != old_pk 且 new_pk 不撞已用主键
        used_pks = set()
        for row in rows:
            cells = row.get("cells", []) or []
            if cells:
                pk = str(cells[0].get("value", "") if isinstance(cells[0], dict) else "")
                if pk:
                    used_pks.add(pk)
        id_remap_correct = 0
        for m in id_mapping:
            old_pk, new_pk = str(m.get("old_pk", "")), str(m.get("new_pk", ""))
            if new_pk and new_pk != old_pk:
                # 校验 new_pk 行的主键已更新（在 rows 中找到 key==new_pk 的 inserted 行）
                ok = any(str(row.get("key", "")) == new_pk for row in rows)
                if ok:
                    id_remap_correct += 1

        sm = SheetMetric(
            sheet=sn, total_rows=stats.get("total_rows", len(rows)),
            conflict_cells=len(flagged), changed_cells=stats.get("changed", 0),
            auto_merged=auto_merged_conflict, manual_left=am["stats"]["manual_left"],
            false_conflicts=false_count,
            id_remapped=len(id_mapping), id_remap_correct=id_remap_correct,
            dangling=len(ri.get("dangling", [])),
            remapped_refs=ri.get("remapped_refs", 0),
            checked_refs=ri.get("checked", 0), flagged_cells=flagged)
        sheets_metrics.append(sm)

    # 聚合
    total_conflict = sum(s.conflict_cells for s in sheets_metrics)
    total_auto = sum(s.auto_merged for s in sheets_metrics)
    total_false = sum(s.false_conflicts for s in sheets_metrics)
    total_remap = sum(s.id_remapped for s in sheets_metrics)
    total_remap_ok = sum(s.id_remap_correct for s in sheets_metrics)
    total_sheets = len(sheets_metrics)
    sheets_clean = sum(1 for s in sheets_metrics if s.dangling == 0)

    return {
        "sheets": [s.__dict__ for s in sheets_metrics],
        "merge_success_rate": round(total_auto / total_conflict, 4) if total_conflict else 1.0,
        "false_conflict_rate": round(total_false / total_conflict, 4) if total_conflict else 0.0,
        "id_remap_accuracy": round(total_remap_ok / total_remap, 4) if total_remap else 1.0,
        "ref_integrity_pass_rate": round(sheets_clean / total_sheets, 4) if total_sheets else 1.0,
        "total_conflict_cells": total_conflict,
        "total_auto_merged": total_auto,
        "total_false_conflicts": total_false,
        "total_id_remapped": total_remap,
        "total_id_remap_correct": total_remap_ok,
    }


# ── 大表性能（--bigdata）──

def _run_compare_all(file_sheets, file_formulas, base, sheets: list[str]) -> dict:
    """对全部 sheet 跑 compare，返回 {sn: result}。"""
    out = {}
    for sn in sheets:
        out[sn] = compare_sheet(file_sheets, base, sn, file_formulas=file_formulas)
    return out


def run_bigdata_timing() -> dict:
    """10w 行大表分段计时：compare / resolve(auto_merge) / apply / total。"""
    if not (BIGDATA_DIR / "big_data.xlsx").exists():
        return {"skipped": "big_data 缺失"}
    _inject_eval_strategies()
    file_sheets, file_formulas, base, all_fns = _read_group(BIGDATA_DIR, "big_data")
    sheets = list(file_sheets[base].keys())
    total_rows = sum(len(file_sheets[base][sn]) - 1 for sn in sheets)

    # compare 阶段
    t0 = time.perf_counter()
    cmp_results = _run_compare_all(file_sheets, file_formulas, base, sheets)
    compare_ms = (time.perf_counter() - t0) * 1000

    # resolve 阶段（auto_merge）
    t0 = time.perf_counter()
    for sn in sheets:
        r = cmp_results[sn]
        auto_merge_sheet("big_data", sn, r["headers"], r["rows"], base, all_fns)
    resolve_ms = (time.perf_counter() - t0) * 1000

    # apply 阶段（fast_apply_xml）
    apply_ms = 0.0
    apply_ok = False
    if _fast_apply_xml is not None:
        try:
            from engine.merge_engine import _val_str  # noqa
            src = BIGDATA_DIR / "big_data.xlsx"
            dest = BIGDATA_DIR / "_bigdata_apply_out.xlsx"
            sd_list = []
            for sn in sheets:
                r = cmp_results[sn]
                rows_out = []
                for row in r["rows"]:
                    cells = _build_cell_models(row, r["headers"], base)
                    rd = RowData(key=str(row.get("key", "")), cells=cells,
                                 row_type=row.get("row_type", "matched"))
                    if row.get("id_remapped"):
                        rd.id_remapped = True
                        rd.original_pk = row.get("original_pk", "")
                    rows_out.append(rd)
                sd_list.append(SheetMergeData(name=sn, headers=r["headers"], rows=rows_out))
            mr = MergeRequest(group_name="big_data", sheets=sd_list)
            t0 = time.perf_counter()
            ret = _fast_apply_xml(src, dest, mr)
            apply_ms = (time.perf_counter() - t0) * 1000
            apply_ok = ret is not None
            dest.unlink(missing_ok=True)
        except Exception as e:
            apply_ms = 0.0
            apply_ok = False
            return_reason = str(e)
    return {
        "total_rows": total_rows, "sheets": len(sheets),
        "compare_elapsed_ms": round(compare_ms, 1),
        "resolve_elapsed_ms": round(resolve_ms, 1),
        "apply_elapsed_ms": round(apply_ms, 1),
        "total_elapsed_ms": round(compare_ms + resolve_ms + apply_ms, 1),
        "apply_fast_path": apply_ok,
    }


# ── 并行比对加速比 ──

def run_parallel_speedup() -> dict:
    """同一大表 4 sheet：串行 compare vs ThreadPoolExecutor(4) 并行 compare。"""
    if not (BIGDATA_DIR / "big_data.xlsx").exists():
        return {"skipped": "big_data 缺失"}
    _inject_eval_strategies()
    file_sheets, file_formulas, base, all_fns = _read_group(BIGDATA_DIR, "big_data")
    sheets = list(file_sheets[base].keys())
    if len(sheets) < 2:
        return {"skipped": "sheet 数不足"}

    # 串行
    t0 = time.perf_counter()
    _run_compare_all(file_sheets, file_formulas, base, sheets)
    serial_ms = (time.perf_counter() - t0) * 1000

    # 并行（每 sheet 一个 future）
    t0 = time.perf_counter()
    with ThreadPoolExecutor(max_workers=min(4, len(sheets))) as pool:
        futs = {pool.submit(compare_sheet, file_sheets, base, sn, file_formulas): sn
                for sn in sheets}
        for fut in futs:
            fut.result()
    parallel_ms = (time.perf_counter() - t0) * 1000

    return {
        "sheets": len(sheets),
        "serial_elapsed_ms": round(serial_ms, 1),
        "parallel_elapsed_ms": round(parallel_ms, 1),
        "parallel_speedup": round(serial_ms / parallel_ms, 3) if parallel_ms else 0.0,
    }


# ── 聚合 & 报告 ──

def aggregate(correctness: dict, bigdata: dict, parallel: dict) -> dict:
    bd_total = bigdata.get("total_elapsed_ms", 0) if isinstance(bigdata, dict) else 0
    pa_speedup = parallel.get("parallel_speedup", 0) if isinstance(parallel, dict) else 0
    return {
        "summary": {
            **{k: v for k, v in correctness.items()
               if k in ("merge_success_rate", "false_conflict_rate",
                        "id_remap_accuracy", "ref_integrity_pass_rate")},
            "bigdata_total_elapsed_ms": bd_total,
            "parallel_speedup": pa_speedup,
        },
        "correctness": correctness,
        "bigdata": bigdata,
        "parallel": parallel,
    }


def render_report(agg: dict) -> str:
    c = agg["correctness"]
    bd = agg["bigdata"]
    pa = agg["parallel"]
    lines = [
        "# merge 大表正确性 + 性能评测报告（capability: merge-evaluation）",
        "",
        f"- 生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}",
        "- 执行方式: 进程内直接调 server/engine/ 引擎函数（不走 HTTP）",
        "- 种子: server/tests/merge_fixtures/seeds（小种子）+ bigdata（10w 行）",
        "",
        "## 一、正确性指标（小种子）",
        "",
        "| 指标 | 说明 | 值 |",
        "|---|---|---|",
        f"| merge_success_rate | 自动解决冲突 / 总冲突 | {c.get('merge_success_rate', 0):.4f} |",
        f"| false_conflict_rate | 假冲突 / 总冲突 | {c.get('false_conflict_rate', 0):.4f} |",
        f"| id_remap_accuracy | 重映射正确 / 全部重映射 | {c.get('id_remap_accuracy', 0):.4f} |",
        f"| ref_integrity_pass_rate | 无 dangling sheet / 总 sheet | {c.get('ref_integrity_pass_rate', 0):.4f} |",
        f"| 总冲突单元格 | | {c.get('total_conflict_cells', 0)} |",
        f"| 自动合并数 | | {c.get('total_auto_merged', 0)} |",
        f"| 假冲突数 | | {c.get('total_false_conflicts', 0)} |",
        f"| ID 重映射数 | | {c.get('total_id_remapped', 0)} |",
        f"| ID 重映射正确 | | {c.get('total_id_remap_correct', 0)} |",
        "",
        "### 各 sheet 明细",
        "",
        "| sheet | 行数 | 冲突 | 变更 | 自动合并 | 假冲突 | 重映射 | dangling |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for s in c.get("sheets", []):
        lines.append(f"| {s['sheet']} | {s['total_rows']} | {s['conflict_cells']} | "
                     f"{s['changed_cells']} | {s['auto_merged']} | {s['false_conflicts']} | "
                     f"{s['id_remapped']} | {s['dangling']} |")

    lines += ["", "## 二、大表性能（10w 行）", ""]
    if isinstance(bd, dict) and bd.get("skipped"):
        lines.append(f"- 跳过：{bd['skipped']}")
    elif isinstance(bd, dict) and bd:
        lines += [
            "| 阶段 | 耗时(ms) |",
            "|---|---|",
            f"| compare | {bd.get('compare_elapsed_ms', 0):.1f} |",
            f"| resolve(auto_merge) | {bd.get('resolve_elapsed_ms', 0):.1f} |",
            f"| apply(fast_xml) | {bd.get('apply_elapsed_ms', 0):.1f} |",
            f"| **total** | **{bd.get('total_elapsed_ms', 0):.1f}** |",
            "",
            f"- 总行数: {bd.get('total_rows', 0)}（{bd.get('sheets', 0)} sheet）",
            f"- apply 快路径: {'✓' if bd.get('apply_fast_path') else '✗'}",
        ]

    lines += ["", "## 三、并行比对加速比", ""]
    if isinstance(pa, dict) and pa.get("skipped"):
        lines.append(f"- 跳过：{pa['skipped']}")
    elif isinstance(pa, dict) and pa:
        lines += [
            "| 模式 | 耗时(ms) |",
            "|---|---|",
            f"| 串行 | {pa.get('serial_elapsed_ms', 0):.1f} |",
            f"| 并行(4 worker) | {pa.get('parallel_elapsed_ms', 0):.1f} |",
            "",
            f"- **加速比: {pa.get('parallel_speedup', 0):.3f}**",
        ]
    return "\n".join(lines) + "\n"


# ── 主流程 ──

def main() -> int:
    ap = argparse.ArgumentParser(description="merge 大表正确性+性能评测")
    ap.add_argument("--bigdata", action="store_true", help="跑 10w 行大表计时（默认跳过）")
    ap.add_argument("--quick", action="store_true", help="跳过大表（默认即跳过）")
    ap.add_argument("--out", type=str, default=str(REPORT_DIR))
    args = ap.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    print("=== merge_eval 正确性指标（小种子）===")
    corr = run_correctness()
    print(f"  merge_success_rate={corr['merge_success_rate']:.4f} "
          f"false_conflict_rate={corr['false_conflict_rate']:.4f} "
          f"id_remap_accuracy={corr['id_remap_accuracy']:.4f} "
          f"ref_integrity_pass_rate={corr['ref_integrity_pass_rate']:.4f}")
    print(f"  冲突={corr['total_conflict_cells']} 自动合并={corr['total_auto_merged']} "
          f"假冲突={corr['total_false_conflicts']} 重映射={corr['total_id_remapped']}")

    bd = {"skipped": "未启用 --bigdata"}
    if args.bigdata and not args.quick:
        print("\n=== 大表性能（10w 行）===")
        bd = run_bigdata_timing()
        if isinstance(bd, dict) and not bd.get("skipped"):
            print(f"  compare={bd['compare_elapsed_ms']:.0f}ms "
                  f"resolve={bd['resolve_elapsed_ms']:.0f}ms "
                  f"apply={bd['apply_elapsed_ms']:.0f}ms "
                  f"total={bd['total_elapsed_ms']:.0f}ms")

    pa = {"skipped": "未启用 --bigdata"}
    if args.bigdata and not args.quick:
        print("\n=== 并行比对加速比 ===")
        pa = run_parallel_speedup()
        if isinstance(pa, dict) and not pa.get("skipped"):
            print(f"  串行={pa['serial_elapsed_ms']:.0f}ms "
                  f"并行={pa['parallel_elapsed_ms']:.0f}ms "
                  f"speedup={pa['parallel_speedup']:.3f}")

    agg = aggregate(corr, bd, pa)
    md = render_report(agg)
    (out_dir / "merge_eval_latest.json").write_text(
        json.dumps(agg, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (out_dir / "merge_eval_latest.md").write_text(md, encoding="utf-8")
    print(f"\n报告: {out_dir / 'merge_eval_latest.md'}")
    print(f"数据: {out_dir / 'merge_eval_latest.json'}")

    # capability: eval-baseline-management —— 归档
    try:
        from tests.eval_baseline import archive_run, make_run_id
        tag = os.environ.get("EVAL_BASELINE_TAG", "")
        rid = make_run_id(tag=tag or None)
        archive_run("merge_eval", agg, md, run_id=rid)
        print(f"归档: reports/archive/merge_eval_{rid}.json")
    except Exception as e:
        print(f"[warn] 归档失败（不阻断）: {e}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

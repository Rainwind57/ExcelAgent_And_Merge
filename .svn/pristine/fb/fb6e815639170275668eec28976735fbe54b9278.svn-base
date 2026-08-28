"""从 _table_index.json 派生 required_fields.yaml（#30 自动生成）。

规则：每表每 sheet，每列非空率 ≥ 阈值（默认 0.9）→ 必填列。
非空率 = col_non_empty[c] / row_count（row_count=0 时跳过该 sheet）。

输出格式与 required_fields.yaml 现状对齐：
  "<stem>":
    "<sheet>":
      required: ["列名1", "列名2", ...]

合并策略：保留现有 required_fields.yaml 手工条目，派生条目仅追加不覆盖
（手工优先，派生补缺）。空表/sheet 无必填列 → 不写。
"""
from __future__ import annotations

import sys
from pathlib import Path

# server/ → agent.* 命名空间（scripts/ → skills/ → excel/ → agent/ → server/）
sys.path.insert(0, str(Path(__file__).resolve().parents[4]))


def derive(workspace: Path | None = None, threshold: float = 0.9,
           output_path: Path | None = None) -> dict:
    """从 index 派生 required_fields，合并到现有 yaml。

    Returns:
        派生后的完整 required_fields dict（stem → {sheet → {required: [cols]}}）。
    """
    import yaml
    from agent.excel.locator.table_index import load_index

    out_path = output_path or (
        Path(__file__).resolve().parent.parent / "L1_derived" / "required_fields.yaml")

    # 读现有手工条目（保留手工优先）
    # 文件结构顶层是 required_fields key 包裹：{required_fields: {stem: {sheet: [cols]}}}
    existing: dict = {}
    if out_path.exists():
        try:
            raw = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}
            if isinstance(raw, dict):
                existing = raw.get("required_fields", {}) or {}
        except Exception:
            pass

    # 派生：从 index 统计非空率
    try:
        tables = load_index()
    except Exception:
        return existing

    derived: dict = {}
    for t in tables:
        stem = t.stem
        for s in t.sheets:
            rc = s.row_count or 0
            if rc < 2:  # 空/极少行表跳过（统计无意义）
                continue
            cne = s.col_non_empty or []
            if not cne or len(cne) != len(s.headers):
                continue  # 旧索引无 col_non_empty 或长度不匹配，跳过
            req_cols: list[str] = []
            for c, col in enumerate(s.headers):
                if not col:
                    continue
                rate = cne[c] / rc if rc > 0 else 0
                if rate >= threshold:
                    req_cols.append(col)
            if req_cols:
                # 现有格式：{stem: {sheet: [cols]}}（非 {sheet: {required: [cols]}}）
                derived.setdefault(stem, {})[s.name] = req_cols

    # 合并：手工优先，派生补缺（同 stem+sheet 手工条目保留，无则追加派生）
    merged: dict = dict(existing)  # 浅拷贝保手工
    for stem, sheets in derived.items():
        if stem not in merged:
            merged[stem] = sheets
        else:
            for sn, cols in sheets.items():
                if sn not in merged[stem]:
                    merged[stem][sn] = cols
                # 同 sheet 已有手工条目 → 不覆盖（手工优先）

    # 写回（顶层 required_fields key 包裹，与 _load_required_fields 读取对齐）
    out_path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# 必填字段配置（独立文件，与 column_aliases.yaml 解耦）\n"
        "# 结构：{required_fields: {table_stem: {sheet: [field_aliases]}}}，sheet 为 \"*\" 通配所有 sheet\n"
        "# 用途：cmd_add 时检查用户是否遗漏建议必填字段（仅警告，不阻断写入）\n"
        "# 由 derive_required_fields.py 自动生成（#30）：非空率 ≥ 阈值的列 → 必填。\n"
        "# 手工条目保留优先（同 stem+sheet 不被派生覆盖）。\n\n"
    )
    out_path.write_text(
        header + yaml.safe_dump({"required_fields": merged},
                                allow_unicode=True, sort_keys=True, default_flow_style=False),
        encoding="utf-8")
    return merged


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="从 _table_index.json 派生 required_fields.yaml (#30)")
    ap.add_argument("--workspace", type=str, default=None, help="workspace 路径（默认 resources/）")
    ap.add_argument("--threshold", type=float, default=0.9, help="非空率阈值（默认 0.9）")
    ap.add_argument("--output", type=str, default=None, help="输出 yaml 路径（默认 skills/L1_derived/required_fields.yaml）")
    args = ap.parse_args()
    ws = Path(args.workspace) if args.workspace else None
    op = Path(args.output) if args.output else None
    result = derive(workspace=ws, threshold=args.threshold, output_path=op)
    n_stems = len(result)
    n_sheets = sum(len(v) for v in result.values())
    print(f"#30 derive_required_fields: {n_stems} 表 / {n_sheets} sheet 必填配置写入 {op or '默认路径'}")

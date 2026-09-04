# -*- coding: utf-8 -*-
"""案例库跨团队同步工具。

dialog_examples / dialog_failures 是本地运行时积累（git/svn 忽略），
本脚本负责：export 本地优秀/失败案例 → 合并去重 → 写入仓库 share/ 目录；
import 仓库 share/ 案例 → 合并进本地案例库（保留本地高分案例）。

用法（项目根目录）：
    uv run python tools/sync_dialog_cases.py export
    uv run python tools/sync_dialog_cases.py import
    uv run python tools/sync_dialog_cases.py export --tables pet,ability
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "server"))

EXAMPLES_DIR = PROJECT_ROOT / "server" / "agent" / "excel" / "core" / "dialog_examples"
FAILURES_DIR = PROJECT_ROOT / "server" / "agent" / "excel" / "core" / "dialog_failures"
SHARE_DIR = PROJECT_ROOT / "share"
SHARE_FILE = SHARE_DIR / "dialog_cases.jsonl"

EXAMPLE_KEEP_PER_TABLE = 50
FAILURE_KEEP_PER_TABLE = 30

# 清洗本地绝对路径（跨机器分享前必须，否则导入方解析到别人的盘符）
_ABS_PATH_RE = re.compile(r"[A-Za-z]:[\\/][^\s\"',}\]]+")
_RESOURCES_RE = re.compile(r"resources[\\/][A-Za-z0-9_./\\-]+\.xlsx")


def _iter_records(path: Path):
    if not path.exists():
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _dedup(records: list[dict]) -> list[dict]:
    """按 user_text+intent_action 去重，保留 quality_score 高者。"""
    best: dict[tuple, dict] = {}
    for r in records:
        key = (r.get("user_text", ""), r.get("intent_action", ""))
        cur = best.get(key)
        if cur is None or r.get("quality_score", 0) > cur.get("quality_score", 0):
            best[key] = r
    return list(best.values())


def _sanitize_paths(text: str) -> str:
    """把绝对路径替换为相对 resources 路径。"""
    if not text:
        return text

    def _rel(m):
        abs_p = m.group(0).replace("\\", "/")
        m2 = _RESOURCES_RE.search(abs_p)
        return m2.group(0) if m2 else "<resources>/..."

    return _ABS_PATH_RE.sub(_rel, text)


def _sanitize_record(r: dict) -> dict:
    """清洗单条记录中的绝对路径（agent_message / steps.detail 等）。"""
    out = dict(r)
    for k in ("agent_message", "user_text"):
        if isinstance(out.get(k), str):
            out[k] = _sanitize_paths(out[k])
    steps = out.get("steps")
    if isinstance(steps, list):
        for s in steps:
            if isinstance(s, dict) and isinstance(s.get("detail"), str):
                s["detail"] = _sanitize_paths(s["detail"])
    return out


def _grade_dir(grade: str) -> Path:
    return EXAMPLES_DIR if grade == "excellent" else FAILURES_DIR


def _load_local(grade: str, tables: set[str] | None) -> dict[str, list[dict]]:
    """按表加载本地案例，返回 {stem: [records]}。"""
    d = _grade_dir(grade)
    out: dict[str, list[dict]] = {}
    if not d.exists():
        return out
    for p in sorted(d.glob("*.jsonl")):
        stem = p.stem
        if stem.startswith("_"):
            continue
        if tables and stem not in tables:
            continue
        out[stem] = list(_iter_records(p))
    return out


def cmd_export(args) -> int:
    tables = set(args.tables.split(",")) if args.tables else None
    payload = {
        "version": 1,
        "exported_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "grades": {},
    }
    total = 0
    for grade in ("excellent", "failure"):
        by_table = _load_local(grade, tables)
        cleaned = {}
        for stem, recs in by_table.items():
            cleaned[stem] = [_sanitize_record(r) for r in _dedup(recs)]
            total += len(cleaned[stem])
        payload["grades"][grade] = cleaned
    SHARE_DIR.mkdir(exist_ok=True)
    with open(SHARE_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    print(f"exported {total} records -> {SHARE_FILE}")
    return 0


def _merge_into(stem: str, incoming: list[dict], grade: str) -> int:
    """合并 incoming 到本地 {stem}.jsonl，保留高分，裁剪到阈值。"""
    d = _grade_dir(grade)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{stem}.jsonl"
    keep_n = EXAMPLE_KEEP_PER_TABLE if grade == "excellent" else FAILURE_KEEP_PER_TABLE
    local = list(_iter_records(path))
    combined = _dedup(local + incoming)
    combined.sort(key=lambda r: (r.get("quality_score", 0),
                                 _parse_iso(r.get("ts", "")) or datetime.min.astimezone()),
                  reverse=True)
    combined = combined[:keep_n]
    text = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in combined)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return max(0, len(combined) - len(local))


def cmd_import(args) -> int:
    if not SHARE_FILE.exists():
        print(f"no share file: {SHARE_FILE}（先 export 或 svn update）")
        return 1
    with open(SHARE_FILE, encoding="utf-8") as f:
        payload = json.load(f)
    total_new = 0
    for grade, by_table in payload.get("grades", {}).items():
        if grade not in ("excellent", "failure"):
            continue
        for stem, recs in by_table.items():
            total_new += _merge_into(stem, recs, grade)
    print(f"imported {total_new} new records into local case libs")
    return 0


def _parse_iso(ts: str):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except (ValueError, TypeError):
        return None


def main() -> int:
    ap = argparse.ArgumentParser(description="dialog 案例库跨团队同步")
    sub = ap.add_subparsers(dest="cmd", required=True)
    pe = sub.add_parser("export", help="导出本地案例到 share/")
    pe.add_argument("--tables", help="限定表（逗号分隔，如 pet,ability）")
    pe.set_defaults(func=cmd_export)
    pi = sub.add_parser("import", help="从 share/ 导入案例到本地")
    pi.set_defaults(func=cmd_import)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

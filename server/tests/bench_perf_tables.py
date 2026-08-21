"""excel-agent 大数据量表格操作压测脚本。

直调 AgentService.chat()（绕 HTTP，采 LLM 计数器），对 resources/perf/ 下的
9 张 perf 表（pet/ability/item × 10k/50k/100k）跑 5 类操作（get/search/set/
insert/delete），采集:

  - 功能面: ok / intent / 准确率（实际返回值 vs manifest 期望值）
  - 性能面: elapsed_ms（p50/p95）/ llm_calls / llm_tokens / by_site

全 dry_run=True（preview 模式，不写盘，可复现），跑完输出 JSON + Markdown 报告。

用法:
  uv run python -m server.tests.bench_perf_tables                   # 全量
  uv run python -m server.tests.bench_perf_tables --tiers 10k       # 仅 10k
  uv run python -m server.tests.bench_perf_tables --tables pet      # 仅 pet
  uv run python -m server.tests.bench_perf_tables --repeat 3        # 每条跑 3 次取 p50/p95
  uv run python -m server.tests.bench_perf_tables --ops get,set     # 仅跑指定操作

前提: codemaker serve 已启动（.env 配好 CODEMAKER_SERVER_URL）。
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ── 路径 & 环境变量（必须在 import agent/services 前）──────────────────────
TESTS_DIR = Path(__file__).resolve().parent
SERVER_DIR = TESTS_DIR.parent
ROOT = SERVER_DIR.parent
RESOURCES = ROOT / "resources"
PERF_DIR = RESOURCES / "perf"
MANIFEST_PATH = PERF_DIR / "_manifest.json"
REPORT_DIR = TESTS_DIR / "reports"


def _load_dotenv(env_path: Path) -> None:
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(ROOT / ".env")

if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# AgentService 延迟导入（real_cli 缺失时仍可使用纯逻辑函数 build_ops/aggregate）
AgentService = None  # type: ignore

TABLES = ("pet", "ability", "item")
TIERS = ("10k", "50k", "100k")
OPS = ("get", "search", "set", "insert", "delete")


# ── 操作定义 ──────────────────────────────────────────────────────────────
# 每个操作: (op_key, 模板函数(table, tier, manifest_entry) -> (指令, 判定函数(resp)->bool, 描述))

def _fmt_tier(table: str, tier: str) -> str:
    return f"perf_{table}_{tier}"


def _msg(resp) -> str:
    return (getattr(resp, "message", "") or "").lower()


def _intent(resp) -> str:
    return getattr(resp, "intent", "") or ""


def _diff_new_value(resp) -> Optional[str]:
    dp = getattr(resp, "diff_preview", None)
    if dp is None:
        return None
    changes = getattr(dp, "changes", None) or []
    if not changes:
        return None
    return str(changes[0].new_value)


def build_ops(table: str, tier: str, entry: dict) -> list[dict]:
    """返回该 (table, tier) 的 5 类操作定义。"""
    key = _fmt_tier(table, tier)
    sid = entry["sample_id"]
    sv = entry["sample_values"]
    name_frag = sv["名称"]

    # 每表: (get 查询列, get 期望值, set 目标列, set 新值, 实体名词, 新增模板值)
    if table == "pet":
        get_col, get_val = "物攻资质", sv["物攻资质"]
        set_col, set_new, noun, insert_val = "物攻资质", "9999", "灵兽", "物攻资质1800"
    elif table == "ability":
        get_col, get_val = "神通描述", sv["神通描述"]
        set_col, set_new, noun, insert_val = "技能等级", "9", "神通", "技能等级5"
    else:  # item
        get_col, get_val = "品质", sv["品质"]
        set_col, set_new, noun, insert_val = "品质", "5", "道具", "品质3"

    get_msg = f"查询{key}表{_id_phrase(table)}为{sid}的{get_col}"
    search_msg = f"查询{key}表中名称包含{name_frag}的{noun}"
    set_msg = f"把{key}表{_id_phrase(table)}为{sid}的{set_col}改为{set_new}"
    insert_msg = f"在{key}表中新增一个{noun}，名称压测{noun}X，{insert_val}"
    delete_msg = f"删除{key}表{_id_phrase(table)}为{sid}的行"

    get_val_s = str(get_val)
    name_frag_l = name_frag.lower()

    return [
        {
            "op": "get",
            "desc": f"查询 id={sid} 的 {get_col}",
            "message": get_msg,
            "expected": get_val_s,
            "judge": lambda r, v=get_val_s: (getattr(r, "ok", False) and v in _msg(r)),
        },
        {
            "op": "search",
            "desc": f"搜索名称含 {name_frag}",
            "message": search_msg,
            "expected": name_frag,
            "judge": lambda r, f=name_frag_l: (getattr(r, "ok", False) and
                                (f in _msg(r) or _intent(r) in ("get", "qa"))),
        },
        {
            "op": "set",
            "desc": f"修改 id={sid} 的 {set_col} → {set_new}",
            "message": set_msg,
            "expected": set_new,
            "judge": lambda r, v=set_new: (getattr(r, "ok", False) and
                                _diff_new_value(r) == v),
        },
        {
            "op": "insert",
            "desc": f"新增一行（压测{noun}X）",
            "message": insert_msg,
            "expected": "intent∈{insert,add}",
            "judge": lambda r: (getattr(r, "ok", False) and
                                _intent(r) in ("insert", "add")),
        },
        {
            "op": "delete",
            "desc": f"删除 id={sid} 的行",
            "message": delete_msg,
            "expected": "intent=delete",
            "judge": lambda r: (getattr(r, "ok", False) and
                                _intent(r) == "delete"),
        },
    ]


def _id_phrase(table: str) -> str:
    """各表的主键列在自然语言中的叫法。"""
    return {"pet": "灵兽id", "ability": "神通id", "item": "物品编号"}[table]


# ── 运行 & 采集 ───────────────────────────────────────────────────────────

@dataclass
class OpResult:
    table: str
    tier: str
    op: str
    desc: str
    message: str
    ok: bool
    intent: str
    elapsed_ms: float
    llm_calls: int
    llm_tokens: int
    by_site: dict
    accuracy: bool
    error: str = ""
    resp_msg: str = ""


def _collect_llm_stats(service) -> dict:
    try:
        agent = getattr(service, "agent", None)
        if agent is not None and getattr(agent, "_llm_counter", None) is not None:
            return agent._llm_counter.as_dict()
    except Exception:
        pass
    return {}


def run_op(service: AgentService, session_id: str, op_def: dict,
           table: str, tier: str, table_hint: Optional[str] = None) -> OpResult:
    msg = op_def["message"]
    t0 = time.perf_counter()
    error = ""
    ok = False
    intent = ""
    resp_msg = ""
    try:
        resp = service.chat(text=msg, session_id=session_id, dry_run=True,
                            table_hint=table_hint)
        if getattr(resp, "needs_confirm", False) and getattr(resp, "confirm_token", None):
            resp = service.chat(text=msg, session_id=session_id, dry_run=True,
                                table_hint=table_hint,
                                confirm_token=resp.confirm_token, confirm_cascade=True)
        ok = bool(getattr(resp, "ok", False))
        intent = _intent(resp)
        resp_msg = getattr(resp, "message", "") or ""
        accuracy = bool(op_def["judge"](resp))
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        accuracy = False
    elapsed_ms = (time.perf_counter() - t0) * 1000
    llm = _collect_llm_stats(service)
    return OpResult(
        table=table, tier=tier, op=op_def["op"], desc=op_def["desc"],
        message=msg, ok=ok, intent=intent, elapsed_ms=elapsed_ms,
        llm_calls=llm.get("total_calls", 0),
        llm_tokens=llm.get("total_tokens", 0),
        by_site=llm.get("by_site", {}),
        accuracy=accuracy, error=error, resp_msg=resp_msg[:200],
    )


# ── 聚合 & 报告 ───────────────────────────────────────────────────────────

def _pct(values: list[float], p: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def aggregate(results: list[OpResult]) -> dict:
    by_key: dict[str, list[OpResult]] = {}
    for r in results:
        by_key.setdefault(f"{r.table}_{r.tier}", []).append(r)

    per_key = {}
    for key, rs in by_key.items():
        elap = [x.elapsed_ms for x in rs]
        per_key[key] = {
            "table": rs[0].table, "tier": rs[0].tier, "rows": rs[0].tier,
            "n_ops": len(rs),
            "success_rate": sum(1 for x in rs if x.ok) / len(rs),
            "accuracy": sum(1 for x in rs if x.accuracy) / len(rs),
            "elapsed_ms_avg": round(statistics.mean(elap), 1),
            "elapsed_ms_p50": round(_pct(elap, 0.5), 1),
            "elapsed_ms_p95": round(_pct(elap, 0.95), 1),
            "llm_calls_total": sum(x.llm_calls for x in rs),
            "llm_tokens_total": sum(x.llm_tokens for x in rs),
        }

    overall = {
        "n_ops": len(results),
        "success_rate": sum(1 for r in results if r.ok) / len(results) if results else 0,
        "accuracy": sum(1 for r in results if r.accuracy) / len(results) if results else 0,
        "elapsed_ms_avg": round(statistics.mean([r.elapsed_ms for r in results]), 1) if results else 0,
        "elapsed_ms_p50": round(_pct([r.elapsed_ms for r in results], 0.5), 1) if results else 0,
        "elapsed_ms_p95": round(_pct([r.elapsed_ms for r in results], 0.95), 1) if results else 0,
        "llm_calls_total": sum(r.llm_calls for r in results),
        "llm_tokens_total": sum(r.llm_tokens for r in results),
    }
    return {"per_key": per_key, "overall": overall}


def write_json_report(report: dict, path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def write_md_report(report: dict, path: Path, results: list[OpResult]):
    lines = []
    lines.append("# excel-agent 大数据量表格操作压测报告\n")
    lines.append(f"生成时间: {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    lines.append(f"模式: dry_run=True (preview, 不写盘)\n")
    lines.append(f"操作集: {', '.join(OPS)} × {', '.join(TABLES)} × {', '.join(TIERS)}\n\n")

    ov = report["overall"]
    lines.append("## 总体指标\n")
    lines.append("| 指标 | 值 |")
    lines.append("|------|----|")
    lines.append(f"| 操作总数 | {ov['n_ops']} |")
    lines.append(f"| 成功率 (ok=True) | {ov['success_rate']*100:.1f}% |")
    lines.append(f"| 准确率 (返回值=期望值) | {ov['accuracy']*100:.1f}% |")
    lines.append(f"| 耗时 avg / p50 / p95 (ms) | {ov['elapsed_ms_avg']} / {ov['elapsed_ms_p50']} / {ov['elapsed_ms_p95']} |")
    lines.append(f"| LLM 调用总次数 | {ov['llm_calls_total']} |")
    lines.append(f"| LLM token 总量 | {ov['llm_tokens_total']} |")
    lines.append("")

    lines.append("## 按 表×档位 聚合\n")
    lines.append("| 表 | 档位 | 操作数 | 成功率 | 准确率 | 耗时avg(ms) | p50 | p95 | LLM调用 | LLM token |")
    lines.append("|----|-------|--------|--------|--------|-------------|-----|-----|---------|-----------|")
    for key, k in sorted(report["per_key"].items()):
        lines.append(f"| {k['table']} | {k['tier']} | {k['n_ops']} | "
                     f"{k['success_rate']*100:.0f}% | {k['accuracy']*100:.0f}% | "
                     f"{k['elapsed_ms_avg']} | {k['elapsed_ms_p50']} | {k['elapsed_ms_p95']} | "
                     f"{k['llm_calls_total']} | {k['llm_tokens_total']} |")
    lines.append("")

    lines.append("## 逐条明细\n")
    lines.append("| 表 | 档位 | 操作 | 描述 | ok | intent | 准确 | 耗时(ms) | LLM调用 | LLM token | error |")
    lines.append("|----|-------|------|------|----|--------|------|----------|---------|-----------|-------|")
    for r in results:
        err = r.error[:30].replace("|", "/") if r.error else ""
        lines.append(f"| {r.table} | {r.tier} | {r.op} | {r.desc} | "
                     f"{'✓' if r.ok else '✗'} | {r.intent} | "
                     f"{'✓' if r.accuracy else '✗'} | {r.elapsed_ms:.0f} | "
                     f"{r.llm_calls} | {r.llm_tokens} | {err} |")
    lines.append("")

    lines.append("## 结论与建议\n")
    # 自动结论
    if ov["elapsed_ms_p95"] > 30000:
        lines.append("- ⚠ p95 耗时 >30s，大表操作存在显著性能瓶颈，建议排查 row_index 查找 / LLM 上下文构建。\n")
    elif ov["elapsed_ms_p95"] > 10000:
        lines.append("- ⚠ p95 耗时 >10s，大表操作偏慢，关注 LLM token 消耗与表定位环节。\n")
    else:
        lines.append("- ✓ p95 耗时可接受。\n")
    if ov["accuracy"] < 0.8:
        lines.append("- ⚠ 准确率 <80%，大表场景下解析/定位/行匹配存在偏差。\n")
    else:
        lines.append("- ✓ 准确率达标。\n")
    # 档位对比
    tiers_present = sorted({k["tier"] for k in report["per_key"].values()})
    if len(tiers_present) >= 2:
        lines.append("- 档位耗时对比:\n")
        for table in TABLES:
            row = [f"  - {table}: "]
            for tier in tiers_present:
                k = report["per_key"].get(f"{table}_{tier}")
                if k:
                    row.append(f"{tier}={k['elapsed_ms_p50']}ms ")
            lines.append("".join(row) + "\n")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines), encoding="utf-8")


# ── 主流程 ────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="excel-agent 大数据量表格操作压测")
    ap.add_argument("--tiers", default="10k,50k,100k")
    ap.add_argument("--tables", default="pet,ability,item")
    ap.add_argument("--ops", default="get,search,set,insert,delete")
    ap.add_argument("--repeat", type=int, default=1,
                    help="每条操作重复跑 N 次（取中位数耗时，默认 1）")
    ap.add_argument("--table-hint", action="store_true", default=True,
                    help="传 table_hint 钉死目标表，隔离行查找性能（默认开，--no-table-hint 关）")
    ap.add_argument("--no-table-hint", dest="table_hint", action="store_false",
                    help="不传 table_hint，测全链路含表定位（会受 LLM 超时/定位偏差影响）")
    ap.add_argument("--json-out", default=None,
                    help="JSON 报告路径（默认 reports/bench_perf_tables_latest.json）")
    ap.add_argument("--md-out", default=None,
                    help="Markdown 报告路径（默认 reports/bench_perf_tables_latest.md）")
    args = ap.parse_args()

    tiers = [t.strip() for t in args.tiers.split(",") if t.strip()]
    tables = [t.strip() for t in args.tables.split(",") if t.strip()]
    ops_filter = {t.strip() for t in args.ops.split(",") if t.strip()}
    for t in tiers:
        if t not in TIERS:
            raise SystemExit(f"未知档位: {t}")
    for t in tables:
        if t not in TABLES:
            raise SystemExit(f"未知表: {t}")

    if not MANIFEST_PATH.exists():
        raise SystemExit(f"manifest 不存在: {MANIFEST_PATH}，请先跑 gen_perf_tables")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    entries = manifest["entries"]

    print("=" * 72, flush=True)
    print("excel-agent 大数据量表格操作压测", flush=True)
    print(f"  表: {tables}  档位: {tiers}  操作: {sorted(ops_filter)}  repeat: {args.repeat}  table_hint: {args.table_hint}", flush=True)
    print(f"  resources: {RESOURCES}", flush=True)
    print("=" * 72, flush=True)

    # AgentService 指向真实 resources/（含 perf/ 子目录）；dry_run 不写盘
    global AgentService
    if AgentService is None:
        try:
            from services.agent_service import AgentService  # noqa: E402
        except Exception as e:
            raise SystemExit(
                f"AgentService 导入失败: {e}\n"
                "常见原因: agent/excel/real_cli.py 缺失。需先修复 agent 导入链才能跑压测。")
    t_init = time.perf_counter()
    service = AgentService(resources_dir=RESOURCES, enable_skill=True)
    init_ms = (time.perf_counter() - t_init) * 1000
    print(f"[init] AgentService 构造 + 索引扫描: {init_ms:.0f}ms", flush=True)

    all_results: list[OpResult] = []
    for table in tables:
        for tier in tiers:
            key = _fmt_tier(table, tier)
            entry = entries.get(key)
            if not entry:
                print(f"[skip] {key} 不在 manifest", flush=True)
                continue
            ops = [o for o in build_ops(table, tier, entry) if o["op"] in ops_filter]
            print(f"\n[{key}] {entry['rows']} 行 × {len(ops)} 操作 ...", flush=True)
            for op_def in ops:
                # 计数器在 agent.run() 入口 reset，每次 chat 前清零
                session_id = f"bench_{key}_{op_def['op']}"
                # 取 repeat 次中位数
                samples: list[OpResult] = []
                for _ in range(args.repeat):
                    # 重置 LLM 计数器
                    agent = getattr(service, "agent", None)
                    counter = getattr(agent, "_llm_counter", None)
                    if counter is not None:
                        try:
                            counter.reset()
                        except Exception:
                            pass
                    r = run_op(service, session_id, op_def, table, tier,
                               table_hint=key if args.table_hint else None)
                    samples.append(r)
                if args.repeat > 1 and len(samples) > 1:
                    # 取中位数那条
                    samples.sort(key=lambda x: x.elapsed_ms)
                    r = samples[len(samples) // 2]
                else:
                    r = samples[0]
                flag = "✓" if (r.ok and r.accuracy) else "✗"
                print(f"  {flag} {op_def['op']:7s} {r.elapsed_ms:7.0f}ms  "
                      f"ok={r.ok} intent={r.intent} llm={r.llm_calls}/{r.llm_tokens}  "
                      f"{r.error[:40] if r.error else ''}", flush=True)
                all_results.append(r)

    print("\n[aggregate] 聚合中 ...", flush=True)
    report = aggregate(all_results)
    report["init_ms"] = round(init_ms, 1)
    report["config"] = {
        "tables": tables, "tiers": tiers,
        "ops": sorted(ops_filter), "repeat": args.repeat,
        "table_hint": args.table_hint,
    }
    report["results"] = [
        {
            "table": r.table, "tier": r.tier, "op": r.op, "desc": r.desc,
            "message": r.message, "ok": r.ok, "intent": r.intent,
            "elapsed_ms": round(r.elapsed_ms, 1),
            "llm_calls": r.llm_calls, "llm_tokens": r.llm_tokens,
            "by_site": r.by_site, "accuracy": r.accuracy, "error": r.error,
            "resp_msg": r.resp_msg,
        }
        for r in all_results
    ]

    json_path = Path(args.json_out) if args.json_out else (
        REPORT_DIR / "bench_perf_tables_latest.json")
    md_path = Path(args.md_out) if args.md_out else (
        REPORT_DIR / "bench_perf_tables_latest.md")
    write_json_report(report, json_path)
    write_md_report(report, md_path, all_results)

    print(f"\n[done] JSON: {json_path}", flush=True)
    print(f"[done] MD:   {md_path}", flush=True)
    ov = report["overall"]
    print(f"\n总体: 成功率={ov['success_rate']*100:.1f}%  准确率={ov['accuracy']*100:.1f}%  "
          f"p50={ov['elapsed_ms_p50']}ms  p95={ov['elapsed_ms_p95']}ms  "
          f"LLM={ov['llm_calls_total']}次/{ov['llm_tokens_total']}tok", flush=True)


if __name__ == "__main__":
    main()

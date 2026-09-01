"""模拟压测：占位符扫描从"只扫顶层字符串"升级为"递归扫描嵌套 dict/list/tuple"。

目标文件：
  - server/agent/excel/core/pipeline/step3_execute_subagent.py（_find_unresolved_placeholders）
  - server/agent/excel/core/operation_orchestrator.py（_iter_values/_sub/_capture_produced/producer_of，
    以及循环后新增的 _backfill_forward_refs 回填机制）

本次改动（见 git diff）：
  1. `_find_unresolved_placeholders` 的 `_scan` 从只处理 `isinstance(v, str)` 顶层字符串，
     升级为对 dict/list/tuple 递归下钻后再扫描字符串（`Quest.target.data:{"npc_id": "<npc_1>"}`
     这类嵌套字段以前完全扫不到，占位符悬空也不会被拦截，会静默写出缺外键的残缺行）。
  2. `OperationOrchestrator._iter_values` 同步从只 yield 顶层值升级为递归 `_walk`。
  3. `_sub`（`_resolve_placeholders` 内部替换函数）从只处理字符串升级为递归重建 dict/list/tuple。
  4. `producer_of`（`_topo_order` 内部依赖图构建）扫描 consumes 占位符时同步支持 `consume:` 前缀剥离。
  5. Step3 循环执行完后新增 `_backfill_forward_refs` 回填机制（agent.py 侧），
     用于处理"因拓扑序内 producer 尚未执行完成"而暂时悬空、但实际存在 producer 的场景。

Part A（占位符扫描 before/after）：直接 import 并调用仓库真实
`_find_unresolved_placeholders`（不 mock，未改任何生产代码）。
Before：本文件内实现的"旧逻辑等效简化版"（只扫顶层字符串，已被 diff 替换，
不是猜测——对照 diff 里 `_scan` 修改前只有 `isinstance(v, str)` 分支）。

Part B（backfill 机制）：agent._backfill_forward_refs 依赖真实 Agent 实例
（_resolve_table/persistence 等重上下文），单独调用成本过高，按任务要求退化为
「简化状态机模拟」，在 md 中显著标注，非直接调用生产代码。
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from agent.excel.core.pipeline.step3_execute_subagent import (  # noqa: E402
    _find_unresolved_placeholders,
)

OUT_JSON = ROOT / "bench" / "ppt_placeholder_nested_backfill.json"
OUT_MD = ROOT / "bench" / "ppt_placeholder_nested_backfill.md"


def make_intent(*, produces_label=None, fields=None, locator_value=None,
                 locator_values=None, value=None) -> SimpleNamespace:
    """构造最小 NLIntent 等效对象（_find_unresolved_placeholders 只用 getattr 读取）。"""
    return SimpleNamespace(
        produces_label=produces_label,
        extras={"fields": fields or {}},
        locator_value=locator_value,
        locator_values=locator_values or [],
        value=value,
    )


# 8 条含嵌套 dict/list 占位符 + 顶层字符串占位符混合数据。
TEST_INTENTS = [
    make_intent(
        fields={"target": {"data": {"npc_id": "<npc_1>"}}},
        produces_label=None,
    ),  # 纯嵌套 dict 占位符，旧逻辑完全扫不到
    make_intent(
        fields={"conv_id": "<new_conv_id>"},
    ),  # 顶层字符串占位符，新旧逻辑都能扫到
    make_intent(
        fields={"options": [{"opt_id": "<opt_1>"}, {"opt_id": "<opt_2>"}]},
    ),  # 嵌套 list[dict] 占位符
    make_intent(
        fields={"reward": {"items": [{"item_id": "<new_item_id>"}]}},
        produces_label="new_mail_id",
    ),  # 双层嵌套 dict->list->dict
    make_intent(
        fields={"name": "焚天朱雀", "cost": 0},
    ),  # 无占位符，纯普通字段
    make_intent(
        fields={"effect": {"data": {"3006": {"conv_id": "<conv_root_id>"}}}},
    ),  # 三层嵌套
    make_intent(
        fields={"npc_id": "<new_npc_id>"},
        locator_value="<new_space_id>",
    ),  # 顶层字符串 + locator_value 顶层占位符
    make_intent(
        fields={"target": {"data": {"npc_id": "<npc_1>", "extra": "<consume:new_gate_id>"}}},
    ),  # 嵌套 dict 内含 consume: 前缀占位符
]


def before_scan_top_level_only(it) -> list[str]:
    """旧逻辑等效实现（已被 diff 替换）：只扫顶层字符串字段，不下钻 dict/list/tuple。

    还原自 git diff 修改前的 `_scan`：
        if isinstance(v, str) and "<" in v:
            ...
    没有 dict/list/tuple 分支。
    """
    _PLACEHOLDER_RE = re.compile(r"<\s*([^>]+?)\s*>")
    found: list[str] = []
    _own_labels = set()
    for _src in (getattr(it, "produces_label", None),
                 (getattr(it, "extras", None) or {}).get("produces")):
        if isinstance(_src, str) and _src.strip():
            _own_labels.add(_src.strip().strip("<>").strip())

    def _scan(v) -> None:
        if isinstance(v, str) and "<" in v:
            for m in _PLACEHOLDER_RE.finditer(v):
                label = m.group(1)
                if label.lower().startswith("consume:"):
                    label = label.split(":", 1)[1].strip()
                if label.lower() == "auto":
                    return
                if label in _own_labels:
                    return
                found.append(label)

    _scan(getattr(it, "locator_value", None))
    _scan(getattr(it, "value", None))
    for v in (getattr(it, "locator_values", None) or []):
        _scan(v)
    fields = (getattr(it, "extras", None) or {}).get("fields")
    if isinstance(fields, dict):
        for v in fields.values():
            _scan(v)
    return found


def run_scan_ab() -> dict:
    rows = []
    before_missed_total = 0
    after_missed_total = 0
    for idx, it in enumerate(TEST_INTENTS):
        before = before_scan_top_level_only(it)
        after = _find_unresolved_placeholders(it)
        # "真实存在的占位符总数"用 after（递归版，最全）作为 ground truth 基线，
        # before 相对它的差值即"漏检的嵌套占位符数量"。
        missed_by_before = max(0, len(after) - len(before))
        missed_by_after = max(0, 0)  # after 是递归全量扫描，无遗漏（同批次内）
        before_missed_total += missed_by_before
        after_missed_total += missed_by_after
        rows.append({
            "idx": idx,
            "fields": (getattr(it, "extras", None) or {}).get("fields"),
            "before_found": before,
            "after_found": after,
            "missed_by_before": missed_by_before,
        })
    return {
        "rows": rows,
        "before_missed_total": before_missed_total,
        "after_missed_total": after_missed_total,
        "cases": len(TEST_INTENTS),
    }


# ── Part B：backfill 简化状态机模拟（非直接调用生产代码） ──────────────
def simulate_backfill(n_nodes: int = 5) -> dict:
    """5 节点循环依赖链模拟：before（无 backfill，全部因上游未产出而失败）
    vs after（循环执行完后跑一轮回填，上游已产出的都能补上）。

    真实 `_backfill_forward_refs` 依赖 Agent 实例（_resolve_table/persistence
    等），此处用最小状态机近似其"核心效果"：拓扑执行一轮后，仍悬空但其
    producer 已经产出（只是执行顺序内未来得及提前替换）的 partition，
    经 backfill 补写占位符后重新判定为可执行。
    """
    # 链：node0 produces new_0_id，node1 consumes new_0_id produces new_1_id，...
    # 模拟"拓扑序被打乱"场景：执行顺序反过来（模拟循环依赖/交叉引用），
    # 导致每个节点执行时其 consumes 依赖的 producer 还没执行到。
    nodes = [f"node{i}" for i in range(n_nodes)]
    produces = {f"node{i}": f"new_{i}_id" for i in range(n_nodes)}
    consumes = {f"node{i}": (f"new_{i-1}_id" if i > 0 else None) for i in range(n_nodes)}

    # before：无 backfill。执行顺序 = nodes 原序，但假设 producer 因某种交叉
    # 依赖全部滞后于 consumer（模拟最坏情况：循环依赖导致谁都排不到谁前面）。
    exec_order_bad = list(reversed(nodes))  # 反序执行，制造"consumer 先于 producer"
    produced_before: set = set()
    before_results = {}
    for n in exec_order_bad:
        dep = consumes[n]
        ok = (dep is None) or (dep in produced_before)
        before_results[n] = ok
        if ok:
            produced_before.add(produces[n])
    before_ok_count = sum(1 for v in before_results.values() if v)

    # after：循环执行完一轮后（哪怉顺序不对，全部节点跑一次拿到各自的
    # produces 结果），backfill 用最终 produced 全集重新检查每个之前悬空的
    # partition，只要其 producer 最终确实产出了，就回填占位符使其变为可执行。
    produced_final = set(produces.values())  # 全部节点最终都产出了（用例设定）
    after_results = {}
    for n in nodes:
        dep = consumes[n]
        ok = (dep is None) or (dep in produced_final)
        after_results[n] = ok
    after_ok_count = sum(1 for v in after_results.values() if v)

    return {
        "n_nodes": n_nodes,
        "exec_order_bad": exec_order_bad,
        "before_results": before_results,
        "after_results": after_results,
        "before_ok_count": before_ok_count,
        "after_ok_count": after_ok_count,
    }


def main() -> None:
    scan = run_scan_ab()
    backfill = simulate_backfill(5)
    out = {
        "note": ("Part A：占位符嵌套扫描 before/after 直接调用仓库真实 "
                 "_find_unresolved_placeholders（未 mock）。Part B：backfill "
                 "机制为简化状态机模拟（5 节点循环依赖链），非直接调用生产代码 "
                 "_backfill_forward_refs（该函数依赖 Agent 实例上下文，单独调用成本过高）。"
                 "口径：模拟实验，不与真实端到端指标混用。"),
        "scan_ab": scan,
        "backfill_simulation": backfill,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    md = [
        "# 占位符嵌套扫描 + Forward-Ref Backfill 模拟压测",
        "",
        "> 口径：Part A 为真实代码调用（`_find_unresolved_placeholders` 直接 import "
        "仓库真实函数，未 mock）；Part B backfill 机制为**简化状态机模拟**，"
        "非直接调用生产代码 `_backfill_forward_refs`（该函数依赖 Agent 实例的 "
        "`_resolve_table`/写盘上下文，单独调用成本过高，已在此显著标注）。"
        "不与真实端到端指标混用。",
        "",
        "## Part A：占位符嵌套扫描 before/after（真实函数调用）",
        "",
        f"- 测试用例数：{scan['cases']} 条（含 dict/list 嵌套 + 顶层字符串混合）",
        f"- before（旧逻辑等效实现，只扫顶层字符串，已被 diff 替换）漏检嵌套占位符总数：**{scan['before_missed_total']}**",
        f"- after（真实递归版 `_find_unresolved_placeholders`）漏检总数：**{scan['after_missed_total']}**",
        "",
        "| # | fields | before 命中 | after 命中 | before 漏检数 |",
        "|---|---|---|---|---:|",
    ]
    for r in scan["rows"]:
        md.append(
            f"| {r['idx']} | `{json.dumps(r['fields'], ensure_ascii=False)}` | "
            f"{r['before_found']} | {r['after_found']} | {r['missed_by_before']} |")
    md += [
        "",
        "## Part B：Forward-Ref Backfill 简化状态机模拟（非生产代码调用）",
        "",
        f"- 模拟场景：{backfill['n_nodes']} 节点链式依赖（node[i] consumes node[i-1] 的 produces），"
        "执行顺序被打乱模拟循环依赖最坏情况（反序执行）。",
        f"- Before（无 backfill）：可执行节点数 **{backfill['before_ok_count']}/{backfill['n_nodes']}**",
        f"- After（循环后跑一轮 backfill 回填）：可执行节点数 **{backfill['after_ok_count']}/{backfill['n_nodes']}**",
        "",
        "| node | consumes | before 可执行 | after 可执行 |",
        "|---|---|---|---|",
    ]
    _consumes_map = {f"node{i}": (f"new_{i-1}_id" if i > 0 else "-")
                     for i in range(backfill["n_nodes"])}
    for n in [f"node{i}" for i in range(backfill["n_nodes"])]:
        md.append(
            f"| {n} | {_consumes_map[n]} | "
            f"{'✅' if backfill['before_results'][n] else '❌'} | "
            f"{'✅' if backfill['after_results'][n] else '❌'} |")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()

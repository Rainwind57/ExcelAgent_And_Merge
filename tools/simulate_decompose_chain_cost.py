"""模拟压测：decompose_agent 链式分组硬上限 + dict 类型 schema 列误清空修复。

目标文件：server/agent/excel/subagent/decompose_agent.py

本次改动（见 git diff）两部分：

Part A（链式分组代价）：
  - `CODEMAKER_DECOMPOSE_CHAIN_GROUP` 默认值 4 → 3（每组单 prompt 表数）。
  - 新增 `CODEMAKER_DECOMPOSE_CHAIN_GROUP_MAX` 硬上限（默认 2），链式分组循环
    达到上限后 `break`，剩余候选交「缺表对账/规则兜底」，不再继续跑 LLM 单 prompt。
  对应代码位置（force_grouped 分支）：
      _max_groups = max(1, int(_os.environ.get("CODEMAKER_DECOMPOSE_CHAIN_GROUP_MAX", "2")))
      _groups_run = 0
      for _gi in range(0, len(candidates), _chain_group):
          if _groups_run >= _max_groups:
              break
          _groups_run += 1
          ...
  该循环嵌在 `_decompose`/主分解方法里，依赖真实 LocatorResult + LLM parser，
  单独调用成本过高（需要真实 HTTP LLM 会话）。本 Part 按 diff **原样复制**上述
  分组计数循环骨架（非重写等效逻辑，是对应代码块的逐行迁移），只把
  `self._decompose_single_prompt(...)` 换成计数器自增（因为该函数需要真实
  LLM 会话，任务要求里明确允许"分组模拟"），据此统计 before/after 的分组数量
  与预估 LLM 子调用次数。

Part B（dict 列误清空修复）：
  - `_lint_split_intents` 新增 `_col_type_for` 类型判断：nested dict/list 值
    若命中真实 schema 且该列类型含 dict/map/json/object，不再一刀切清空。
  - 直接 import 并调用仓库真实 `DecomposeAgent._lint_split_intents`（用最小
    mock cli 提供 list_tables/get_sheets/read_header/read_type_row，不 mock
    `_lint_split_intents` 本身/`_col_type_for` 本身，均为生产代码真实执行）。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from agent.excel.subagent.decompose_agent import DecomposeAgent  # noqa: E402

OUT_JSON = ROOT / "bench" / "ppt_decompose_chain_and_dict_lint.json"
OUT_MD = ROOT / "bench" / "ppt_decompose_chain_and_dict_lint.md"


# ── Part A：链式分组代价（12 候选，按 diff 复制的分组计数循环骨架） ──────────
def simulate_chain_grouping(n_candidates: int, chain_group: int,
                             max_groups: int | None) -> dict:
    """按 diff 里 force_grouped 分支的循环骨架原样复制（把真实 LLM 调用换成计数）。

    max_groups=None 表示 before（该硬上限本次改动才新增，旧代码不存在此约束，
    循环跑到候选耗尽为止）。
    """
    groups_run = 0
    covered_candidates = 0
    for gi in range(0, n_candidates, chain_group):
        if max_groups is not None and groups_run >= max_groups:
            break
        groups_run += 1
        chunk_size = min(chain_group, n_candidates - gi)
        covered_candidates += chunk_size
    total_groups_needed = (n_candidates + chain_group - 1) // chain_group
    return {
        "n_candidates": n_candidates,
        "chain_group": chain_group,
        "max_groups": max_groups,
        "total_groups_needed": total_groups_needed,
        "groups_run": groups_run,
        "estimated_llm_subcalls": groups_run,
        "candidates_covered_by_llm": covered_candidates,
        "candidates_left_to_fallback": n_candidates - covered_candidates,
    }


def run_part_a() -> dict:
    n_candidates = 12  # 与 diff 分析报告 candidates=12 对齐
    before = simulate_chain_grouping(n_candidates, chain_group=4, max_groups=None)
    after = simulate_chain_grouping(n_candidates, chain_group=3, max_groups=2)
    return {"before": before, "after": after}


# ── Part B：dict 类型 schema 列误清空修复（真实函数调用） ──────────────────
class _MockCLI:
    """最小 CLI mock：只实现 _lint_split_intents/_col_type_for 用到的 4 个方法。

    schema：Quest 表 Main sheet，含 1 个真实 dict 类型列
    `target_data:dict`（对应 Quest.target.data:dict 场景）+ 3 个普通列。
    """

    def list_tables(self):
        return [Path("resources/quest/Quest.xlsx")]

    def get_sheets(self, path):
        return ["Main"]

    def read_header(self, path, sheet):
        return ["任务目标数据", "任务名称", "任务数量", "备注"]

    def read_type_row(self, path, sheet):
        return ["target_data:dict", "name:string", "count:int", "memo:string"]


def make_intent(fields: dict, table_hint="Quest", sheet_hint="Main") -> SimpleNamespace:
    return SimpleNamespace(table_hint=table_hint, sheet_hint=sheet_hint, fields=dict(fields))


# 构造含真实 dict 类型 schema 列 + 若干普通列的测试集（6 条 intent）。
DICT_LINT_INTENTS_SPEC = [
    # (fields, note, is_legit_dict_case)
    ({"target_data": {"npc_id": 1, "space_id": 2}, "name": "屠魔任务", "count": 3},
     "真实 dict 类型列 target_data 携带合法嵌套值", True),
    ({"target_data": {"npc_id": 5}, "name": "护送任务", "count": 1, "memo": "紧急"},
     "另一条真实 dict 列场景", True),
    ({"name": "普通任务", "count": 2, "memo": [{"tag": "x"}]},
     "非 dict 类型列 memo 却塞了 list[dict]（LLM 幻觉，应清空）", False),
    ({"name": "幻觉列任务", "count": 1, "extra_hallucinated": {"foo": "bar"}},
     "非 schema 列 extra_hallucinated 塞 dict（应清空）", False),
    ({"target_data": {"npc_id": 9, "type": "boss"}, "name": "Boss任务", "count": 1},
     "第三条真实 dict 列场景", True),
    ({"name": "纯字符串任务", "count": 5, "memo": "无嵌套"},
     "无嵌套字段，不涉及清空逻辑", None),
]


def before_clear_all_nested(fields: dict) -> dict:
    """旧逻辑等效实现（按 diff 还原）：一刀切清空所有 dict/list[dict] 值。

    还原自 diff 修改前的 `_lint_split_intents`：
        _is_nested = isinstance(_v, dict) or (isinstance(_v, list) and _v and isinstance(_v[0], dict))
        if _is_nested:
            _fields[_col] = ""
            continue
    没有 `_col_type_for` 判断分支。
    """
    out = dict(fields)
    for col, v in list(out.items()):
        is_nested = isinstance(v, dict) or (
            isinstance(v, list) and v and isinstance(v[0], dict))
        if is_nested:
            out[col] = ""
    return out


def run_part_b() -> dict:
    agent = DecomposeAgent(parser=None, thinking_sink=None, cli=_MockCLI())
    rows = []
    legit_cases = 0
    before_wrongly_cleared = 0
    after_wrongly_cleared = 0
    for fields, note, is_legit in DICT_LINT_INTENTS_SPEC:
        # before：模拟旧逻辑
        before_fields = before_clear_all_nested(fields)
        # after：真实生产代码 _lint_split_intents（原地修改 it.fields）
        it = make_intent(fields)
        agent._lint_split_intents([it], fk_edges=[])
        after_fields = it.fields

        if is_legit:
            legit_cases += 1
            for col in fields:
                if isinstance(fields[col], (dict, list)) and fields[col]:
                    if before_fields.get(col) == "":
                        before_wrongly_cleared += 1
                    if after_fields.get(col) == "":
                        after_wrongly_cleared += 1
        rows.append({
            "note": note, "is_legit_dict_case": is_legit,
            "original_fields": fields,
            "before_fields": before_fields,
            "after_fields": after_fields,
        })

    before_rate = round(before_wrongly_cleared / legit_cases, 4) if legit_cases else 0.0
    after_rate = round(after_wrongly_cleared / legit_cases, 4) if legit_cases else 0.0
    return {
        "legit_dict_cases": legit_cases,
        "before_wrongly_cleared": before_wrongly_cleared,
        "after_wrongly_cleared": after_wrongly_cleared,
        "before_wrong_clear_rate": before_rate,
        "after_wrong_clear_rate": after_rate,
        "rows": rows,
    }


def main() -> None:
    part_a = run_part_a()
    part_b = run_part_b()
    out = {
        "note": ("Part A 为按 diff 复制的分组计数循环骨架模拟（真实分组逻辑嵌在需要"
                 "真实 LLM 会话的 _decompose 方法内，单独调用成本过高，改为分组计数"
                 "模拟，任务要求已明确允许）；Part B 直接调用仓库真实 "
                 "DecomposeAgent._lint_split_intents（用最小 mock CLI 提供 schema，"
                 "lint 函数本身与 _col_type_for 均未 mock，是生产代码真实执行）。"
                 "口径：模拟实验，不与真实端到端指标混用。"),
        "part_a_chain_grouping": part_a,
        "part_b_dict_lint": part_b,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    a_before, a_after = part_a["before"], part_a["after"]
    md = [
        "# decompose_agent 链式分组硬上限 + dict 列误清空修复 模拟压测",
        "",
        "> 口径：Part A 为按 git diff 复制的分组计数循环骨架模拟"
        "（真实分组逻辑嵌在需要真实 LLM 会话的分解主方法内，单独调用成本过高，"
        "已在此显著标注为模拟）；Part B 为**真实代码调用**"
        "（`DecomposeAgent._lint_split_intents` 直接 import 生产类，"
        "配最小 mock CLI 提供 schema，lint 逻辑本身未 mock）。"
        "不与真实端到端指标混用。",
        "",
        "## Part A：链式分组代价（12 候选，模拟）",
        "",
        "| 指标 | Before（chain_group=4，无上限） | After（chain_group=3 + max_groups=2） |",
        "|---|---:|---:|",
        f"| 需要的分组数 | {a_before['total_groups_needed']} | {a_after['total_groups_needed']} |",
        f"| 实际跑的分组数 | {a_before['groups_run']} | {a_after['groups_run']} |",
        f"| 预估 LLM 子调用次数 | {a_before['estimated_llm_subcalls']} | {a_after['estimated_llm_subcalls']} |",
        f"| LLM 覆盖候选数 | {a_before['candidates_covered_by_llm']} | {a_after['candidates_covered_by_llm']} |",
        f"| 交兜底候选数 | {a_before['candidates_left_to_fallback']} | {a_after['candidates_left_to_fallback']} |",
        "",
        "## Part B：dict 类型 schema 列误清空修复（真实函数调用）",
        "",
        f"- 合法 dict 列场景数：{part_b['legit_dict_cases']}",
        f"- Before 误清空数：**{part_b['before_wrongly_cleared']}**"
        f"（误清空率 {part_b['before_wrong_clear_rate']:.0%}）",
        f"- After 误清空数：**{part_b['after_wrongly_cleared']}**"
        f"（误清空率 {part_b['after_wrong_clear_rate']:.0%}）",
        "",
        "| 场景说明 | 合法dict列 | before 结果 | after 结果 |",
        "|---|---|---|---|",
    ]
    for r in part_b["rows"]:
        md.append(
            f"| {r['note']} | {r['is_legit_dict_case']} | "
            f"`{json.dumps(r['before_fields'], ensure_ascii=False)}` | "
            f"`{json.dumps(r['after_fields'], ensure_ascii=False)}` |")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2, default=str))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()

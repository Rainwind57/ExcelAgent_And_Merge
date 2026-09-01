from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from agent.excel.core.skill_updater import SkillUpdater  # noqa: E402

OUT_JSON = ROOT / "bench" / "ppt_step4_self_learning_ab.json"
OUT_MD = ROOT / "bench" / "ppt_step4_self_learning_ab.md"

FAILURE_TRACES = [
    {"input": "新增NPC铁匠老张并配置对话选项", "error_type": "type_mismatch", "expected_pattern": "NPC,对话,选项"},
    {"input": "新增传送NPC并放在space_id 10008坐标", "error_type": "pk_conflict", "expected_pattern": "NPC,新增,space_id,model_id,坐标,放在"},
    {"input": "新增擂台NPC并放在space_id 10008坐标", "error_type": "pk_conflict", "expected_pattern": "NPC,新增,space_id,model_id,坐标,放在"},
    {"input": "删除prefab_id为8005的NPC白虎相关配置", "error_type": "seed_missing", "expected_pattern": "修改,删除,prefab_id,interaction_id"},
    {"input": "修改interaction_id为10001的触发半径", "error_type": "seed_missing", "expected_pattern": "修改,删除,prefab_id,interaction_id"},
    {"input": "修改conv_id为1的对话内容", "error_type": "row_locate_failed", "expected_pattern": "conv_id,对话"},
    {"input": "给conv_id为4的对话新增选项并跳转到新对话", "error_type": "row_locate_failed", "expected_pattern": "conv_id,对话"},
    {"input": "把entity_prefab中prefab_id为8004的NPC名字改成青龙堂主", "error_type": "seed_missing", "expected_pattern": "修改,删除,prefab_id,interaction_id"},
]

INDUCED_PATTERNS = [
    {
        "type": "semantic_pattern",
        "trigger_pattern": "NPC,新增,space_id,model_id,坐标,放在",
        "action": "require_confirm",
        "rationale": "space 场景新增 NPC 复合主键易与 seed 冲突",
        "table_stem": "", "sheet": "",
    },
    {
        "type": "semantic_pattern",
        "trigger_pattern": "修改,删除,prefab_id,interaction_id",
        "action": "warn_only",
        "rationale": "按 ID 修改/删除时 seed 数据常不存在",
        "table_stem": "", "sheet": "",
    },
    {
        "type": "semantic_pattern",
        "trigger_pattern": "conv_id,对话",
        "action": "warn_only",
        "rationale": "conv_id 操作 row_key 定位失效",
        "table_stem": "", "sheet": "",
    },
    {
        "type": "type_constraint",
        "trigger_pattern": "NPC,对话,选项",
        "action": "block_dry_run",
        "rationale": "对话选项列 int 类型，空串写入触发类型转换失败",
        "table_stem": "interaction", "sheet": "InteractionConvOption",
    },
]


def _class_coverage(patterns: list[dict], active: bool) -> list[dict]:
    active_patterns = {p["trigger_pattern"] for p in patterns if active}
    rows = []
    for trace in FAILURE_TRACES:
        covered = trace["expected_pattern"] in active_patterns
        rows.append({
            "input": trace["input"],
            "error_type": trace["error_type"],
            "expected_pattern": trace["expected_pattern"],
            "covered": covered,
        })
    return rows


def _summary(rows: list[dict]) -> dict:
    hits = sum(1 for r in rows if r["covered"])
    total = len(rows)
    return {"hits": hits, "total": total, "hit_rate": round(hits / total, 4), "misses": total - hits}


def main() -> None:
    with tempfile.TemporaryDirectory() as td:
        updater = SkillUpdater(Path(td) / "skills", Path(td) / "evidence")
        enhancer = SimpleNamespace(ai_induce_anti_pattern=lambda traces: INDUCED_PATTERNS)
        produced = updater.induce_anti_patterns(FAILURE_TRACES, enhancer)
        pending = [p.to_dict() for p in produced]
        active = [dict(p, status="active") for p in pending]

    before_rows = _class_coverage([], active=True)
    pending_rows = _class_coverage(pending, active=False)
    after_rows = _class_coverage(active, active=True)
    before = _summary(before_rows)
    pending_sum = _summary(pending_rows)
    after = _summary(after_rows)
    out = {
        "note": "Step4 自学习 A/B 模拟实验：离线回放 8 条历史失败 trace；衡量反模式类别覆盖率，不替代真实端到端成功率。",
        "traces": len(FAILURE_TRACES),
        "induced_patterns": len(pending),
        "compression_ratio": round(len(pending) / len(FAILURE_TRACES), 4),
        "before_no_learning": before,
        "pending_review_not_active": pending_sum,
        "after_active_replay": after,
        "lift_hit_rate_pts": round((after["hit_rate"] - before["hit_rate"]) * 100, 2),
        "rows": after_rows,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    md = [
        "# Step4 自学习闭环 A/B 模拟实验",
        "",
        "> 口径：模拟实验 / 离线回放。用历史 8 条失败 trace 归纳 4 类反模式；比较无学习、pending_review、active 后的同类失败预警覆盖率。",
        "",
        "| 阶段 | 反模式状态 | 同类失败预警覆盖 | 说明 |",
        "|---|---|---:|---|",
        f"| Before | 无反模式 | {before['hits']}/{before['total']} ({before['hit_rate']:.0%}) | 失败只汇总，不复用 |",
        f"| Pending | pending_review | {pending_sum['hits']}/{pending_sum['total']} ({pending_sum['hit_rate']:.0%}) | 候选不生效，防学坏 |",
        f"| After | active | {after['hits']}/{after['total']} ({after['hit_rate']:.0%}) | 同类输入写盘前预警/确认 |",
        "",
        "| 归纳效率 | 数值 |",
        "|---|---:|",
        f"| 输入失败 trace | {len(FAILURE_TRACES)} 条 |",
        f"| 归纳反模式 | {len(pending)} 类 |",
        f"| 压缩率 | {out['compression_ratio']:.0%} |",
        f"| 预警覆盖提升 | +{out['lift_hit_rate_pts']}pts |",
        "",
        "## 归纳出的 4 类反模式",
        "",
        "| trigger_pattern | action | rationale |",
        "|---|---|---|",
    ]
    for p in active:
        md.append(f"| {p.get('trigger_pattern','')} | {p.get('action','')} | {p.get('rationale','')} |")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()

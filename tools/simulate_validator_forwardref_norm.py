"""模拟压测：validator_agent._norm_name 归一化修复 FORWARD_REF_BROKEN 假阳性。

目标文件：server/agent/excel/subagent/validator_agent.py
本次改动（见 git diff）：validate_fk_layer / _collect_produced_labels /
_collect_unresolved_placeholder_issues 里 produces/consumes 标签匹配从
「精确字符串相等」升级为「先查 _norm_name 归一化后再匹配」，修复命名风格漂移
（如 new_pet vs new_pet_id、下划线/驼峰混用、`<consume:label>` 前缀）导致的
FORWARD_REF_BROKEN 假阳性（合法前向引用被误判为"上游未产出"从而整条 intent
被 skip 不落盘）。

Before：按 git diff 还原的旧逻辑——`label == prod_label_this` /
`label not in produced` 精确字符串相等（旧代码已被替换，此处按 diff 复原,
不是猜测；对应 diff 位置：
    -                if prod_label_this and label == prod_label_this:
    -                if label not in produced:
    +                if prod_label_this and _norm_name(label) == _norm_name(prod_label_this):
    +                if (_norm_name(label) not in {_norm_name(x) for x in produced}
    +                        and label not in produced):
）。
After：直接 import 仓库真实 `_norm_name` 函数跑同一组数据。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
sys.path.insert(0, str(SERVER))

from agent.excel.subagent.validator_agent import _norm_name, _label_from_consumes  # noqa: E402

OUT_JSON = ROOT / "bench" / "ppt_validator_forwardref_norm.json"
OUT_MD = ROOT / "bench" / "ppt_validator_forwardref_norm.md"

# 每条：(produces_label, consumes_placeholder_raw, should_match)
# should_match=True 表示这是合法前向引用（命名风格漂移但语义一致），期望被识别为已产出。
# should_match=False 表示确实是悬空引用（producer 真的不存在/拼写错误到不相关），期望仍报 FORWARD_REF_BROKEN。
CASES = [
    # 命名风格漂移 —— 应匹配（5 组）
    {"produces": "new_pet_id", "consumes": "<new_pet>", "should_match": True,
     "note": "produces 带 _id 后缀，consumes 裸名"},
    {"produces": "New_Quest_Id", "consumes": "<new_quest_id>", "should_match": True,
     "note": "大小写漂移"},
    {"produces": "newConvId", "consumes": "<new_conv_id>", "should_match": True,
     "note": "驼峰 vs 下划线"},
    {"produces": "new_option_id", "consumes": "<consume:new_option_id>", "should_match": True,
     "note": "consume: 前缀"},
    {"produces": " new_npc_id ", "consumes": "<new_npc_id>", "should_match": True,
     "note": "首尾空白漂移"},
    {"produces": "new_activity_id", "consumes": "< New_Activity_Id >", "should_match": True,
     "note": "括号内空白 + 大小写"},
    # 确实悬空/不相关 —— 不应匹配（6 组）
    {"produces": "new_pet_id", "consumes": "<new_reward_id>", "should_match": False,
     "note": "标签语义不同，producer 确实不存在"},
    {"produces": "new_quest_id", "consumes": "<new_quest_target_id>", "should_match": False,
     "note": "看似相关但不同标签，不应模糊匹配"},
    {"produces": "new_conv_id", "consumes": "<new_option_id>", "should_match": False,
     "note": "拼写完全不同"},
    {"produces": "", "consumes": "<new_gate_id>", "should_match": False,
     "note": "无 producer，纯悬空引用"},
    {"produces": "new_mail_id", "consumes": "<consume:new_reward_group_id>", "should_match": False,
     "note": "consume: 前缀但标签不同"},
    {"produces": "new_skill_id", "consumes": "<new_skilllevel_id>", "should_match": False,
     "note": "子串相似但非同一标签，不应误合并"},
]


def before_match(produces: str, consumes_raw: str) -> bool:
    """旧逻辑等效实现（已被替换）：精确字符串相等，不做归一化。

    还原自 git diff：
        if prod_label_this and label == prod_label_this: continue
        if label not in produced: <FORWARD_REF_BROKEN>
    这里 produced 集合只含 1 个 producer 标签（单组对比场景）。
    """
    label = _label_from_consumes(consumes_raw)
    if label is None:
        return False
    produced = {produces} if produces else set()
    if produces and label == produces:
        return True
    return label in produced


def after_match(produces: str, consumes_raw: str) -> bool:
    """真实新逻辑：调用仓库真实 _norm_name 做归一化匹配。"""
    label = _label_from_consumes(consumes_raw)
    if label is None:
        return False
    produced = {produces} if produces else set()
    if produces and _norm_name(label) == _norm_name(produces):
        return True
    norm_produced = {_norm_name(x) for x in produced}
    return _norm_name(label) in norm_produced or label in produced


def main() -> None:
    rows = []
    before_fp = 0  # false positive = 应匹配却没匹配（导致假阳性 FORWARD_REF_BROKEN）
    after_fp = 0
    before_fn = 0  # false negative = 不该匹配却匹配了（漏检真悬空引用）
    after_fn = 0
    for c in CASES:
        b = before_match(c["produces"], c["consumes"])
        a = after_match(c["produces"], c["consumes"])
        should = c["should_match"]
        if should and not b:
            before_fp += 1
        if should and not a:
            after_fp += 1
        if not should and b:
            before_fn += 1
        if not should and a:
            after_fn += 1
        rows.append({
            "produces": c["produces"], "consumes": c["consumes"],
            "should_match": should, "before_matched": b, "after_matched": a,
            "note": c["note"],
        })

    n_should_match = sum(1 for c in CASES if c["should_match"])
    n_should_not = len(CASES) - n_should_match
    out = {
        "note": ("模拟压测：produces/consumes 命名风格漂移场景，"
                 "对比 before（精确字符串相等，按 diff 还原旧逻辑）"
                 " vs after（真实 _norm_name 归一化）。"
                 "口径：构造用例模拟，非端到端真实指令回放。"),
        "total_cases": len(CASES),
        "should_match_cases": n_should_match,
        "should_not_match_cases": n_should_not,
        "before": {
            "false_positive_forward_ref_broken": before_fp,
            "false_negative_missed_dangling": before_fn,
        },
        "after": {
            "false_positive_forward_ref_broken": after_fp,
            "false_negative_missed_dangling": after_fn,
        },
        "rows": rows,
    }
    OUT_JSON.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    md = [
        "# validator_agent _norm_name 归一化修复 FORWARD_REF_BROKEN 假阳性 模拟压测",
        "",
        "> 口径：模拟实验。构造 12 组 produces/consumes 命名风格漂移场景"
        "（6 组应匹配 / 6 组确实悬空不应匹配），对比 before（精确字符串相等，"
        "按 git diff 还原的旧逻辑等效实现，原始逻辑已被替换）vs after"
        "（直接调用仓库真实 `_norm_name` 归一化）。不与真实端到端指标混用。",
        "",
        "## 汇总",
        "",
        "| 指标 | Before（精确匹配） | After（_norm_name 归一化） |",
        "|---|---:|---:|",
        f"| 假阳性 FORWARD_REF_BROKEN（应匹配未匹配） | {before_fp}/{n_should_match} | {after_fp}/{n_should_match} |",
        f"| 漏检悬空引用（不该匹配却匹配） | {before_fn}/{n_should_not} | {after_fn}/{n_should_not} |",
        "",
        "## 明细",
        "",
        "| produces | consumes | 应匹配 | before | after | 说明 |",
        "|---|---|---:|---:|---:|---|",
    ]
    for r in rows:
        md.append(
            f"| `{r['produces']}` | `{r['consumes']}` | "
            f"{'✅' if r['should_match'] else '❌'} | "
            f"{'✅' if r['before_matched'] else '❌'} | "
            f"{'✅' if r['after_matched'] else '❌'} | {r['note']} |")
    OUT_MD.write_text("\n".join(md), encoding="utf-8")
    print(json.dumps(out, ensure_ascii=False, indent=2))
    print(f"written: {OUT_JSON}")
    print(f"written: {OUT_MD}")


if __name__ == "__main__":
    main()

"""repair 成功捕获全链 demo：capture → promote → 快路径 apply。

证明「失败→正确做法」经验真值闭环：
1. repair L1 成功（column_remap fix_payload，re-verify 验过）→ capture 入 staging
2. 同 error_signature 累积 ≥ RECIPE_PROMOTE_HITS(3) → promote committed active
3. 下次同签名失败 → verify-repair 循环先查 recipe → 命中直接 apply 已验证 fix
   + re-verify 通过 → 快路径返回（跳 playbook/LLM）

与方案2（LLM 猜 fix）区别：recipe 的 fix 是 **re-verify 验过的经验真值**，非猜。

运行: uv run python -m tests.demo_repair_recipe_fast_path
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEMAKER_SKIP_REGRESSION", "1")

import agent.excel.core.agent as agent_mod  # noqa: E402
from agent.excel.core.agent import TableAgent  # noqa: E402
from agent.excel.core.skill_updater import SkillUpdater, RECIPE_PROMOTE_HITS  # noqa: E402
from agent.excel.repair.error_classifier import ErrorType  # noqa: E402


class _Cls:
    def __init__(self, et=ErrorType.COLUMN_NOT_FOUND, col="名称"):
        self.error_type = et
        self.failed_col = col
        self.failed_val = "名"
        self.root_cause = "列名「名」未命中表头"
        self.confidence = 0.9


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="recipe_demo_"))
    su = SkillUpdater(tmp / "skills", tmp / "evidence")
    agent_mod.get_skill_updater = lambda: su

    # ── #1 capture：3 次 L1 成功修复（同 column_not_found pet.Pet.名称 → 名:名称）
    print("=" * 60)
    print(f"#1 capture：{RECIPE_PROMOTE_HITS} 次 L1 成功修复（column_remap 名→名称）")
    print("=" * 60)

    # 轻量 agent 绑 _capture_repair_recipe
    ag_cap = types.SimpleNamespace(enable_skill=True)
    ag_cap._capture_repair_recipe = TableAgent._capture_repair_recipe.__get__(ag_cap)
    fix_payload = {"column_remap": {"名": "名称"}, "value_coerce": {"名": 1}}  # value_coerce 会被滤
    for i in range(RECIPE_PROMOTE_HITS):
        ag_cap._capture_repair_recipe(_Cls(), "pet", "Pet", fix_payload, "level1")
    from agent.excel.core.skill_updater import _read_jsonl
    recs = _read_jsonl(su.repair_recipes_staging_path)
    print(f"staging：{len(recs)} 条；fix_payload 键={list(recs[0]['fix_payload'].keys())}（value_coerce 已滤）")

    # ── #2 promote
    print("\n" + "=" * 60)
    print("#2 promote → committed repair_recipes.yaml(active)")
    print("=" * 60)
    promoted = su.promote_repair_recipes()
    print(f"promoted: {promoted}")
    print(f"committed recipe yaml exists: {su.repair_recipes_path.exists()}")
    if su.repair_recipes_path.exists():
        print("--- repair_recipes.yaml ---")
        print(su.repair_recipes_path.read_text(encoding="utf-8"))
    print(f"staging 清空: {not su.repair_recipes_staging_path.exists()}")

    # ── #3 快路径 apply
    print("\n" + "=" * 60)
    print("#3 快路径：同签名失败再现 → 先查 recipe → apply 已验证 fix → re-verify 过")
    print("=" * 60)
    recipe = su.lookup_repair_recipe("column_not_found", "pet", "Pet", "名称")
    print(f"lookup recipe: {'命中' if recipe else '未命中'} → fix_kind={recipe.get('fix_kind') if recipe else None}")

    # 轻量 agent 绑 _try_recipe_fast_path + mock apply/verify
    thinking = []
    ag = types.SimpleNamespace(enable_skill=True)
    ag._try_recipe_fast_path = TableAgent._try_recipe_fast_path.__get__(ag)
    ag._rollback_write = MagicMock()
    ag._apply_repair_fix = MagicMock(return_value=True)  # fix 应用成功
    new_out = MagicMock(ok=True)
    ag._safe_redispatch = MagicMock(return_value=new_out)
    vr = MagicMock(); vr.passed = True
    ag._verify_write = MagicMock(return_value=vr)
    res = types.SimpleNamespace(
        ok=False, message="", _skip_summarize=False,
        add_thinking=lambda p, d: thinking.append((p, d)))

    out = ag._try_recipe_fast_path(_Cls(), MagicMock(), Path("pet.xlsx"), "Pet", res, None, True)
    print(f"快路径返回: {'new_out（命中即修，跳 playbook/LLM）' if out is not None else 'None'}")
    print(f"res.ok={res.ok} skip_summarize={res._skip_summarize}")
    print(f"thinking: {thinking}")

    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    print("- repair 成功的 fix_payload（re-verify 验过）= 经验真值，capture 入 staging。")
    print("- 同 error_signature 累积 3 次 → promote committed repair_recipes.yaml(active)。")
    print("- 下次同签名失败 → 快路径查 recipe → apply 已验证 fix + re-verify → 跳 playbook/LLM。")
    print("- 这是「失败→正确做法」的真闭环：fix 来自验过的成功，非 LLM 猜。")
    print("- value_coerce 等 case-specific fix 不入 recipe（不可泛化），只记 column_remap/clear_pk/allocate_new_id/add_dependency。")


if __name__ == "__main__":
    main()

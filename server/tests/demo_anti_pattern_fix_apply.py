"""方案2 修正学习全链 demo：induce 产 fix → 暂存 → promote active → apply 滤离群。

证明：失败 trace → LLM 归纳产 fix（正确修法）→ 暂存 pending_review → 达阈值
promote active → 下次同模式命中时 _apply_anti_pattern_fix_filter 写前 apply，
离群 issue 滤除 → 合法值通过 → 失败不发生（预防 > 修复）。

运行: uv run python -m tests.demo_anti_pattern_fix_apply
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

import yaml  # noqa: E402
import agent.excel.core.agent as agent_mod  # noqa: E402
from agent.excel.core.agent import TableAgent  # noqa: E402
from agent.excel.core.skill_updater import SkillUpdater  # noqa: E402


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fix_demo_"))
    su = SkillUpdater(tmp / "skills", tmp / "evidence")

    # mock enhancer：ai_induce 返回带 fix 的候选（模拟 LLM 归纳出「跳离群」修法）
    enhancer = MagicMock()
    enhancer.ai_induce_anti_pattern.return_value = [{
        "type": "semantic_pattern",
        "trigger_pattern": "新增,NPC,编号",
        "action": "warn_only",
        "rationale": "新增 NPC 编号远超历史 median 但合法（自增/拼接规则），离群误报",
        "table_stem": "entity_prefab",
        "sheet": "Base",
        "fix": {"skip_outlier_check": True},
    }]
    agent_mod.get_skill_updater = lambda: su

    print("=" * 60)
    print("#1 induce 产 fix → 暂存 pending_review（fix 保留）")
    print("=" * 60)
    su.induce_anti_patterns([{
        "input": "新增 NPC 编号 10013112007",
        "error_type": "semantic_outlier",
        "error_detail": "值远高于分布 median=10001",
        "entries_summary": "entity_prefab/Base 编号",
    }], enhancer)
    pend = su.load_pending_anti_patterns()
    print(f"暂存候选：{len(pend)} 条，fix={pend[0].fix if pend else '??'}")

    print("\n" + "=" * 60)
    print("#2 二次 induce → occ=2 → promote active（fix 带入 committed）")
    print("=" * 60)
    su.induce_anti_patterns([{
        "input": "新增 NPC 编号 10013112008",
        "error_type": "semantic_outlier",
        "error_detail": "值远高于分布",
        "entries_summary": "entity_prefab/Base 编号",
    }], enhancer)
    active = [a for a in su.load_anti_patterns() if a.fix]
    print(f"committed active 带 fix：{len(active)} 条")
    if active:
        a = active[0]
        print(f"  id={a.id} fix={a.fix} status={a.status}")
    print(f"暂存清空：{not su.pending_anti_patterns_path.exists()}")

    print("\n" + "=" * 60)
    print("#3 apply：命中 fix.skip_outlier_check → 离群 issue 滤除")
    print("=" * 60)
    # 轻量 agent：_check_anti_pattern 返回 active 条目（含 fix），绑 filter 方法
    ap_entry = active[0].to_dict() if active else {}
    ag = types.SimpleNamespace()
    ag._apply_anti_pattern_fix_filter = TableAgent._apply_anti_pattern_fix_filter.__get__(ag)
    ag._check_anti_pattern = lambda *a, **kw: ap_entry

    intent = types.SimpleNamespace(raw="新增 NPC 编号 10013112009")
    issues = [
        {"column": "编号", "reason": "值 10013112009 远高于列历史分布（median=10001, MAD=190）",
         "severity": "error"},
        {"column": "名称", "reason": "值为空", "severity": "warn"},
    ]
    filtered = ag._apply_anti_pattern_fix_filter("entity_prefab", "Base", intent, issues)
    print(f"apply 前 {len(issues)} 条 issue：{[i['reason'][:30] for i in issues]}")
    print(f"apply 后 {len(filtered)} 条 issue：{[i['reason'][:30] for i in filtered]}")
    print(f"离群 issue 滤除 → 合法编号通过，无 SEMANTIC_OUTLIER → 不进 repair → 失败预防。")

    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    print("- 失败 trace → LLM 归纳产 fix（正确修法）→ 暂存（svn:ignore）")
    print("- 达 AP_AI_HITS=2 → promote active（committed yaml，fix 带入）")
    print("- 下次同模式命中 → _apply_anti_pattern_fix_filter 写前 apply → 离群 issue 滤除")
    print("- 合法值通过 → 失败不发生。学到的「正确修法」真正指导，非只 warn。")


if __name__ == "__main__":
    main()

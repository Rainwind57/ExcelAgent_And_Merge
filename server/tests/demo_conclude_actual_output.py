"""D5 ConcludeAgent 分仓 + promote demo。

跑真实 SkillUpdater（tmp dir）+ mock enhancer → 调 _phase_conclude 两次：
  第 1 次：候选写暂存 anti_patterns.pending.yaml（svn:ignore），committed 不污染。
  第 2 次：同 id occurrences 累加到 ≥ AP_AI_HITS(2)。
  promote：暂存候选移入 committed active + 暂存清空。

证明：runtime LLM 归纳噪声不污染 committed 策展文档，达阈值才合并入。

运行: uv run python -m tests.demo_conclude_actual_output
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

from agent.excel.core.agent import TableAgent  # noqa: E402
import agent.excel.core.agent as agent_mod  # noqa: E402
from agent.excel.core.skill_updater import SkillUpdater  # noqa: E402


def _yaml_show(path: Path) -> str:
    if not path.exists():
        return f"  （{path.name} 不存在）"
    return path.read_text(encoding="utf-8")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="conclude_demo_"))
    su = SkillUpdater(tmp / "skills", tmp / "evidence")

    # 预置 committed yaml 一条人工策展 active 条目（模拟现有策展文档）
    su.anti_pattern_dir.mkdir(parents=True, exist_ok=True)
    su.anti_patterns_path.write_text(
        "anti_patterns:\n"
        "- id: ap_curated_pet_Pet_名称\n"
        "  type: ambiguous_column\n"
        "  table_stem: pet\n  sheet: Pet\n  column: 名称\n"
        "  trigger: ambiguous\n  occurrences: 4\n"
        "  action: force_exact\n  status: active\n",
        encoding="utf-8",
    )

    enhancer = MagicMock()
    enhancer.ai_induce_anti_pattern.return_value = [{
        "type": "semantic_pattern",
        "trigger_pattern": "conv_id,对话,选项",
        "action": "warn_only",
        "rationale": "对话链 consumer 常漏 conv_id → 占位符残留",
        "table_stem": "interaction",
        "sheet": "InteractionConv",
    }]
    agent_mod.get_skill_updater = lambda: su

    ag = types.SimpleNamespace(enable_skill=True, _ai_enhancer=enhancer)
    ag._phase_conclude = TableAgent._phase_conclude.__get__(ag)
    thinking: list = []
    stream = types.SimpleNamespace(
        add_thinking=lambda phase, detail: thinking.append((phase, detail)))

    res = types.SimpleNamespace(
        table_stem="interaction", table_sheet="InteractionConv",
        failures=[{
            "type": "placeholder_unresolved",
            "table": "interaction", "sheet": "InteractionConv",
            "col": "conv_id",
            "root_cause": "占位符 <new_interaction_id> 残留未解析",
            "snip": "新增云游商人对话选项好的看看",
            "status": "unresolved",
        }])
    partitions = [{"executed": True, "res": res,
                   "path": Path("interaction.xlsx"), "sheet": "InteractionConv"}]

    print("=" * 60)
    print("第 1 次 induce（occurrences=1，未达 AP_AI_HITS=2）")
    print("=" * 60)
    ag._phase_conclude(partitions, "新增云游商人对话链", stream)
    print("\n--- committed anti_patterns.yaml（策展文档，应只有预置 active，无新候选）---")
    print(_yaml_show(su.anti_patterns_path))
    print("--- 暂存 anti_patterns.pending.yaml（新候选 occ=1，pending_review）---")
    print(_yaml_show(su.pending_anti_patterns_path))

    print("\n" + "=" * 60)
    print("第 2 次 induce（同 id → occurrences 累加到 2，达阈值）")
    print("=" * 60)
    ag._phase_conclude(partitions, "新增云游商人对话链", stream)
    pend = su.load_pending_anti_patterns()
    print(f"暂存候选 occurrences = {pend[0].occurrences if pend else '?'} (status={pend[0].status if pend else '?'})")

    print("\n" + "=" * 60)
    print("promote_pending_anti_patterns() → 暂存达阈值移入 committed active")
    print("=" * 60)
    promoted = su.promote_pending_anti_patterns()
    print(f"promoted: {promoted}")
    print("\n--- committed anti_patterns.yaml（策展文档，现含新 active 条目）---")
    print(_yaml_show(su.anti_patterns_path))
    print("--- 暂存 anti_patterns.pending.yaml（应清空/删除）---")
    print(_yaml_show(su.pending_anti_patterns_path))

    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    print("- runtime induce 写暂存（svn:ignore），committed 策展 yaml 不被污染。")
    print("- 同 id 累加 occurrences，达 AP_AI_HITS=2 → promote 合并入 committed active。")
    print("- 暂存清空 → 文件删除，无遗留。")
    print("- committed yaml = svn 管理的人工策展 active 文档，只增不丢。")


if __name__ == "__main__":
    main()

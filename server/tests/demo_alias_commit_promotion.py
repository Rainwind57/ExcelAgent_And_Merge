"""B 成功学习 demo：runtime alias → committed column_short_form.yaml。

预置：
- committed column_short_form.yaml 一条 curated alias（名称: [名, 名字]）。
- runtime column_aliases.runtime.yaml 两条 alias：
  A. 称呼→名称 hits=5 conf=0.7（达 ALIAS_COMMIT_HITS=5 + MIN_CONF=0.6）。
  B. 颜色→毛色 hits=2 conf=0.9（hits 不足）。

promote_runtime_aliases_to_committed() → A 追加入 committed，B 不动。
runtime meta A 标 committed=True，二次调不重复。

证明：成功 op 学到的列映射累积达阈值后并入人工策展 committed 别名文档，
跨 dev/run 共享；未达标留 runtime 本地继续观察。

运行: uv run python -m tests.demo_alias_commit_promotion
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("CODEMAKER_SKIP_REGRESSION", "1")

import yaml  # noqa: E402
from agent.excel.core.skill_updater import SkillUpdater  # noqa: E402


def _show(path: Path) -> str:
    if not path.exists():
        return f"  （{path.name} 不存在）"
    return path.read_text(encoding="utf-8")


def main() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="alias_demo_"))
    su = SkillUpdater(tmp / "skills", tmp / "evidence")

    # committed curated 别名文档
    su.committed_aliases_path.parent.mkdir(parents=True, exist_ok=True)
    su.committed_aliases_path.write_text(yaml.safe_dump({
        "version": "1.0",
        "columns": {"pet": {"Pet": {"名称": ["名", "名字"]}}},
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")

    # runtime alias（模拟低置信/纠正命中累积）
    su.runtime_aliases_path.parent.mkdir(parents=True, exist_ok=True)
    su.runtime_aliases_path.write_text(yaml.safe_dump({
        "columns": {
            "pet": {
                "Pet": {
                    "称呼": "名称",   # 达阈值
                    "颜色": "毛色",   # hits 不足
                },
            },
        },
        "_meta": {
            "pet": {"Pet": {
                "称呼": {"hits": 5, "confidence_avg": 0.7, "source": "runtime"},
                "颜色": {"hits": 2, "confidence_avg": 0.9, "source": "runtime"},
            }},
        },
    }, allow_unicode=True, sort_keys=False), encoding="utf-8")

    print("=" * 60)
    print("输入：runtime 两条 alias —— 称呼→名称(达标) / 颜色→毛色(hits 不足)")
    print(f"阈值：ALIAS_COMMIT_HITS=5, ALIAS_COMMIT_MIN_CONF=0.6")
    print("=" * 60)
    print("\n--- promote 前 committed column_short_form.yaml ---")
    print(_show(su.committed_aliases_path))

    promoted = su.promote_runtime_aliases_to_committed()
    print(f"\npromoted: {promoted}")

    print("\n--- promote 后 committed column_short_form.yaml（称呼 应并入 名称 列表）---")
    print(_show(su.committed_aliases_path))
    print("--- runtime _meta（称呼 标 committed=True）---")
    print(_show(su.runtime_aliases_path))

    print("\n--- 二次调（已 committed，不重复）---")
    promoted2 = su.promote_runtime_aliases_to_committed()
    print(f"promoted2: {promoted2}（应空）")

    print("\n" + "=" * 60)
    print("结论")
    print("=" * 60)
    print("- 成功 op 学到的列映射 → runtime 累积达 5 次 + conf≥0.6 → 并入 committed。")
    print("- committed column_short_form.yaml = svn 管理，跨 dev/run 共享，只增不删。")
    print("- 未达标留 runtime 本地继续累积，不污染 committed 策展文档。")
    print("- 成功权重低于失败（阈值 5 > AP_AI_HITS 2）。")


if __name__ == "__main__":
    main()

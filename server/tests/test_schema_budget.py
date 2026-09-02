"""schema_budget 单测（文档「schema_budget」MVP #4）。

验证按分层 + 字符预算裁剪 schema 的纯函数逻辑：
  - 预算关闭(<=0) 或 未超预算 → 原样完整渲染（applied=False）。
  - 超预算 → required 完整 / dependency 摘要 / context 省略。
  - 未知 stem 安全按 required 保留；全裁空则退回完整。

运行: python -m pytest server/tests/test_schema_budget.py -q
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.schema_budget import (
    apply_schema_budget,
    render_full,
    render_summary,
    total_chars,
)


def _rec(stem, sheet, cols, sig=None):
    return {"stem": stem, "sheet": sheet, "cols": cols, "sig_cols": set(sig or [])}


class TestRender:
    def test_render_full(self):
        assert render_full(_rec("pet", "Pet", ["灵兽id", "名称"])) == "- pet/Pet: 灵兽id | 名称"

    def test_render_summary_pk_sig_id(self):
        r = _rec("pet_evolve", "PetEvolveData",
                 ["进化id", "宠物id", "进化后的灵兽ID", "描述", "进化等级"],
                 sig=["进化等级"])
        out = render_summary(r)
        # 主键(首列) + 命中列 + id 类列
        assert out.startswith("- pet_evolve/PetEvolveData: ")
        assert "进化id" in out and "进化等级" in out and "宠物id" in out
        assert "描述" not in out  # 非 pk/sig/id 列被省


class TestBudget:
    def _records(self):
        return [
            _rec("pet", "Pet", ["灵兽id", "名称", "品质", "元素", "体力", "物攻"]),
            _rec("pet_evolve", "PetEvolveData",
                 ["进化id", "宠物id", "进化后的灵兽ID", "描述"], sig=["描述"]),
            _rec("school", "School", ["school_id", "门派名", "说明"]),
        ]

    def test_disabled_returns_full(self):
        recs = self._records()
        lines, applied = apply_schema_budget(recs, {"required": ["pet"]}, 0)
        assert applied is False
        assert lines == [render_full(r) for r in recs]

    def test_under_budget_returns_full(self):
        recs = self._records()
        lines, applied = apply_schema_budget(recs, {"required": ["pet"]}, 100000)
        assert applied is False
        assert len(lines) == 3

    def test_over_budget_tiered(self):
        recs = self._records()
        groups = {"required": ["pet"], "dependency": ["pet_evolve"], "context": ["school"]}
        lines, applied = apply_schema_budget(recs, groups, 1)  # 强制超预算
        assert applied is True
        joined = "\n".join(lines)
        # required 完整
        assert "- pet/Pet: 灵兽id | 名称 | 品质 | 元素 | 体力 | 物攻" in joined
        # dependency 摘要（主键 + id 类，无非关键列）
        assert "pet_evolve/PetEvolveData" in joined
        assert "进化id" in joined and "宠物id" in joined
        # context 省略
        assert "school/School" not in joined
        assert len(lines) == 2

    def test_unknown_stem_kept_as_required(self):
        recs = [_rec("mystery", "S", ["a", "b", "c"])]
        lines, applied = apply_schema_budget(recs, {"required": []}, 1)
        # 未知 stem 安全按 required 保留完整（不误删动作主语表）
        assert lines == ["- mystery/S: a | b | c"]

    def test_all_context_falls_back_to_full(self):
        recs = [_rec("x", "S", ["a", "b"]), _rec("y", "S", ["c", "d"])]
        groups = {"required": [], "dependency": [], "context": ["x", "y"]}
        lines, applied = apply_schema_budget(recs, groups, 1)
        # 全是 context 会裁空 → 退回完整，避免 prompt 无 schema
        assert applied is False
        assert len(lines) == 2

    def test_total_chars(self):
        assert total_chars(["ab", "cde"]) == 3 + 4  # +1 换行 each

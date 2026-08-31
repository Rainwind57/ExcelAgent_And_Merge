"""§9.2 跨段引用上下文继承单测（路线图 §2/§9.2，0 LLM）。

覆盖：
  - 前序段声明实体 → 后段回指词（它/这个/前面那个）→ 生成上下文块。
  - 无回指/无前序实体 → 不生成（零噪声）。
  - enrich_segments 仅增强含回指的后段，前段原样。
  - 实体池顺序推进：最近前序优先；同名去重。
"""
from __future__ import annotations

from agent.excel.parser.cross_ref_context import (
    build_segment_context, enrich_segments, extract_declared_entities,
)


class TestExtractDeclaredEntities:
    def test_named_entity(self):
        ents = extract_declared_entities("新增一个灵兽叫朱雀，品质神兽")
        assert any(e["name"] == "朱雀" for e in ents)

    def test_quoted_entity(self):
        ents = extract_declared_entities("配一个活动叫「春节活动」")
        assert any(e["name"] == "春节活动" for e in ents)

    def test_no_entity(self):
        assert extract_declared_entities("把灵兽饕餮的攻击改为1500") == []


class TestBuildSegmentContext:
    def test_anaphora_resolves_to_recent_entity(self):
        ctxs = build_segment_context([
            "新增一个活动叫春节活动",
            "再把它绑定到邮件模板",
        ])
        assert ctxs[0] == ""
        assert "春节活动" in ctxs[1]
        assert "上文实体继承" in ctxs[1]

    def test_no_anaphora_no_context(self):
        ctxs = build_segment_context([
            "新增一个活动叫春节活动",
            "修改活动类型为限时",
        ])
        assert ctxs[1] == ""

    def test_no_antecedent_no_context(self):
        ctxs = build_segment_context(["把它改成神兽"])
        assert ctxs[0] == ""

    def test_bind_verb_resolves_even_without_pronoun(self):
        # 绑定类动作（无代词但需前文对象）→ 也继承最近实体
        ctxs = build_segment_context([
            "新增一个活动叫春节活动",
            "绑定到邮件模板1001",
        ])
        assert "春节活动" in ctxs[1]


class TestEnrichSegments:
    def test_only_anaphoric_segments_enriched(self):
        out = enrich_segments([
            "新增一个活动叫春节活动",
            "修改活动类型为限时",
            "再把它绑定到邮件模板",
        ])
        assert out[0] == "新增一个活动叫春节活动"
        assert out[1] == "修改活动类型为限时"
        assert out[2].startswith("【上文实体继承】")

    def test_dedup_same_entity_name(self):
        ctxs = build_segment_context([
            "新增一个活动叫春节活动",
            "新增一个活动叫春节活动",
            "把它删掉",
        ])
        # 同名实体去重，仍取最近
        assert "春节活动" in ctxs[2]


if __name__ == "__main__":
    import pytest
    pytest.main([__file__, "-v", "-s"])

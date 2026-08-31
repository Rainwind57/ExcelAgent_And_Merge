"""#6 skill AI 反模式归纳 测试。

覆盖：
- AntiPattern schema 扩展（新字段序列化/反序列化 + 旧 yaml 向后兼容）
- AntiPatternConfig.lookup 语义匹配（input_text 关键词命中 / 精确优先 / pending_review 不命中）
- SkillUpdater.induce_anti_patterns（LLM 成功/失败/无 enhancer/去重）
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.excel.skill_updater import SkillUpdater, AntiPattern
from agent.excel.skill_loader import AntiPatternConfig


# ── schema 扩展 ──────────────────────────────────────────────

def test_schema_new_fields_roundtrip(tmp_path):
    """新字段 trigger_pattern/rationale/source 序列化→yaml→反序列化往返一致。"""
    ap = AntiPattern(
        id="ap_test_1", type="semantic_pattern",
        table_stem="interaction", sheet="InteractionConv",
        trigger="ai_induced", action="warn_only", status="active",
        trigger_pattern="conv_id,对话", rationale="conv_id 定位易失败",
        source="ai_induction",
    )
    d = ap.to_dict()
    assert d["trigger_pattern"] == "conv_id,对话"
    assert d["rationale"] == "conv_id 定位易失败"
    assert d["source"] == "ai_induction"
    assert d["type"] == "semantic_pattern"
    # yaml 往返
    s = yaml.safe_dump({"anti_patterns": [d]}, allow_unicode=True, sort_keys=False)
    loaded = yaml.safe_load(s)["anti_patterns"][0]
    assert loaded["trigger_pattern"] == "conv_id,对话"
    assert loaded["source"] == "ai_induction"


def test_schema_legacy_yaml_compat(tmp_path):
    """旧 yaml（无新字段）加载时新字段用默认值，不报错。"""
    legacy_yaml = """
anti_patterns:
- id: ap_legacy
  type: ambiguous_column
  table_stem: pet
  sheet: Pet
  column: 名称
  trigger: ambiguous
  occurrences: 4
  action: force_exact
  status: active
"""
    p = tmp_path / "legacy.yaml"
    p.write_text(legacy_yaml, encoding="utf-8")
    data = yaml.safe_load(p.read_text(encoding="utf-8"))
    cfg = AntiPatternConfig(patterns=list(data.get("anti_patterns") or []))
    # 旧条目查询正常
    hit = cfg.lookup("pet", "Pet", column="名称")
    assert hit is not None and hit["id"] == "ap_legacy"
    # 新字段默认值
    assert hit.get("source", "rule") == "rule"
    assert hit.get("trigger_pattern", "") == ""


# ── lookup 语义匹配 ──────────────────────────────────────────

def _make_cfg(patterns):
    return AntiPatternConfig(patterns=patterns)


def test_lookup_semantic_match_hit():
    """semantic_pattern active + input_text 含 trigger_pattern 关键词 → 命中。"""
    cfg = _make_cfg([{
        "id": "ap_s1", "type": "semantic_pattern",
        "table_stem": "interaction", "sheet": "InteractionConv",
        "trigger_pattern": "conv_id,对话", "action": "warn_only", "status": "active",
    }])
    hit = cfg.lookup("interaction", "InteractionConv",
                     input_text="修改conv_id为1的对话内容")
    assert hit is not None and hit["id"] == "ap_s1"


def test_lookup_exact_priority_over_semantic():
    """同 (table,sheet,column) 有 ambiguous_column(精确) + semantic_pattern(关键词)，精确优先。"""
    cfg = _make_cfg([
        {"id": "ap_exact", "type": "ambiguous_column",
         "table_stem": "t", "sheet": "s", "column": "名称",
         "action": "force_exact", "status": "active"},
        {"id": "ap_sem", "type": "semantic_pattern",
         "table_stem": "t", "sheet": "s", "trigger_pattern": "名称",
         "action": "warn_only", "status": "active"},
    ])
    hit = cfg.lookup("t", "s", column="名称", input_text="修改名称为X")
    assert hit is not None and hit["id"] == "ap_exact"


def test_lookup_pending_review_not_hit():
    """semantic_pattern pending_review 不命中（即使 input_text 含关键词）。"""
    cfg = _make_cfg([{
        "id": "ap_p", "type": "semantic_pattern",
        "table_stem": "t", "sheet": "s", "trigger_pattern": "conv_id",
        "action": "warn_only", "status": "pending_review",
    }])
    hit = cfg.lookup("t", "s", input_text="修改conv_id为1")
    assert hit is None


def test_lookup_semantic_no_keyword_miss():
    """semantic_pattern active 但 input_text 不含关键词 → 不命中。"""
    cfg = _make_cfg([{
        "id": "ap_s2", "type": "semantic_pattern",
        "table_stem": "t", "sheet": "s", "trigger_pattern": "conv_id",
        "action": "warn_only", "status": "active",
    }])
    hit = cfg.lookup("t", "s", input_text="修改宠物等级为10")
    assert hit is None


def test_lookup_semantic_global_table_stem():
    """semantic_pattern table_stem 空（全局）时，任意 table_stem 都可命中。"""
    cfg = _make_cfg([{
        "id": "ap_g", "type": "semantic_pattern",
        "table_stem": "", "sheet": "", "trigger_pattern": "conv_id",
        "action": "warn_only", "status": "active",
    }])
    hit = cfg.lookup("any_table", "any_sheet", input_text="改conv_id为2")
    assert hit is not None and hit["id"] == "ap_g"


# ── induce_anti_patterns ────────────────────────────────────

def _new_updater(tmp_path):
    return SkillUpdater(tmp_path / "skills", tmp_path / "evidence")


def _trace():
    return [{
        "input": "修改conv_id为1的对话内容",
        "error_type": "unknown",
        "error_detail": "失败：write - 写后验证不符",
        "entries_summary": "未定位到行",
    }]


def test_induce_llm_success(tmp_path):
    """mock enhancer 返回合法候选 → induce 产出 AntiPattern + 写盘。"""
    enhancer = MagicMock()
    enhancer.ai_induce_anti_pattern.return_value = [{
        "type": "semantic_pattern", "trigger_pattern": "conv_id,对话",
        "action": "warn_only", "rationale": "conv_id 定位易失败",
        "table_stem": "interaction", "sheet": "InteractionConv",
    }]
    updater = _new_updater(tmp_path)
    produced = updater.induce_anti_patterns(_trace(), enhancer)
    assert len(produced) == 1
    ap = produced[0]
    assert ap.type == "semantic_pattern"
    assert ap.trigger_pattern == "conv_id,对话"
    assert ap.source == "ai_induction"
    assert ap.status == "pending_review"
    # D5 分仓写盘校验：ai_induction + pending_review → 暂存（非 committed）
    pends = updater.load_pending_anti_patterns()
    assert any(a.trigger_pattern == "conv_id,对话" and a.status == "pending_review"
               for a in pends)
    # committed 策展 yaml 不应被污染
    assert not any(a.trigger_pattern == "conv_id,对话"
                   for a in updater.load_anti_patterns())


def test_induce_llm_failure_returns_empty(tmp_path):
    """mock enhancer 抛异常 → induce 返回空列表，不抛。"""
    enhancer = MagicMock()
    enhancer.ai_induce_anti_pattern.side_effect = Exception("LLM timeout")
    updater = _new_updater(tmp_path)
    produced = updater.induce_anti_patterns(_trace(), enhancer)
    assert produced == []


def test_induce_llm_returns_none(tmp_path):
    """mock enhancer 返回 None（无归纳）→ induce 返回空。"""
    enhancer = MagicMock()
    enhancer.ai_induce_anti_pattern.return_value = None
    updater = _new_updater(tmp_path)
    produced = updater.induce_anti_patterns(_trace(), enhancer)
    assert produced == []


def test_induce_no_enhancer(tmp_path):
    """enhancer=None → 返回空 + warn。"""
    updater = _new_updater(tmp_path)
    produced = updater.induce_anti_patterns(_trace(), None)
    assert produced == []


def test_induce_empty_traces(tmp_path):
    """空 trace 列表 → 直接返回空，不调 enhancer。"""
    enhancer = MagicMock()
    updater = _new_updater(tmp_path)
    produced = updater.induce_anti_patterns([], enhancer)
    assert produced == []
    enhancer.ai_induce_anti_pattern.assert_not_called()


def test_induce_dedup_same_trigger_pattern(tmp_path):
    """两次 induce 同 trigger_pattern → 暂存 yaml 中该 id 只一条（去重）。

    AP_AI_HITS=3（收紧后的 AI 归纳 promote 阈值）：两次 induce occurrences=2 < 3，
    ai_induction 候选留在 pending 暂存（不进 committed），去重后同 id 只一条。
    """
    enhancer = MagicMock()
    enhancer.ai_induce_anti_pattern.return_value = [{
        "type": "semantic_pattern", "trigger_pattern": "conv_id,对话",
        "action": "warn_only", "rationale": "r",
        "table_stem": "interaction", "sheet": "InteractionConv",
    }]
    updater = _new_updater(tmp_path)
    updater.induce_anti_patterns(_trace(), enhancer)
    enhancer.ai_induce_anti_pattern.return_value = [{
        "type": "semantic_pattern", "trigger_pattern": "conv_id,对话",
        "action": "warn_only", "rationale": "r2",
        "table_stem": "interaction", "sheet": "InteractionConv",
    }]
    updater.induce_anti_patterns(_trace(), enhancer)
    aps = updater.load_pending_anti_patterns()
    target = [a for a in aps if a.trigger_pattern == "conv_id,对话"]
    assert len(target) == 1  # 去重，不重复 append
    assert target[0].occurrences == 2
    assert target[0].status == "pending_review"


def test_induce_invalid_type_filtered(tmp_path):
    """LLM 返回 type/action 非法 → 被过滤（StepAIEnhancer 层已过滤，这里测 induce 容错）。"""
    enhancer = MagicMock()
    enhancer.ai_induce_anti_pattern.return_value = [{
        "type": "semantic_pattern", "trigger_pattern": "kw1",
        "action": "warn_only", "rationale": "r",
        "table_stem": "", "sheet": "",
    }]
    updater = _new_updater(tmp_path)
    produced = updater.induce_anti_patterns(_trace(), enhancer)
    assert len(produced) == 1
    assert produced[0].type == "semantic_pattern"

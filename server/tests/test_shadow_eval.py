"""金标 shadow 评测打分 + error-budget 门控单测（纯函数，0 LLM，确定性）。"""
from agent.excel.core.pipeline.shadow_eval import score_case, aggregate_scores
from agent.excel.core.pipeline.promotion import evaluate_promotion, DEFAULT_ERROR_BUDGET


def _it(table, fields=None):
    return {"table_hint": table, "fields": fields or {}}


# ── score_case ──
def test_perfect_table_recall():
    s = score_case([_it("reward"), _it("mail")],
                   {"tables": ["reward", "mail"]})
    assert s["table_recall"] == 1.0
    assert s["table_precision"] == 1.0
    assert s["missing_tables"] == [] and s["extra_tables"] == []


def test_missing_and_extra_tables():
    s = score_case([_it("reward"), _it("ghost")],
                   {"tables": ["reward", "mail"]})
    assert s["table_recall"] == 0.5
    assert "mail" in s["missing_tables"]
    assert "ghost" in s["extra_tables"]


def test_field_recall():
    s = score_case([_it("reward", {"reward_id": 1, "名称": "x"})],
                   {"tables": ["reward"], "fields": {"reward": ["reward_id", "名称", "数量"]}})
    # 2/3 关键字段命中
    assert s["field_recall"] == round(2 / 3, 4)


def test_normalization_table_and_field():
    s = score_case([_it("Reward", {"reward_id:int": 1})],
                   {"tables": ["reward"], "fields": {"reward": ["Reward_ID"]}})
    assert s["table_recall"] == 1.0
    assert s["field_recall"] == 1.0


def test_empty_expect_is_recall_1():
    s = score_case([_it("x")], {"tables": []})
    assert s["table_recall"] == 1.0


# ── aggregate ──
def test_aggregate():
    scores = [
        score_case([_it("a"), _it("b")], {"tables": ["a", "b"]}),      # recall 1
        score_case([_it("a")], {"tables": ["a", "c"]}),                # recall .5, miss c
    ]
    agg = aggregate_scores(scores)
    assert agg["cases"] == 2
    assert agg["avg_table_recall"] == 0.75
    assert agg["total_missing_tables"] == 1
    assert agg["perfect_recall_cases"] == 1


# ── promotion gate ──
def test_promote_when_candidate_not_worse():
    base = {"avg_table_recall": 0.90, "avg_field_recall": 0.80,
            "avg_table_precision": 0.7, "total_missing_tables": 3}
    cand = {"avg_table_recall": 0.92, "avg_field_recall": 0.82,
            "avg_table_precision": 0.75, "total_missing_tables": 2}
    r = evaluate_promotion(base, cand)
    assert r["promote"] is True
    assert r["deltas"]["recall"] == 0.02


def test_hold_when_recall_drops_beyond_budget():
    base = {"avg_table_recall": 0.90, "total_missing_tables": 2}
    cand = {"avg_table_recall": 0.85, "total_missing_tables": 2}  # 跌 0.05 > 0.02
    r = evaluate_promotion(base, cand)
    assert r["promote"] is False
    assert any("recall" in v for v in r["violations"])


def test_hold_when_new_missing_tables():
    base = {"avg_table_recall": 0.9, "total_missing_tables": 2}
    cand = {"avg_table_recall": 0.9, "total_missing_tables": 5}
    r = evaluate_promotion(base, cand)
    assert r["promote"] is False
    assert any("缺表" in v for v in r["violations"])


def test_small_recall_drop_within_budget_promotes():
    base = {"avg_table_recall": 0.90, "total_missing_tables": 2}
    cand = {"avg_table_recall": 0.89, "total_missing_tables": 2}  # 跌 0.01 <= 0.02
    r = evaluate_promotion(base, cand)
    assert r["promote"] is True


def test_budget_override_min_recall():
    base = {"avg_table_recall": 0.5, "total_missing_tables": 0}
    cand = {"avg_table_recall": 0.5, "total_missing_tables": 0}
    r = evaluate_promotion(base, cand, budget={"min_recall": 0.8})
    assert r["promote"] is False
    assert any("下限" in v for v in r["violations"])


def test_default_budget_shape():
    assert set(DEFAULT_ERROR_BUDGET) >= {
        "max_recall_drop", "max_field_drop", "allow_new_missing"}

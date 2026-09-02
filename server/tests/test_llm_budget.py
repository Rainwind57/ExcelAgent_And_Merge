"""建议2 LLM 预算单测（纯状态，0 IO，确定性）。"""
from agent.excel.core.pipeline.llm_budget import LLMBudget


def test_consume_within_limit():
    b = LLMBudget(3)
    assert b.try_consume() and b.used == 1
    assert b.try_consume() and b.used == 2
    assert b.try_consume() and b.used == 3
    assert b.remaining == 0
    assert b.exhausted


def test_consume_over_limit_returns_false_no_increment():
    b = LLMBudget(2)
    assert b.try_consume()
    assert b.try_consume()
    assert not b.try_consume()      # 第 3 次超预算
    assert b.used == 2              # 不累加
    assert not b.can_afford()


def test_zero_limit_never_affords():
    b = LLMBudget(0)
    assert not b.can_afford()
    assert not b.try_consume()
    assert b.exhausted


def test_invalid_limit_defaults_to_3():
    assert LLMBudget("x").limit == 3
    assert LLMBudget(None).limit == 3


def test_negative_limit_clamped_zero():
    assert LLMBudget(-5).limit == 0


def test_snapshot():
    b = LLMBudget(3)
    b.try_consume()
    assert b.snapshot() == {"limit": 3, "used": 1, "remaining": 2, "exhausted": False}


def test_multi_consume():
    b = LLMBudget(3)
    assert b.try_consume(2)
    assert not b.try_consume(2)     # 只剩 1，要 2 → 拒绝
    assert b.try_consume(1)
    assert b.exhausted

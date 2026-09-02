"""主线2：_to_split_intents 非 dict 元素丢弃从静默 continue 转可追踪 trace（0 LLM）。"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.decompose_agent import DecomposeAgent


def _agent_with_sink():
    da = DecomposeAgent(parser=object())
    captured = []
    da._thinking_sink = lambda phase, detail: captured.append((phase, detail))
    return da, captured


def test_non_dict_items_dropped_but_traced():
    da, captured = _agent_with_sink()
    arr = [
        {"table": "tips", "sheet": "tips", "action": "add", "fields": {"key": "K1"}},
        "这是一个非法的非 dict 元素",   # 结构异常项，应被丢弃
        123,                            # 同上
        {"table": "guild", "sheet": "Const", "action": "add", "fields": {"key": "K2"}},
    ]
    intents, dropped_stems = da._to_split_intents(arr, "原文")

    # 只有两个合法 dict 产出 intent
    assert len(intents) == 2
    assert {it.table_hint for it in intents} == {"tips", "guild"}
    # 非 dict 丢弃留下可追踪 trace（禁止静默吞）
    assert any("非 dict" in d for _p, d in captured), captured


def test_all_dict_items_no_drop_trace():
    da, captured = _agent_with_sink()
    arr = [{"table": "tips", "sheet": "tips", "action": "add", "fields": {"key": "K"}}]
    intents, _ = da._to_split_intents(arr, "原文")
    assert len(intents) == 1
    assert not any("非 dict" in d for _p, d in captured)

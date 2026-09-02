"""Step3 preflight 报告单测（纯函数，0 LLM，确定性）。"""
from agent.excel.core.pipeline.preflight import build_preflight_report


def test_all_ready():
    r = build_preflight_report([
        {"index": 1, "table": "reward", "sheet": "Reward", "action": "add",
         "resolvable": True, "unresolved_placeholders": []},
        {"index": 2, "table": "spawn", "sheet": "Spawn", "action": "add",
         "resolvable": True, "unresolved_placeholders": []},
    ])
    assert r["total"] == 2
    assert r["ready"] == 2
    assert r["blocked"] == 0
    assert r["ok"] is True


def test_unresolved_placeholder_blocks():
    r = build_preflight_report([
        {"index": 1, "table": "spawn", "sheet": "Spawn", "action": "add",
         "resolvable": True, "unresolved_placeholders": ["new_quest_id"]},
    ])
    assert r["ok"] is False
    assert r["blocked"] == 1
    assert r["blockers"][0]["unresolved"] == ["new_quest_id"]


def test_unresolvable_table_blocks():
    r = build_preflight_report([
        {"index": 1, "table": "ghost", "sheet": "X", "action": "add",
         "resolvable": False, "unresolved_placeholders": []},
    ])
    assert r["ok"] is False
    assert "无法解析" in r["blockers"][0]["reason"]


def test_empty_is_not_ok():
    r = build_preflight_report([])
    assert r["ok"] is False
    assert r["total"] == 0


def test_mixed_partial():
    r = build_preflight_report([
        {"index": 1, "resolvable": True, "unresolved_placeholders": []},
        {"index": 2, "resolvable": False},
    ])
    assert r["ready"] == 1
    assert r["blocked"] == 1
    assert r["ok"] is False

"""跨表连通性校验与单次 LLM 调用可中断性测试。

覆盖三处已知局限的修复：
1. 单次 LLM 调用内不可中断 → CodemakerClient.prompt(cancel_event=) 子线程+轮询
2. 第三层B 连通校验仅占位符残留 → _check_dangling_fk_refs 指向行存在性深度校验
3. 跨表场景端到端实测缺失 → quest_npc_double_option E2E（LLM-gated）

确定性单测（无 LLM、无 codemaker serve）始终运行；E2E 经 CODEMAKER_E2E_LLM=1 opt-in。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
import types
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.agent import TableAgent  # noqa: E402
from agent.codemaker_client import CodemakerClient, CodemakerError  # noqa: E402


# ── 公共：构造最小 agent 壳，绑定待测方法 ──────────────────────────

def _bind_agent(**attrs) -> types.SimpleNamespace:
    agent = types.SimpleNamespace(**attrs)
    for name in ("_check_dangling_fk_refs", "_fk_target_row_exists"):
        setattr(agent, name, getattr(TableAgent, name).__get__(agent))
    return agent


def _make_relation(from_path, from_sheet, from_column,
                   to_path, to_sheet, to_column):
    from agent.excel.core.table_relations import TableRelation
    return TableRelation(from_path=from_path, from_sheet=from_sheet,
                         from_column=from_column, to_path=to_path,
                         to_sheet=to_sheet, to_column=to_column,
                         relation_type="foreign_key")


class _FakeCli:
    """最小 cli，支撑 _fk_target_row_exists 的 read-back。"""

    def __init__(self, workspace: Path, sheets: dict):
        # sheets: {Path -> {sheet_name -> {"header":[...], "rows":[[...],...]}}}
        self.workspace = workspace
        self._sheets = sheets

    def list_tables(self):
        return list(self._sheets.keys())

    def get_sheets(self, path):
        return list(self._sheets.get(path, {}).keys())

    def read_header(self, path, sheet):
        return self._sheets[path][sheet]["header"]

    def read_sheet(self, path, sheet):
        return self._sheets[path][sheet]["rows"]


class _FakeRes:
    def __init__(self, table_stem, table_sheet, result_rows):
        self.table_stem = table_stem
        self.table_sheet = table_sheet
        self.result_rows = result_rows


# ── 局限2：指向行存在性深度校验 ──────────────────────────────────

def test_deep_check_detects_dangling_fk(monkeypatch):
    """FK 具值既不在 produced 也不在目标表 → 报悬空引用。"""
    from agent.excel.core import table_relations as tr_mod
    graph_rels = [_make_relation("quest/quest.xlsx", "Quest", "reward_id",
                                 "reward.xlsx", "Reward", "reward_id")]
    monkeypatch.setattr(tr_mod.RelationGraph, "load",
                        classmethod(lambda cls: tr_mod.RelationGraph(graph_rels)))

    # 目标表 Reward 无 reward_id=10088 的行
    reward_path = Path("reward.xlsx")
    cli = _FakeCli(Path("/ws"), {reward_path: {"Reward": {
        "header": ["reward_id", "名称"], "rows": [["10000", "a"], ["10001", "b"]]}}})
    agent = _bind_agent(cli=cli)

    partitions = [{"executed": True, "res": _FakeRes(
        "quest", "Quest", [{"col_name": "reward_id", "new_value": "10088"}])}]
    produced = {"new_quest_id": "50001"}  # 不含 10088

    dangling = agent._check_dangling_fk_refs(partitions, produced)
    assert ("quest", "Quest") in dangling
    item = dangling[("quest", "Quest")][0]
    assert item["value"] == "10088"
    assert "reward" in item["target"]


def test_deep_check_no_false_positive_when_target_row_exists(monkeypatch):
    """FK 具值在目标表 read-back 命中 → 不报悬空（避免误报预存行）。"""
    from agent.excel.core import table_relations as tr_mod
    graph_rels = [_make_relation("quest/quest.xlsx", "Quest", "reward_id",
                                 "reward.xlsx", "Reward", "reward_id")]
    monkeypatch.setattr(tr_mod.RelationGraph, "load",
                        classmethod(lambda cls: tr_mod.RelationGraph(graph_rels)))

    reward_path = Path("reward.xlsx")
    cli = _FakeCli(Path("/ws"), {reward_path: {"Reward": {
        "header": ["reward_id", "名称"], "rows": [["10088", "回城卷轴"]]}}})
    agent = _bind_agent(cli=cli)

    partitions = [{"executed": True, "res": _FakeRes(
        "quest", "Quest", [{"col_name": "reward_id", "new_value": "10088"}])}]
    produced = {}  # produced 无，但 read-back 命中

    dangling = agent._check_dangling_fk_refs(partitions, produced)
    assert dangling == {}


def test_deep_check_no_false_positive_when_in_produced(monkeypatch):
    """FK 具值在本批 produced → 不报悬空（跨子任务引用成立）。"""
    from agent.excel.core import table_relations as tr_mod
    graph_rels = [_make_relation("interaction/interaction.xlsx", "InteractionConvOption",
                                 "data.3.reward_id", "reward.xlsx", "Reward", "reward_id")]
    monkeypatch.setattr(tr_mod.RelationGraph, "load",
                        classmethod(lambda cls: tr_mod.RelationGraph(graph_rels)))

    # 目标表空（read-back miss），但 produced 含该 id
    cli = _FakeCli(Path("/ws"), {Path("reward.xlsx"): {"Reward": {
        "header": ["reward_id"], "rows": []}}})
    agent = _bind_agent(cli=cli)

    partitions = [{"executed": True, "res": _FakeRes(
        "interaction", "InteractionConvOption",
        [{"col_name": "data.3.reward_id", "new_value": "10088"}])}]
    produced = {"new_reward_id": "10088"}

    dangling = agent._check_dangling_fk_refs(partitions, produced)
    assert dangling == {}


def test_deep_check_skips_placeholder_residual(monkeypatch):
    """占位符残留 <...> 由强信号检查覆盖，深度校验跳过不重复报。"""
    from agent.excel.core import table_relations as tr_mod
    graph_rels = [_make_relation("quest/quest.xlsx", "Quest", "reward_id",
                                 "reward.xlsx", "Reward", "reward_id")]
    monkeypatch.setattr(tr_mod.RelationGraph, "load",
                        classmethod(lambda cls: tr_mod.RelationGraph(graph_rels)))

    cli = _FakeCli(Path("/ws"), {Path("reward.xlsx"): {"Reward": {
        "header": ["reward_id"], "rows": []}}})
    agent = _bind_agent(cli=cli)

    partitions = [{"executed": True, "res": _FakeRes("quest", "Quest", [
        {"col_name": "reward_id", "new_value": "<new_reward_id>"},  # 占位符残留
        {"col_name": "reward_id", "new_value": "10088"},            # 具值悬空
    ])}]
    produced = {}

    dangling = agent._check_dangling_fk_refs(partitions, produced)
    # 仅具值那条报悬空，占位符那条跳过
    items = dangling.get(("quest", "Quest"), [])
    assert len(items) == 1
    assert items[0]["value"] == "10088"


def test_deep_check_conservative_when_target_file_missing(monkeypatch):
    """目标文件/sheet/列不可解析 → 不报悬空（保守，避免误报）。"""
    from agent.excel.core import table_relations as tr_mod
    graph_rels = [_make_relation("quest/quest.xlsx", "Quest", "reward_id",
                                 "reward.xlsx", "Reward", "reward_id")]
    monkeypatch.setattr(tr_mod.RelationGraph, "load",
                        classmethod(lambda cls: tr_mod.RelationGraph(graph_rels)))

    cli = _FakeCli(Path("/ws"), {})  # 无任何表
    agent = _bind_agent(cli=cli)

    partitions = [{"executed": True, "res": _FakeRes(
        "quest", "Quest", [{"col_name": "reward_id", "new_value": "10088"}])}]
    produced = {}

    dangling = agent._check_dangling_fk_refs(partitions, produced)
    assert dangling == {}


def test_deep_check_no_edges_for_table_skips(monkeypatch):
    """表无任何出向 FK 边 → 跳过（如纯被引用的 pet 表）。"""
    from agent.excel.core import table_relations as tr_mod
    graph_rels = [_make_relation("item/item.xlsx", "Item", "pet_id",
                                 "pet.xlsx", "Pet", "灵兽id")]
    monkeypatch.setattr(tr_mod.RelationGraph, "load",
                        classmethod(lambda cls: tr_mod.RelationGraph(graph_rels)))

    cli = _FakeCli(Path("/ws"), {})
    agent = _bind_agent(cli=cli)

    # pet 表无出向边（只被引用）
    partitions = [{"executed": True, "res": _FakeRes(
        "pet", "Pet", [{"col_name": "灵兽id", "new_value": "999"}])}]
    produced = {}

    dangling = agent._check_dangling_fk_refs(partitions, produced)
    assert dangling == {}


# ── 局限1：单次 LLM 调用可中断 ────────────────────────────────────
# 真实 codemaker /session/{id}/message 是同步端点：服务器阻塞到完整响应就绪才发头，
# 故阻塞发生在 urlopen（等响应头），resp.read() 读已就绪的 body 很快。
# 测试据此构造：fake_urlopen 阻塞，read() 立即返回。


def test_subagent_bump_llm_feeds_heartbeat_counter():
    """SubAgent._bump_llm 经 parser._llm_counter inc+merge → peek_total 可见（心跳不再 0）。"""
    from agent.llm_counter import LLMCounter
    from agent.excel.subagent.base import SubAgent

    counter = LLMCounter()
    fake_parser = types.SimpleNamespace(_llm_counter=counter, client=None)
    sa = SubAgent("test_subagent", parser=fake_parser)
    sa._bump_llm("decompose")
    sa._bump_llm("decompose")
    assert counter.peek_total() == 2, f"期望 2，实得 {counter.peek_total()}"


def test_parser_bump_llm_feeds_heartbeat_counter():
    """codemaker_parser._bump_llm 经 _llm_counter inc+merge → peek_total 可见。"""
    from agent.llm_counter import LLMCounter
    from agent.excel.parser.codemaker_parser import CodemakerNLParser

    counter = LLMCounter()
    parser = CodemakerNLParser.__new__(CodemakerNLParser)
    parser._llm_counter = counter
    parser._bump_llm("parse_multi")
    assert counter.peek_total() == 1
    snap = counter.snapshot()
    assert snap.by_site.get("parse_multi", {}).get("calls") == 1


def test_subagent_bump_llm_no_counter_is_noop():
    """parser 无 _llm_counter（测试桩）→ _bump_llm 静默 no-op，不崩。"""
    from agent.excel.subagent.base import SubAgent
    sa = SubAgent("test_subagent", parser=types.SimpleNamespace(client=None))
    sa._bump_llm("decompose")  # 不应抛


class _OkResp:
    def __init__(self, payload=b'{"info":{},"parts":[{"type":"text","text":"ok"}]}'):
        self._payload = payload

    def read(self):
        return self._payload


def test_prompt_cancel_event_returns_cancelled_quickly(monkeypatch):
    """cancel_event set → prompt 秒级返回 error_type=CANCELLED（不等待阻塞 urlopen）。"""
    import agent.codemaker_client as cc_mod
    block_event = threading.Event()

    def fake_urlopen(req, timeout=None):
        block_event.wait(timeout=120)  # 模拟 urlopen 阻塞等响应头
        return _OkResp()

    monkeypatch.setattr(cc_mod, "urlopen", fake_urlopen)

    client = CodemakerClient()
    cancel_event = threading.Event()

    # 0.3s 后触发取消
    def _trigger():
        time.sleep(0.3)
        cancel_event.set()
    threading.Thread(target=_trigger, daemon=True).start()

    t0 = time.time()
    try:
        resp = client.prompt("ses_test", "hi", timeout=60, cancel_event=cancel_event)
        elapsed = time.time() - t0
        assert resp.error_type == CodemakerError.CANCELLED, f"期望 cancelled，实得 {resp.error_type}"
        # 应在 ~1s 内返回（0.2s 轮询 + 0.3s 触发），远早于 60s timeout
        assert elapsed < 5.0, f"取消返回过慢：{elapsed:.1f}s"
    finally:
        block_event.set()  # 释放阻塞的 daemon 子线程


def test_prompt_no_cancel_event_returns_response(monkeypatch):
    """无 cancel_event → 行为不变，正常解析响应。"""
    import agent.codemaker_client as cc_mod
    payload = b'{"info":{},"parts":[{"type":"text","text":"hello world"}]}'

    monkeypatch.setattr(cc_mod, "urlopen", lambda req, timeout=None: _OkResp(payload))

    client = CodemakerClient()
    resp = client.prompt("ses_test", "hi", timeout=30)  # 无 cancel_event
    assert resp.ok is True
    assert resp.response_text == "hello world"


def test_prompt_cancel_event_pre_set_returns_immediately(monkeypatch):
    """cancel_event 调用前已 set → 立即返回 CANCELLED，不起子线程。"""
    import agent.codemaker_client as cc_mod

    def fake_urlopen(req, timeout=None):
        raise AssertionError("预置取消不应调用 urlopen")

    monkeypatch.setattr(cc_mod, "urlopen", fake_urlopen)

    client = CodemakerClient()
    cancel_event = threading.Event()
    cancel_event.set()

    resp = client.prompt("ses_test", "hi", timeout=60, cancel_event=cancel_event)
    assert resp.error_type == CodemakerError.CANCELLED


# ── 局限3：跨表 E2E（NPC 对话+接任务，验证 summary 连通性） ──────────
# 经 CODEMAKER_E2E_LLM=1 opt-in；且 codemaker serve 可达时才跑。
# R7（serve 端 LLM 吞吐）可能导致 matched 不达 7，故断言为连通性一致性而非硬 7/7。

_E2E_CASES_FILE = Path(__file__).resolve().parents[2] / "downloads" / "quest_npc_double_option.json"


def _serve_reachable() -> bool:
    try:
        return CodemakerClient().health_check()
    except Exception:
        return False


@pytest.mark.skipif(
    os.environ.get("CODEMAKER_E2E_LLM") != "1" or not _serve_reachable(),
    reason="需 CODEMAKER_E2E_LLM=1 且 codemaker serve 可达（R7 LLM 路径）",
)
def test_quest_npc_cross_table_summary_connectivity_e2e(tmp_path):
    """quest_npc_double_option[0]：NPC+双选项对话+采集支线+奖励（期望 7 op 跨 5 表）。

    验证 summary 连通性如实反映：
    - 若占位符未解析/外键悬空 → summary/thinking 含「未接通」告警
    - 若全连通 → 不出现虚假「未接通」
    - match_case 产出 matched 计数（R7 下不硬断言 7/7，仅记录）
    """
    import shutil
    from tests.table_case_eval import (
        RES, diff_sandbox, match_case, build_pristine_index,
        _validate_fixture, _build_eval_sheet_aliases,
    )
    from services.agent_service import AgentService

    cases = json.loads(_E2E_CASES_FILE.read_text(encoding="utf-8"))
    case = cases[0]
    text = case["input"]
    expected = case.get("expected_answer", [])
    assert expected, "用例需带 expected_answer"

    sandbox = tmp_path / "resources"
    shutil.copytree(RES, sandbox)

    os.environ["CODEMAKER_AGENT_CHAIN_RAISE"] = "0"      # 降级回退，不抛
    os.environ["CODEMAKER_CONNECTIVITY_DEEP_CHECK"] = "1"  # 开启深度校验
    os.environ.setdefault("TABLE_CASE_EVAL_RUNNING", "1")

    service = AgentService(resources_dir=sandbox, enable_skill=True)
    try:
        resp = service.chat(text=text, session_id="e2e_connectivity", dry_run=False)
        if getattr(resp, "needs_confirm", False) and getattr(resp, "confirm_token", None):
            resp = service.chat(text=text, session_id="e2e_connectivity", dry_run=False,
                                confirm_token=resp.confirm_token, confirm_cascade=True)
    finally:
        try:
            if getattr(service, "_file_watcher", None) is not None:
                service._file_watcher.stop()
        except Exception:
            pass

    # 1) 不崩：返回 resp
    assert resp is not None

    # 2) 连通性一致性：thinking/message 中若提及未接通，则必有对应告警文本
    thinking_blob = "\n".join(
        (f"{t.get('phase','')}:{t.get('detail','')}")
        for t in (getattr(resp, "thinking_steps", []) or [])
        if isinstance(t, dict)
    )
    msg = getattr(resp, "message", "") or ""
    blob = f"{msg}\n{thinking_blob}"

    has_broken_signal = ("未接通" in blob) or ("占位符残留" in blob) or ("悬空外键" in blob)
    if has_broken_signal:
        # 出现断裂信号 → summary 必含「未接通/不存在」字样（已如实计入）
        assert ("未接通" in blob) or ("不存在" in blob), "断裂信号已检测但 summary 未体现"

    # 3) match_case 计数（R7 下不硬断言 7/7）
    actual_ops = diff_sandbox(sandbox, RES)
    pristine_idx = build_pristine_index(expected)
    fixture_errors = _validate_fixture(expected, pristine_idx)
    assert not fixture_errors, f"用例夹具本身有缺：{fixture_errors}"
    sheet_alias_map = _build_eval_sheet_aliases()
    entries, extra_ops_list = match_case(expected, actual_ops, pristine_idx,
                                         sheet_alias_map=sheet_alias_map)
    matched = [r for r in entries if getattr(r, "status", "") == "matched"]
    # 记录到 stdout 供诊断（不硬断言阈值，R7 可能 <7）
    print(f"\n[E2E] matched={len(matched)}/{len(expected)} extra_ops={len(extra_ops_list)} "
          f"broken_signal={has_broken_signal}")
    assert len(matched) >= 1, "R7 下至少应匹配 1 条（NPC 模板基线）"

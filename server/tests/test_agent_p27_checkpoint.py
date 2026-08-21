"""TableAgent P27 checkpoint 接线单测（OPTIMIZATION_LEDGER §4 follow-up）。

覆盖 P27 4-step NL 路径 checkpoint 的 TableAgent 级 save/load/resume 方法
（_save_nl_checkpoint / _load_nl_checkpoint / _resume_from_checkpoint）。
opt-in CODEMAKER_4STEP_CHECKPOINT=1。4-step 路径 save 调用已接线（post_parse
+ post_validate）；resume 自动跳过 parse 留 follow-up（需 stall 检测 + 已成功
项跟踪 + e2e）。

P24/P25 真 partial 态（重激活 _mark_validation_skipped + _phase_execute skip
分支）需反转 O3 非阻断设计（design 决策，非接线）— 文档化为 design follow-up。

运行: python -m pytest server/tests/test_agent_p27_checkpoint.py -v
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.agent import TableAgent
from agent.excel.parser.nl_parser import NLIntent, ValidationResult


def _make_agent():
    """轻量 TableAgent（绕过重 __init__）。"""
    ag = object.__new__(TableAgent)
    ag._nl_checkpoints = {}
    return ag


def _intent(table="pet", sheet="Pet"):
    it = NLIntent(action="add", table_hint=table, sheet_hint=sheet,
                  raw="加灵兽", extras={"fields": {"pet_id": 1}})
    it.validation = ValidationResult(ok=True)
    return it


class TestP27SaveLoad:
    def test_save_off_when_env_not_set(self, monkeypatch):
        """CODEMAKER_4STEP_CHECKPOINT 缺省 → save 不写,返 False。"""
        monkeypatch.delenv("CODEMAKER_4STEP_CHECKPOINT", raising=False)
        ag = _make_agent()
        ok = ag._save_nl_checkpoint("sess", "post_parse", [_intent()])
        assert ok is False
        assert ag._nl_checkpoints == {}

    def test_save_on_writes_checkpoint(self, monkeypatch):
        """opt-in → save 写 _nl_checkpoints[session_id][stage]。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        it = _intent()
        ok = ag._save_nl_checkpoint("sess", "post_parse", [it])
        assert ok is True
        assert "sess" in ag._nl_checkpoints
        assert "post_parse" in ag._nl_checkpoints["sess"]
        assert len(ag._nl_checkpoints["sess"]["post_parse"]["intents"]) == 1

    def test_save_load_round_trip(self, monkeypatch):
        """opt-in → save + load round-trip NLIntent[]（含嵌套 validation）。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        it = _intent()
        it.validation = ValidationResult(issues=[{"col": "x"}], ok=False)
        ag._save_nl_checkpoint("sess", "post_validate", [it])
        loaded = ag._load_nl_checkpoint("sess", "post_validate")
        assert loaded is not None
        assert len(loaded) == 1
        rt = loaded[0]
        assert rt.table_hint == "pet"
        assert rt.validation is not None
        assert rt.validation.ok is False
        assert rt.validation.issues == [{"col": "x"}]

    def test_load_missing_returns_none(self, monkeypatch):
        """无 checkpoint → load 返 None。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        assert ag._load_nl_checkpoint("nope", "post_parse") is None
        assert ag._load_nl_checkpoint("sess", "nope") is None

    def test_save_empty_session_or_intents_skipped(self, monkeypatch):
        """空 session_id 或空 intents → save 不写,返 False。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        assert ag._save_nl_checkpoint("", "post_parse", [_intent()]) is False
        assert ag._save_nl_checkpoint("sess", "post_parse", []) is False
        assert ag._nl_checkpoints == {}

    def test_save_failure_silent_false(self, monkeypatch):
        """save 异常 → 静默返 False 不阻断。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._nl_checkpoints = None  # 故意致 .setdefault 抛
        assert ag._save_nl_checkpoint("sess", "post_parse", [_intent()]) is False

    def test_save_overwrites_same_stage(self, monkeypatch):
        """同 session+stage 二次 save → 覆盖（最新中间态）。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent("pet")])
        ag._save_nl_checkpoint("sess", "post_parse", [_intent("quest")])
        loaded = ag._load_nl_checkpoint("sess", "post_parse")
        assert len(loaded) == 1
        assert loaded[0].table_hint == "quest"  # 覆盖为最新


class TestP27Resume:
    def test_resume_off_when_env_not_set(self, monkeypatch):
        """缺省 → resume 返 (None, None, None)。"""
        monkeypatch.delenv("CODEMAKER_4STEP_CHECKPOINT", raising=False)
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent()])  # env off 不写
        intents, stage, completed = ag._resume_from_checkpoint("sess")
        assert intents is None
        assert stage is None
        assert completed is None

    def test_resume_prefers_post_validate(self, monkeypatch):
        """resume 优先 post_validate（最远中间态）,回退 post_parse。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent("pet")])
        ag._save_nl_checkpoint("sess", "post_validate", [_intent("quest")])
        intents, stage, completed = ag._resume_from_checkpoint("sess")
        assert stage == "post_validate"
        assert len(intents) == 1
        assert intents[0].table_hint == "quest"
        assert completed == []

    def test_resume_falls_back_post_parse(self, monkeypatch):
        """无 post_validate → 回退 post_parse。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent("pet")])
        intents, stage, completed = ag._resume_from_checkpoint("sess")
        assert stage == "post_parse"
        assert intents[0].table_hint == "pet"
        assert completed == []

    def test_resume_no_checkpoint_returns_none(self, monkeypatch):
        """无任何 checkpoint → (None, None, None)。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        intents, stage, completed = ag._resume_from_checkpoint("nope")
        assert intents is None
        assert stage is None
        assert completed is None

    def test_resume_empty_session_returns_none(self, monkeypatch):
        """空 session_id → (None, None, None)。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent()])
        intents, stage, completed = ag._resume_from_checkpoint("")
        assert intents is None
        assert stage is None
        assert completed is None

    def test_resume_isolation_per_session(self, monkeypatch):
        """多 session 隔离：sess_A 的 checkpoint 不被 sess_B resume。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess_A", "post_parse", [_intent("pet")])
        # sess_B 无 checkpoint
        intents_b, stage_b, completed_b = ag._resume_from_checkpoint("sess_B")
        assert intents_b is None
        # sess_A 有
        intents_a, stage_a, completed_a = ag._resume_from_checkpoint("sess_A")
        assert intents_a is not None
        assert intents_a[0].table_hint == "pet"
        assert completed_a == []


class TestO14CompletedOpKeys:
    """O14：completed_op_keys 字段 + _save_nl_progress 增量 + resume 返回 completed。"""

    def test_save_with_completed_op_keys_persisted(self, monkeypatch):
        """save 传 completed_op_keys → checkpoint dict 存该字段。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_validate", [_intent()],
                               completed_op_keys=[0, 2])
        ckpt = ag._nl_checkpoints["sess"]["post_validate"]
        assert ckpt["completed_op_keys"] == [0, 2]

    def test_save_default_completed_empty(self, monkeypatch):
        """save 不传 completed_op_keys → 默认空 list（parse/validate 后无 op 完成）。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent()])
        assert ag._nl_checkpoints["sess"]["post_parse"]["completed_op_keys"] == []

    def test_resume_returns_completed_op_keys(self, monkeypatch):
        """resume 返回 completed_op_keys（已成功 op 跳过集）。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_validate", [_intent("pet"), _intent("quest")],
                               completed_op_keys=[0])
        intents, stage, completed = ag._resume_from_checkpoint("sess")
        assert stage == "post_validate"
        assert completed == [0]
        assert len(intents) == 2

    def test_save_nl_progress_updates_completed(self, monkeypatch):
        """_save_nl_progress 增量回写：覆盖 post_validate 的 completed_op_keys。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_validate", [_intent("pet")],
                              completed_op_keys=[0])
        # Step5 又成功 op 1 → 增量回写
        ag._save_nl_progress("sess", [_intent("pet"), _intent("quest")],
                             completed_op_keys=[0, 1])
        intents, stage, completed = ag._resume_from_checkpoint("sess")
        assert completed == [0, 1]
        assert len(intents) == 2

    def test_save_nl_progress_falls_back_post_parse(self, monkeypatch):
        """_save_nl_progress 无 post_validate → 回退 post_parse stage。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent("pet")])
        ag._save_nl_progress("sess", [_intent("pet")], completed_op_keys=[0])
        intents, stage, completed = ag._resume_from_checkpoint("sess")
        assert stage == "post_parse"
        assert completed == [0]

    def test_save_nl_progress_no_checkpoint_returns_false(self, monkeypatch):
        """_save_nl_progress 无任何 checkpoint → 返 False（无 stage 可回写）。"""
        monkeypatch.setenv("CODEMAKER_4STEP_CHECKPOINT", "1")
        ag = _make_agent()
        assert ag._save_nl_progress("sess", [_intent()], completed_op_keys=[0]) is False

    def test_save_nl_progress_env_off_returns_false(self, monkeypatch):
        """_save_nl_progress env off → 返 False。"""
        monkeypatch.delenv("CODEMAKER_4STEP_CHECKPOINT", raising=False)
        ag = _make_agent()
        ag._save_nl_checkpoint("sess", "post_parse", [_intent()])  # env off 不写
        assert ag._save_nl_progress("sess", [_intent()], completed_op_keys=[0]) is False

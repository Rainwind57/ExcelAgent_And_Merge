"""ReAct 表级回读自检（_llm_verify_table_coverage）确定性单测。

与 _backfill_missing 的区别：_backfill_missing 用规则算 expected 集合差补漏；
本方法把候选池 + FK 图原样丢给 LLM，让 LLM 自己判断有没有漏产表。grounding
硬约束：LLM 只能从候选池（未产出部分）里选缺失表，候选池外的幻觉表名被拒绝。
"""
import os
import sys
import json as _json
from dataclasses import dataclass

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.locator_agent import CandidateTable, FKEdge
from agent.excel.core.cross_table_splitter import SplitIntent


class _MockResp:
    def __init__(self, text):
        self.response_text = text
        self.ok = True
        self.error = ""


class _VerifyMockClient:
    """按 prompt 关键词分流：自检 prompt 走 missing_stems 响应，其它（单表重拆）
    走按 stem 预设的响应表。"""

    def __init__(self, missing_stems_response, retry_responses=None):
        self._missing_resp = missing_stems_response
        self._retry_responses = retry_responses or {}
        self.calls = []

    def create_session(self, **kw):
        @dataclass
        class S:
            ok: bool = True
            session_id: str = "mock-sid"
        return S()

    def health_check(self):
        return True

    def prompt(self, sid, prompt, timeout=90, model="", cancel_event=None):
        self.calls.append(prompt)
        if "自我复核" in prompt:
            return _MockResp(self._missing_resp)
        # 单表重拆：按 prompt 里出现的候选 stem 分流
        for stem, resp in self._retry_responses.items():
            if f"- {stem}/" in prompt or f"stem={stem}" in prompt or stem in prompt:
                return _MockResp(resp)
        return _MockResp("[]")

    def extract_json_from_response(self, text):
        import re
        m = re.search(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)
        if not m:
            m = re.search(r"(\{.*\}|\[.*\])", text, re.DOTALL)
        if not m:
            return None
        try:
            return _json.loads(m.group(1))
        except ValueError:
            return None


class _MockParser:
    def __init__(self, client):
        self.client = client
        self.directory = ""
        self.model = ""


def _existing_intent(stem, sheet, produces=""):
    return SplitIntent(text="x", table_hint=stem, sheet_hint=sheet, action="add",
                        fields={}, produces=produces)


class _PetCli:
    """最小 CLI stub：让 _decompose_single_prompt 的重拆能构出非空 schema。"""

    class _P:
        def __init__(self, stem):
            self.stem = stem

    def __init__(self):
        self._paths = [self._P("pet"), self._P("pet_evolve")]

    def list_tables(self):
        return self._paths

    def get_sheets(self, path):
        return {"pet": ["Pet"], "pet_evolve": ["PetEvolveData"]}[path.stem]

    def read_header(self, path, sheet):
        return {"Pet": ["宠物id", "名称"],
                "PetEvolveData": ["进化id", "宠物id"]}[sheet]

    def read_type_row(self, path, sheet):
        return {"Pet": ["宠物id:int", "名称:string"],
                "PetEvolveData": ["进化id:int", "宠物id:int"]}[sheet]


def test_confirms_missing_stem_and_backfills():
    client = _VerifyMockClient(
        missing_stems_response='{"missing_stems": ["pet_evolve"]}',
        retry_responses={
            "pet_evolve": '```json\n[{"table":"pet_evolve","sheet":"PetEvolveData",'
                          '"action":"add","fields":{"进化id":"<new_pet_id>"}}]\n```',
        })
    da = DecomposeAgent(parser=_MockParser(client), cli=_PetCli())
    intents = [_existing_intent("pet", "Pet", produces="new_pet_id")]
    candidates = [CandidateTable("pet", "Pet", 1.0),
                  CandidateTable("pet_evolve", "PetEvolveData", 0.9)]
    fk_edges = [FKEdge("pet_evolve", "PetEvolveData", "宠物id", "pet", "Pet", "宠物id")]
    out = da._llm_verify_table_coverage("新增灵兽子鼠并配置进化链", intents,
                                         candidates, fk_edges, 40)
    stems = {it.table_hint for it in out}
    assert stems == {"pet", "pet_evolve"}


def test_no_missing_returns_unchanged():
    client = _VerifyMockClient(missing_stems_response='{"missing_stems": []}')
    da = DecomposeAgent(parser=_MockParser(client))
    intents = [_existing_intent("pet", "Pet")]
    candidates = [CandidateTable("pet", "Pet", 1.0),
                  CandidateTable("pet_evolve", "PetEvolveData", 0.9)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠", intents, candidates, [], 40)
    assert len(out) == 1
    assert out[0].table_hint == "pet"


def test_rejects_out_of_candidate_hallucinated_stem():
    """grounding 硬约束：LLM 提的缺失表若不在候选池里，直接拒绝，不触发重拆。"""
    client = _VerifyMockClient(
        missing_stems_response='{"missing_stems": ["ghost_table"]}')
    da = DecomposeAgent(parser=_MockParser(client))
    intents = [_existing_intent("pet", "Pet")]
    candidates = [CandidateTable("pet", "Pet", 1.0),
                  CandidateTable("pet_evolve", "PetEvolveData", 0.9)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠", intents, candidates, [], 40)
    stems = {it.table_hint for it in out}
    assert "ghost_table" not in stems
    assert stems == {"pet"}
    # 幻觉表名被拒绝，不应该为它触发单表重拆 prompt（只应有 1 次自检调用）
    assert len(client.calls) == 1


def test_all_produced_still_checks_for_extras_but_finds_none():
    """候选池已全部产出 → 仍会调 1 次 LLM 做精确率反向检查（是否有多余表），
    但本例 LLM 判定无多余 → intents 不变。"""
    client = _VerifyMockClient(missing_stems_response='{"missing_stems": []}')
    da = DecomposeAgent(parser=_MockParser(client))
    intents = [_existing_intent("pet", "Pet"), _existing_intent("pet_evolve", "PetEvolveData")]
    candidates = [CandidateTable("pet", "Pet", 1.0),
                  CandidateTable("pet_evolve", "PetEvolveData", 0.9)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠", intents, candidates, [], 40)
    assert len(out) == 2
    assert len(client.calls) == 1


def test_single_produced_table_no_unproduced_skips_entirely():
    """只产出 1 张表、候选也全部产出 → 没有"多余"可言（单表本身无从比较），
    零 LLM 调用（避免对最简单的单表场景也白跑一次自检）。"""
    client = _VerifyMockClient(missing_stems_response='{"missing_stems": []}')
    da = DecomposeAgent(parser=_MockParser(client))
    intents = [_existing_intent("pet", "Pet")]
    candidates = [CandidateTable("pet", "Pet", 1.0)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠", intents, candidates, [], 40)
    assert len(out) == 1
    assert len(client.calls) == 0


def test_disabled_by_env_flag(monkeypatch):
    monkeypatch.setenv("CODEMAKER_DECOMPOSE_TABLE_SELFCHECK", "0")
    client = _VerifyMockClient(
        missing_stems_response='{"missing_stems": ["pet_evolve"]}')
    da = DecomposeAgent(parser=_MockParser(client))
    intents = [_existing_intent("pet", "Pet")]
    candidates = [CandidateTable("pet", "Pet", 1.0),
                  CandidateTable("pet_evolve", "PetEvolveData", 0.9)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠", intents, candidates, [], 40)
    assert len(out) == 1
    assert len(client.calls) == 0


def test_no_candidates_or_intents_is_noop():
    client = _VerifyMockClient(missing_stems_response='{"missing_stems": []}')
    da = DecomposeAgent(parser=_MockParser(client))
    assert da._llm_verify_table_coverage("text", [], [], [], 40) == []
    intents = [_existing_intent("pet", "Pet")]
    assert da._llm_verify_table_coverage("text", intents, [], [], 40) == intents


def test_llm_call_exception_falls_back_gracefully():
    """自检 LLM 调用异常（如 mock client 缺方法）→ 原样返回，不阻断主链路。"""
    class _BrokenClient:
        def create_session(self, **kw):
            @dataclass
            class S:
                ok: bool = True
                session_id: str = "mock-sid"
            return S()

        def prompt(self, *a, **kw):
            return _MockResp('{"missing_stems": ["pet_evolve"]}')
        # 故意不实现 extract_json_from_response，触发 AttributeError

    da = DecomposeAgent(parser=_MockParser(_BrokenClient()))
    intents = [_existing_intent("pet", "Pet")]
    candidates = [CandidateTable("pet", "Pet", 1.0),
                  CandidateTable("pet_evolve", "PetEvolveData", 0.9)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠", intents, candidates, [], 40)
    assert out == intents


def test_extra_stems_removes_unreferenced_hallucinated_table():
    """精确率反向检查：LLM 判定某已产出表是弱信号凑巧混入 → 安全移除
    （该表不是任何其它 intent 的 FK producer 依赖）。"""
    client = _VerifyMockClient(
        missing_stems_response='{"missing_stems": [], "extra_stems": ["space"]}')
    da = DecomposeAgent(parser=_MockParser(client))
    intents = [_existing_intent("pet", "Pet", produces="new_pet_id"),
               _existing_intent("space", "Space", produces="new_space_id")]
    candidates = [CandidateTable("pet", "Pet", 1.0),
                  CandidateTable("space", "Space", 0.4)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠子鼠", intents, candidates, [], 40)
    stems = {it.table_hint for it in out}
    assert stems == {"pet"}


def test_extra_stems_does_not_remove_table_referenced_by_other_intent():
    """安全约束：即使 LLM 判定某表多余，若它是另一个 intent 的 FK producer
    （其它 intent 的 fields 里引用了它的 produces 占位符），也不移除——
    移除会撕断真实跨表引用链，比留着多余表更危险。"""
    client = _VerifyMockClient(
        missing_stems_response='{"missing_stems": [], "extra_stems": ["reward"]}')
    da = DecomposeAgent(parser=_MockParser(client))
    reward_intent = SplitIntent(text="x", table_hint="reward", sheet_hint="Reward",
                                 action="add", fields={}, produces="new_reward_id")
    mail_intent = SplitIntent(text="x", table_hint="mail", sheet_hint="GlobalMail",
                               action="add", fields={"奖励id": "<new_reward_id>"})
    out = da._llm_verify_table_coverage(
        "新增奖励并发邮件", [reward_intent, mail_intent],
        [CandidateTable("reward", "Reward", 1.0), CandidateTable("mail", "GlobalMail", 1.0)],
        [], 40)
    stems = {it.table_hint for it in out}
    assert "reward" in stems  # 被引用，不移除


def test_extra_stems_rejects_hallucinated_stem_not_in_produced():
    """grounding：extra_stems 只能是已产出的表，候选池外/未产出的幻觉名被忽略。"""
    client = _VerifyMockClient(
        missing_stems_response='{"missing_stems": [], "extra_stems": ["ghost_table"]}')
    da = DecomposeAgent(parser=_MockParser(client))
    intents = [_existing_intent("pet", "Pet")]
    candidates = [CandidateTable("pet", "Pet", 1.0)]
    out = da._llm_verify_table_coverage("新增灵兽子鼠", intents, candidates, [], 40)
    assert len(out) == 1
    assert out[0].table_hint == "pet"

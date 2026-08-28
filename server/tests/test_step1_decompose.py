# -*- coding: utf-8 -*-
"""Step1 decompose/parse 解析层健壮性测试（聚焦 Step1，不走 Step2/3/4）。

针对真实跑出的 Step1 失败场景（dict 嵌套值、produces/consumes 标签悬空、
神通名误填、空 fields 占位壳），用 LLM JSON 重放（MockParser.set_response）
或直接构造 SplitIntent，验证 DecomposeAgent 解析层（_to_split_intents +
_flatten_dict_fields + _lint_split_intents）的修正逻辑。

不依赖整条 pipeline / 真实 serve，快、确定、可重复。
"""
import sys
import threading
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.locator_agent import LocatorResult, CandidateTable, FKEdge
from agent.excel.cli_interface import StubCodeMakerCLI


class MockLLMResponse:
    def __init__(self, text):
        self.response_text = text
        self.ok = True
        self.error = ""


class MockClient:
    def __init__(self):
        self._next = ""
        self._calls = 0
        self._lck = threading.Lock()

    def set_response(self, t):
        self._next = t

    def create_session(self, **kw):
        @dataclass
        class S:
            ok: bool = True
            session_id: str = "mock"
        return S()

    def health_check(self):
        return True

    def prompt(self, sid, prompt, timeout=90, model="", cancel_event=None):
        if "候选表 schema" in prompt:
            with self._lck:
                n = self._calls
                self._calls += 1
            if n == 0:
                return MockLLMResponse(self._next)
            return MockLLMResponse("[]")
        return MockLLMResponse(self._next)


class MockParser:
    def __init__(self):
        self.client = MockClient()
        self.directory = ""
        self.model = ""


_REPO = Path(__file__).resolve().parents[2]
_RES = _REPO / "resources"


def make_da(sink):
    """da 带 thinking_sink 收集 lint 文本。cli 用 Stub（不读真实 header，
    _flatten 走无 schema 分支 → dict 置空；_lint 不依赖 cli）。"""
    cli = StubCodeMakerCLI(workspace=_RES)
    return DecomposeAgent(parser=MockParser(), thinking_sink=sink, cli=cli)


def _make_sink():
    out = []

    def _s(phase, detail):
        out.append((phase, detail))
    return out, _s


# ── _lint: produces/consumes 标签悬空 → FK 边保守回填 ───────────────

def test_lint_consumes_label_backfill_unique():
    """#3 场景：SchoolTalentLevel consumes <new_pojun_lv1_id> 对不上 produces
    new_school_talent_id。FK 边指向 school_talent，本批唯一 producer → 回填标签。"""
    out, sink = _make_sink()
    da = make_da(sink)
    arr = [
        {"table": "school_talent", "sheet": "SchoolTalent", "action": "add",
         "fields": {"名称": "破军"}, "produces": "new_school_talent_id", "consumes": {}},
        {"table": "school_talent", "sheet": "SchoolTalentLevel", "action": "add",
         "fields": {"天赋id": "<new_pojun_lv1_id>", "层级": 1},
         "produces": "", "consumes": {"天赋id": "new_pojun_lv1_id"}},
    ]
    intents, _dropped = da._to_split_intents(arr, "破军天赋等级")
    # 注入 FK 边：(school_talent/SchoolTalentLevel.天赋id) → school_talent
    fk = [FKEdge("school_talent", "SchoolTalentLevel", "天赋id",
                 "school_talent", "SchoolTalent", "天赋id")]
    n = da._lint_split_intents(intents, fk)
    assert intents[1].fields["天赋id"] == "<new_school_talent_id>", \
        f"应回填为 <new_school_talent_id>，实际 {intents[1].fields['天赋id']!r}"
    assert n >= 1, f"应至少修正 1 项，实际 {n}"
    assert any("标签悬空" in d for _, d in out), f"应上报悬空 thinking：{out}"
    print("PASS lint_label_backfill_unique: 悬空标签按 FK 边回填到唯一 producer")


def test_lint_consumes_label_ambiguous_no_backfill():
    """被引表有 2 个 producer（破军/贪狼两 SchoolTalent add 各 produces）→ 歧义，
    不回填（避免串错行），仅 warning。fields 不变。"""
    out, sink = _make_sink()
    da = make_da(sink)
    arr = [
        {"table": "school_talent", "sheet": "SchoolTalent", "action": "add",
         "fields": {"名称": "破军"}, "produces": "new_pojun_id_1", "consumes": {}},
        {"table": "school_talent", "sheet": "SchoolTalent", "action": "add",
         "fields": {"名称": "贪狼"}, "produces": "new_pojun_id_2", "consumes": {}},
        {"table": "school_talent", "sheet": "SchoolTalentLevel", "action": "add",
         "fields": {"天赋id": "<new_x_unmatched>"},
         "produces": "", "consumes": {"天赋id": "new_x_unmatched"}},
    ]
    intents, _ = da._to_split_intents(arr, "两天赋等级")
    fk = [FKEdge("school_talent", "SchoolTalentLevel", "天赋id",
                 "school_talent", "SchoolTalent", "天赋id")]
    before = intents[2].fields["天赋id"]
    da._lint_split_intents(intents, fk)
    assert intents[2].fields["天赋id"] == before, "歧义不应回填"
    assert any("歧义不回填" in d for _, d in out), f"应上报歧义：{out}"
    print("PASS lint_label_ambiguous_no_backfill: 多 producer 歧义不回填只 warning")


def test_lint_consumes_label_matched_no_change():
    """consume 标签匹配 produces → 不动。"""
    out, sink = _make_sink()
    da = make_da(sink)
    arr = [
        {"table": "pet", "sheet": "Pet", "action": "add",
         "fields": {"名称": "饕餮"}, "produces": "new_pet_id", "consumes": {}},
        {"table": "pet_evolve", "sheet": "PetEvolveData", "action": "add",
         "fields": {"宠物id": "<new_pet_id>"},
         "produces": "", "consumes": {"宠物id": "new_pet_id"}},
    ]
    intents, _ = da._to_split_intents(arr, "饕餮进化")
    fk = [FKEdge("pet_evolve", "PetEvolveData", "宠物id",
                 "pet", "Pet", "灵兽id")]
    n = da._lint_split_intents(intents, fk)
    assert intents[1].fields["宠物id"] == "<new_pet_id>", "匹配标签不应改"
    assert n == 0, f"无修正应报 0，实际 {n}"
    print("PASS lint_label_matched_no_change: 标签闭环匹配不动")


def test_lint_dict_residue_cleared():
    """#2 场景：_flatten 漏网/直接构造的 dict 残留 → _lint 双保险置空（防落盘崩）。"""
    out, sink = _make_sink()
    da = make_da(sink)
    arr = [
        {"table": "ability", "sheet": "Ability", "action": "add",
         "fields": {"名称": "裂空斩", "cost": 5}, "produces": "", "consumes": {}},
    ]
    intents, _ = da._to_split_intents(arr, "裂空斩")
    # 模拟 _flatten 漏网：手工注入 dict / list-dict 残留
    intents[0].fields["nested"] = {"a": 1}
    intents[0].fields["listd"] = [{"x": 2}]
    n = da._lint_split_intents(intents, [])
    assert intents[0].fields["nested"] == "", f"dict 应置空，实际 {intents[0].fields['nested']!r}"
    assert intents[0].fields["listd"] == "", f"list-dict 应置空"
    assert n == 2, f"应修正 2 项，实际 {n}"
    print("PASS lint_dict_residue_cleared: dict/list-dict 残留置空")


# ── _flatten_dict_fields: dict 嵌套值不落盘 ──────────────────────────

def test_flatten_dict_fields_no_schema_drop():
    """无 schema（Stub cli read_header 空）→ dict/list-dict 直接置空（绝不落盘）。"""
    out, sink = _make_sink()
    da = make_da(sink)  # Stub cli 不读真实 header
    fields = {"名称": "裂空斩", "data": {"cost": 0, "require_level": 1}, "listd": [{"x": 2}]}
    notes = da._flatten_dict_fields(fields, "ability", "Ability")
    assert fields["data"] == "", f"无 schema dict 应置空，实际 {fields['data']!r}"
    assert fields["listd"] == "", "list-dict 应置空"
    assert notes and any("data" in x for x in notes)
    print("PASS flatten_dict_no_schema_drop: 无 schema dict/list-dict 置空")


def test_decompose_runs_lint_and_keeps_dict_free():
    """端到端 decompose（重放 LLM JSON 含 dict 占位列）后 fields 无 dict 残留，
    且 produces/consumes 标签经 _lint 闭环。"""
    out, sink = _make_sink()
    da = make_da(sink)
    da.parser.client.set_response("""```json
[
  {"table":"pet","sheet":"Pet","action":"add",
   "fields":{"名称":"饕餮","灵兽类型":"神兽","badobj":{"cost":0}},
   "produces":"new_pet_id","consumes":{}},
  {"table":"pet_evolve","sheet":"PetEvolveData","action":"add",
   "fields":{"宠物id":"<new_pet_id>","进化等级":"1"},
   "produces":"","consumes":{"宠物id":"new_pet_id"}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("pet", "Pet", 1.0),
                    CandidateTable("pet_evolve", "PetEvolveData", 0.9)],
        fk_edges=[FKEdge("pet_evolve", "PetEvolveData", "宠物id",
                         "pet", "Pet", "灵兽id")])
    intents = da.decompose("新增灵兽饕餮,进化成饕餮王", lr)
    assert len(intents) == 2, f"期望 2 条，实际 {len(intents)}"
    # 无 dict 残留（_flatten 或 _lint 兜底）
    for it in intents:
        for k, v in (it.fields or {}).items():
            assert not isinstance(v, dict), f"{it.table_hint}.{k} 仍残留 dict: {v}"
    # consume 占位匹配 produces
    assert intents[1].fields.get("宠物id") == "<new_pet_id>"
    print("PASS decompose_dict_free_lint_closure: decompose 后无 dict 残留 + 标签闭环")


if __name__ == "__main__":
    for fn in [test_lint_consumes_label_backfill_unique,
               test_lint_consumes_label_ambiguous_no_backfill,
               test_lint_consumes_label_matched_no_change,
               test_lint_dict_residue_cleared,
               test_flatten_dict_fields_no_schema_drop,
               test_decompose_runs_lint_and_keeps_dict_free]:
        try:
            fn()
        except AssertionError as e:
            print(f"FAIL {fn.__name__}: {e}")
            raise

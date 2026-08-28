# -*- coding: utf-8 -*-
"""DecomposeAgent 分解正确性测试:4 种结构样例覆盖。

不依赖真实 LLM(超时/不可达),用 MockParser 返回预设 JSON,
严格验证 DecomposeAgent 对不同输入的分解逻辑:
  1. 每表产一 op(抑制过产)
  2. produces/consumes 标签正确
  3. fields 用真实表头列名
  4. FK 链消费占位符替换
  5. 三 agent 完整链路 Locator→Decompose→Validator
"""
import sys
import json
import threading
import traceback
from pathlib import Path
from dataclasses import dataclass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # server/ → agent.* 命名空间

from agent.excel.subagent.locator_agent import (
    LocatorAgent, LocatorResult, CandidateTable, FKEdge)
from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.validator_agent import ValidatorAgent
from agent.excel.cli_interface import StubCodeMakerCLI


# ── Mock LLM:返回预设 JSON 数组 ──────────────────────────────
class MockLLMResponse:
    def __init__(self, text):
        self.response_text = text
        self.ok = True
        self.error = ""


class MockClient:
    def __init__(self):
        self._next_response = ""
        self._llm_responses = {}  # 关键词 → 响应文本(模拟 Locator 的 LLM 裁决)
        # DecomposeAgent per-candidate 调用计数(R8g 改每表一 prompt,但测试预设是全表数组):
        # 首次调用返全数组(模拟 LLM 一次产全部 op),后续候选返空,避免每候选返全数组致翻倍。
        # 多候选 ThreadPoolExecutor 并发,用 lock 保首次计数原子。
        self._decompose_calls = 0
        self._decompose_lock = threading.Lock()

    def set_response(self, json_text):
        self._next_response = json_text

    def create_session(self, **kw):
        @dataclass
        class S:
            ok: bool = True
            session_id: str = "mock-sid"
        return S()

    def health_check(self):
        return True

    def prompt(self, sid, prompt, timeout=90, model="", cancel_event=None):
        # Locator 的 LLM 裁决(歧义时)用 prompt 内容判断返回 stem
        if "路由专家" in prompt or "选最合适" in prompt:
            # 简单返回首个候选 stem
            import re
            m = re.search(r"候选 = ([^\n]+)", prompt)
            if m:
                cands = m.group(1).split("、")
                return MockLLMResponse(cands[0].strip())
            return MockLLMResponse("pet")
        # DecomposeAgent per-candidate prompt(R8g 改每表一 prompt,但测试预设是全表数组):
        # 首次调用返全数组(模拟 LLM 一次产全部 op),后续候选返空数组,避免每候选返全数组致翻倍。
        # 多候选 ThreadPoolExecutor 并发调 prompt,用 lock 保首次计数原子。
        if "候选表 schema" in prompt:
            with self._decompose_lock:
                n = self._decompose_calls
                self._decompose_calls += 1
            if n == 0:
                return MockLLMResponse(self._next_response)
            return MockLLMResponse("[]")
        return MockLLMResponse(self._next_response)

    def extract_json_from_response(self, text):
        import re
        m = re.search(r"```json\s*(\[.*?\])\s*```", text, re.DOTALL)
        if not m:
            m = re.search(r"\[\s*\{.*\}\s*\]", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except ValueError:
                return None
        return None


class MockParser:
    def __init__(self):
        self.client = MockClient()
        self.directory = ""
        self.model = ""
        self._session_id = ""


# ── 测试夹具 ─────────────────────────────────────────────────
# repo 根 resources/（cwd 无关：从 server/ 或 repo 根跑 pytest 均解析对）
_REPO_ROOT = Path(__file__).resolve().parents[2]
_RESOURCES = _REPO_ROOT / "resources"


def make_cli():
    return StubCodeMakerCLI(workspace=_RESOURCES)


def make_agent():
    parser = MockParser()
    cli = make_cli()
    da = DecomposeAgent(parser=parser, thinking_sink=lambda p, d: None, cli=cli)
    return da, parser


# ── 样例1: pet 进化链(2 表, FK 链)──────────────────────────
def test_pet_evolve_chain():
    da, parser = make_agent()
    parser.client.set_response("""```json
[
  {"table":"pet","sheet":"Pet","action":"add",
   "fields":{"名称":"饕餮","灵兽model_id":"1020","灵兽类型":"神兽"},
   "produces":"new_pet_id","consumes":{}},
  {"table":"pet_evolve","sheet":"PetEvolveData","action":"add",
   "fields":{"宠物id":"<new_pet_id>","进化后的灵兽ID":"<new_pet_id>","进化等级":"1"},
   "produces":"","consumes":{"宠物id":"new_pet_id","进化后的灵兽ID":"new_pet_id"}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("pet", "Pet", 1.0),
                    CandidateTable("pet_evolve", "PetEvolveData", 0.9)],
        fk_edges=[FKEdge("pet_evolve", "PetEvolveData", "宠物id", "pet", "Pet", "灵兽id"),
                  FKEdge("pet_evolve", "PetEvolveData", "进化后的灵兽ID", "pet", "Pet", "灵兽id")])
    intents = da.decompose("新增灵兽饕餮,进化成饕餮王", lr)

    assert len(intents) == 2, f"期望 2 条,实际 {len(intents)}"
    pet_it = intents[0]
    ev_it = intents[1]
    assert pet_it.table_hint == "pet", f"pet 表错: {pet_it.table_hint}"
    assert pet_it.sheet_hint == "Pet"
    assert pet_it.action == "add"
    assert pet_it.produces == "new_pet_id", f"produces 错: {pet_it.produces}"
    assert ev_it.fields["宠物id"] == "<new_pet_id>", f"消费占位符未替换: {ev_it.fields}"
    assert ev_it.fields["进化后的灵兽ID"] == "<new_pet_id>"
    assert "名称" in pet_it.fields, f"非真实列名: {list(pet_it.fields.keys())}"
    assert "灵兽model_id" in pet_it.fields
    print("PASS pet_evolve_chain: 2 op, produces+consumes 闭环, 真实列名")


# ── 样例2: mail 跨 sheet(同表 2 sheet, FK)──────────────────
def test_mail_cross_sheet():
    da, parser = make_agent()
    parser.client.set_response("""```json
[
  {"table":"mail","sheet":"MailTemplate","action":"add",
   "fields":{"标题":"新手礼包","内容":"欢迎来到游戏"},
   "produces":"new_mail_template_id","consumes":{}},
  {"table":"mail","sheet":"GlobalMail","action":"add",
   "fields":{"模板ID":"<new_mail_template_id>","邮件类型":"系统"},
   "produces":"new_global_mail_id","consumes":{"模板ID":"new_mail_template_id"}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("mail", "MailTemplate", 1.0),
                    CandidateTable("mail", "GlobalMail", 0.9)],
        fk_edges=[FKEdge("mail", "GlobalMail", "模板ID", "mail", "MailTemplate", "模板ID")])
    intents = da.decompose("新增新手礼包邮件模板", lr)

    assert len(intents) == 2, f"期望 2 条,实际 {len(intents)}"
    assert intents[0].sheet_hint == "MailTemplate"
    assert intents[1].sheet_hint == "GlobalMail"
    assert intents[0].produces == "new_mail_template_id"
    assert intents[1].fields["模板ID"] == "<new_mail_template_id>", \
        f"跨 sheet 消费未替换: {intents[1].fields}"
    print("PASS mail_cross_sheet: 同表跨 sheet, 模板ID 消费闭环")


# ── 样例3: item 装备(item 单表多列, 验证不误拆)──────────────
def test_item_single_table():
    da, parser = make_agent()
    parser.client.set_response("""```json
[
  {"table":"item","sheet":"ItemBase","action":"add",
   "fields":{"名称":"屠龙刀","道具类型":"武器","品质":"传说"},
   "produces":"new_item_id","consumes":{}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("item", "ItemBase", 1.0)],
        fk_edges=[])
    intents = da.decompose("新增屠龙刀装备", lr)

    assert len(intents) == 1, f"单表应产 1 条,实际 {len(intents)}"
    assert intents[0].table_hint == "item"
    assert intents[0].produces == "new_item_id"
    assert "名称" in intents[0].fields
    print("PASS item_single_table: 单表不误拆, 1 op")


# ── 样例4: quest_npc 多表 DAG(对话+任务+实体, 3 表 FK 链)───
def test_quest_npc_dag():
    da, parser = make_agent()
    # 模拟 quest_npc 11 步链的 3 核心表:interaction + spawn_world_entity + task
    parser.client.set_response("""```json
[
  {"table":"interaction","sheet":"Interaction","action":"add",
   "fields":{"交互效果":"对话","对话内容":"你好"},
   "produces":"new_interaction_id","consumes":{}},
  {"table":"spawn_world_entity","sheet":"SpawnWorldEntity","action":"add",
   "fields":{"交互id":"<new_interaction_id>","实体名字":"NPC福神"},
   "produces":"new_spawn_id","consumes":{"交互id":"new_interaction_id"}},
  {"table":"task","sheet":"Task","action":"add",
   "fields":{"任务名称":"福神任务","触发交互":"<new_interaction_id>"},
   "produces":"new_task_id","consumes":{"触发交互":"new_interaction_id"}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("interaction", "Interaction", 1.0),
                    CandidateTable("spawn_world_entity", "SpawnWorldEntity", 0.9),
                    CandidateTable("task", "Task", 0.85)],
        fk_edges=[FKEdge("spawn_world_entity", "SpawnWorldEntity", "交互id",
                         "interaction", "Interaction", "交互id"),
                  FKEdge("task", "Task", "触发交互",
                         "interaction", "Interaction", "交互id")])
    intents = da.decompose("新增NPC福神带对话和任务", lr)

    assert len(intents) == 3, f"3 表 DAG 应产 3 条,实际 {len(intents)}"
    # interaction 是 producer, spawn + task 都消费它
    inter = intents[0]
    spawn = intents[1]
    task = intents[2]
    assert inter.produces == "new_interaction_id"
    assert spawn.fields["交互id"] == "<new_interaction_id>"
    assert task.fields["触发交互"] == "<new_interaction_id>"
    print("PASS quest_npc_dag: 3 表 DAG, interaction 为 producer 被 2 表消费")


# ── 样例5: 过产抑制(mail LLM 产 3 op, 应保留 2)─────────────
def test_over_produce_suppress():
    da, parser = make_agent()
    parser.client.set_response("""```json
[
  {"table":"mail","sheet":"MailTemplate","action":"add",
   "fields":{"标题":"礼包A"},"produces":"new_mail_template_id","consumes":{}},
  {"table":"mail","sheet":"MailTemplate","action":"add",
   "fields":{"标题":"礼包B"},"produces":"new_mail_template_id2","consumes":{}},
  {"table":"mail","sheet":"GlobalMail","action":"add",
   "fields":{"模板ID":"<new_mail_template_id>"},"produces":"","consumes":{"模板ID":"new_mail_template_id"}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("mail", "MailTemplate", 1.0),
                    CandidateTable("mail", "GlobalMail", 0.9)],
        fk_edges=[FKEdge("mail", "GlobalMail", "模板ID", "mail", "MailTemplate", "模板ID")])
    intents = da.decompose("新增礼包邮件", lr)

    assert len(intents) == 3, f"Decompose 产 {len(intents)}(不过滤, Validator 才过滤)"
    va = ValidatorAgent(parser=parser, thinking_sink=lambda p, d: None, cli=make_cli())
    res = va.validate(intents, lr)
    assert len(res["intents"]) == 2, f"Validator 应抑制到 2,实际 {len(res['intents'])}"
    sheets = [it.sheet_hint for it in res["intents"]]
    assert "MailTemplate" in sheets
    assert "GlobalMail" in sheets
    print("PASS over_produce_suppress: 3→2, 同表第2条被抑制")


# ── 样例6: produces 标签漂移归一 ────────────────────────────
def test_produce_label_normalize():
    da, parser = make_agent()
    parser.client.set_response("""```json
[
  {"table":"pet","sheet":"Pet","action":"add",
   "fields":{"名称":"a"},"produces":"new_pet","consumes":{}},
  {"table":"pet_evolve","sheet":"PetEvolveData","action":"add",
   "fields":{"宠物id":"<new_pet>"},"produces":"","consumes":{"宠物id":"new_pet"}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("pet", "Pet", 1.0),
                    CandidateTable("pet_evolve", "PetEvolveData", 0.9)],
        fk_edges=[FKEdge("pet_evolve", "PetEvolveData", "宠物id", "pet", "Pet", "灵兽id")])
    intents = da.decompose("x", lr)
    va = ValidatorAgent(parser=parser, thinking_sink=lambda p, d: None, cli=make_cli())
    res = va.validate(intents, lr)
    assert res["intents"][0].produces == "new_pet_id", \
        f"归一失败: {res['intents'][0].produces}"
    fixes_text = " ".join(res["fixes"])
    assert "new_pet → new_pet_id" in fixes_text or "new_pet" in fixes_text
    print("PASS produce_label_normalize: new_pet → new_pet_id")


# ── 样例7: consumes 断链检测 ────────────────────────────────
def test_consume_broken_link():
    da, parser = make_agent()
    parser.client.set_response("""```json
[
  {"table":"pet","sheet":"Pet","action":"add",
   "fields":{"名称":"a"},"produces":"new_pet_id","consumes":{}},
  {"table":"mail","sheet":"GlobalMail","action":"add",
   "fields":{"模板ID":"<new_reward_id>"},"produces":"","consumes":{"模板ID":"new_reward_id"}}
]
```""")
    lr = LocatorResult(
        candidates=[CandidateTable("pet", "Pet", 1.0),
                    CandidateTable("mail", "GlobalMail", 0.9)],
        fk_edges=[])
    intents = da.decompose("x", lr)
    va = ValidatorAgent(parser=parser, thinking_sink=lambda p, d: None, cli=make_cli())
    res = va.validate(intents, lr)
    issues_text = " ".join(res["issues"])
    assert "断链" in issues_text or "new_reward_id" in issues_text, \
        f"未检出断链: {res['issues']}"
    assert not res["ok"], f"断链应 ok=False: ok={res['ok']}"
    print("PASS consume_broken_link: 检出 new_reward_id 断链")


# ── 样例8: 空/畸形 LLM 返回降级（零 LLM 兜底） ────────────────
def test_malformed_llm_fallback():
    """LLM 返非 JSON 时走 _splitter_baseline 零 LLM 兜底，不返空。

    pet 进化链文本触发 detect_cross_table_action=evolve → splitter 11 模板产 intent。
    保链路完整走通，serve 挂/超时/非 JSON 时仍产可执行 intent。
    """
    da, parser = make_agent()
    parser.client.set_response("抱歉,无法理解指令")
    lr = LocatorResult(
        candidates=[CandidateTable("pet", "Pet", 1.0),
                    CandidateTable("pet_evolve", "PetEvolveData", 0.9)],
        fk_edges=[])
    intents = da.decompose("灵兽饕餮进化成饕餮王", lr)
    # 零 LLM 兜底应产 intent（splitter evolve 模板命中），不返空
    assert len(intents) >= 1, f"零 LLM 兜底应产 intent,实际 {len(intents)}"
    print(f"PASS malformed_llm_fallback: 非JSON走兜底产 {len(intents)} 条")


# ── 样例8b: 零 LLM 兜底（LLM 全空响应） ─────────────────────
def test_zero_llm_fallback_empty_response():
    """LLM 路径全空响应（serve 挂/超时）时 _splitter_baseline 产确定性 intent。

    不依赖 LLM，用 splitter 11 模板 + ColumnExtractor 信号兜底。
    """
    da, parser = make_agent()
    # MockClient.prompt 对 DecomposeAgent 候选表 schema prompt 返空（模拟 serve 挂）
    parser.client.set_response("")
    lr = LocatorResult(
        candidates=[CandidateTable("pet", "Pet", 1.0),
                    CandidateTable("pet_evolve", "PetEvolveData", 0.9)],
        fk_edges=[FKEdge("pet_evolve", "PetEvolveData", "宠物id",
                          "pet", "Pet", "灵兽id")])
    intents = da.decompose("灵兽饕餮进化成饕餮王", lr)
    # 兜底应产 intent（splitter evolve 模板），不返空
    assert len(intents) >= 1, f"零 LLM 兜底应产 intent,实际 {len(intents)}"
    # 验证兜底 intent 基本结构
    for it in intents:
        assert it.table_hint, f"兜底 intent 缺 table_hint: {it}"
        assert it.action in ("add", "set", "delete", "get"), \
            f"兜底 intent action 非法: {it.action}"
    print(f"PASS zero_llm_fallback: LLM 全空走兜底产 {len(intents)} 条")


# ── 样例9: action 类型透传(add/set/delete)─────────────────
def test_action_passthrough():
    da, parser = make_agent()
    parser.client.set_response("""```json
[
  {"table":"pet","sheet":"Pet","action":"set",
   "fields":{"名称":"改名"},"produces":"","consumes":{}}
]
```""")
    lr = LocatorResult(candidates=[CandidateTable("pet", "Pet", 1.0)], fk_edges=[])
    intents = da.decompose("修改灵兽名称", lr)
    assert len(intents) == 1
    assert intents[0].action == "set", f"action 透传错: {intents[0].action}"
    print("PASS action_passthrough: set action 正确透传")


# ── 样例10: 三 agent 完整链路 Locator→Decompose→Validator ──
def test_full_agent_chain():
    """端到端:文本 → Locator 探候选 → Decompose 分解 → Validator 校验修正。"""
    parser = MockParser()
    cli = make_cli()
    # Decompose 阶段 LLM 响应
    parser.client.set_response("""```json
[
  {"table":"pet","sheet":"Pet","action":"add",
   "fields":{"名称":"饕餮"},"produces":"new_pet","consumes":{}},
  {"table":"pet_evolve","sheet":"PetEvolveData","action":"add",
   "fields":{"宠物id":"<new_pet>"},"produces":"","consumes":{"宠物id":"new_pet"}}
]
```""")
    # Locator 阶段(规则路径即可,不触发 LLM)
    la = LocatorAgent(parser=parser, thinking_sink=lambda p, d: None, cli=cli)
    da = DecomposeAgent(parser=parser, thinking_sink=lambda p, d: None, cli=cli)
    va = ValidatorAgent(parser=parser, thinking_sink=lambda p, d: None, cli=cli)

    text = "灵兽饕餮进化成饕餮王"
    # 1. Locator
    lr = la.locate(text)
    assert lr.is_cross_table, f"应触发跨表,实际 cross={lr.is_cross_table}"
    assert len(lr.candidates) >= 2, f"应 ≥2 候选,实际 {len(lr.candidates)}"
    # 2. Decompose
    intents = da.decompose(text, lr)
    assert len(intents) == 2, f"应产 2 条,实际 {len(intents)}"
    # 3. Validator(归一 produces 标签)
    res = va.validate(intents, lr)
    assert res["intents"][0].produces == "new_pet_id", \
        f"归一失败: {res['intents'][0].produces}"
    # 消费占位符应已替换
    assert res["intents"][1].fields["宠物id"] == "<new_pet_id>", \
        f"消费占位符错: {res['intents'][1].fields}"
    print(f"PASS full_agent_chain: Locator({len(lr.candidates)}候选)→Decompose(2 op)→Validator(归一+校验)")


# ── 样例11: 单表不触发跨表链(回归保护)──────────────────────
def test_single_table_no_cross():
    """单表输入不应触发 DecomposeAgent(应走单表 ai_pipeline_merge 路径)。"""
    parser = MockParser()
    cli = make_cli()
    la = LocatorAgent(parser=parser, thinking_sink=lambda p, d: None, cli=cli)
    # 单表:只查不改
    lr = la.locate("查询灵兽饕餮")
    assert not lr.is_cross_table, f"单表查询不应跨表: cross={lr.is_cross_table}"
    _rule = [c for c in lr.candidates if getattr(c, "level", "") != "column_extract"]
    assert len(_rule) <= 1, f"单表规则候选应 ≤1（参考候选不计）: {len(_rule)}"
    print("PASS single_table_no_cross: 单表查询不误触发跨表链")


def main():
    tests = [
        test_pet_evolve_chain,
        test_mail_cross_sheet,
        test_item_single_table,
        test_quest_npc_dag,
        test_over_produce_suppress,
        test_produce_label_normalize,
        test_consume_broken_link,
        test_malformed_llm_fallback,
        test_zero_llm_fallback_empty_response,
        test_action_passthrough,
        test_full_agent_chain,
        test_single_table_no_cross,
    ]
    passed = 0
    failed = 0
    for t in tests:
        try:
            t()
            passed += 1
        except Exception as e:
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{'='*50}")
    print(f"结果: {passed} PASS / {failed} FAIL / {len(tests)} 总")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

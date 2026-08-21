"""LLMSubAgent 单测:mock _call_llm 验证 fragment 产出 + prompt 构造。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.excel.subagent.llm_agent import LLMSubAgent
from agent.excel.pipeline.types import DocIntent


class MockLLMSubAgent(LLMSubAgent):
    """mock _call_llm 返回固定 dict。"""
    def __init__(self, name, llm_response, parser=None):
        super().__init__(name, parser=parser)
        self._llm_response = llm_response

    def _call_llm(self, prompt, timeout=90):
        self.last_prompt = prompt
        return self._llm_response


def test_llm_sub_agent_produces_fragment():
    """LLM 返回有效 dict → 产出 fragment。"""
    llm_resp = {
        "sql_or_ops": [{"action": "add", "table_hint": "pet",
                        "sheet_hint": "Pet", "fields": {"灵兽id": 1, "名称": "t"}}],
        "produces": "<pet_new>",
        "references": [],
        "target_table": "pet",
        "target_sheet": "Pet",
    }
    sa = MockLLMSubAgent("test", llm_resp)
    frag = sa.run("生成 pet", context={})
    assert frag.ok
    assert frag.agent_name == "test"
    assert len(frag.sql_or_ops) == 1
    assert frag.produces == "<pet_new>"
    assert frag.target_table == "pet"


def test_llm_sub_agent_llm_none_fails():
    """LLM 返回 None → fragment ok=False。"""
    sa = MockLLMSubAgent("test", None)
    frag = sa.run("任务", context={})
    assert not frag.ok


def test_llm_sub_agent_missing_sql_or_ops_fails():
    """LLM 返回 dict 无 sql_or_ops → fragment ok=False。"""
    sa = MockLLMSubAgent("test", {"produces": "<x>"})
    frag = sa.run("任务", context={})
    assert not frag.ok


def test_build_prompt_injects_symbol_map():
    """prompt 含符号映射表 + 分区 + 输出格式说明。"""
    sa = MockLLMSubAgent("test", None)
    doc = DocIntent(source_path="t.md", symbol_map={"<pet_new>": "灵兽id", "<dlg_1>": "对话id"})
    ctx = {"doc_intent": doc, "partitions": {"npc": "pve_combat_npc:PveCombatNpc"}}
    prompt = sa._build_prompt("生成 pet 片段", ctx)
    assert "生成 pet 片段" in prompt
    assert "<pet_new>: 灵兽id" in prompt
    assert "<dlg_1>: 对话id" in prompt
    assert "pve_combat_npc:PveCombatNpc" in prompt
    assert "sql_or_ops" in prompt
    assert "produces" in prompt


def test_build_prompt_empty_context():
    """空 context → prompt 仅含任务 + 输出格式(不报错)。"""
    sa = MockLLMSubAgent("test", None)
    prompt = sa._build_prompt("任务X", {})
    assert "任务X" in prompt
    assert "sql_or_ops" in prompt


if __name__ == "__main__":
    test_llm_sub_agent_produces_fragment()
    print("test_llm_sub_agent_produces_fragment: PASS")
    test_llm_sub_agent_llm_none_fails()
    print("test_llm_sub_agent_llm_none_fails: PASS")
    test_llm_sub_agent_missing_sql_or_ops_fails()
    print("test_llm_sub_agent_missing_sql_or_ops_fails: PASS")
    test_build_prompt_injects_symbol_map()
    print("test_build_prompt_injects_symbol_map: PASS")
    test_build_prompt_empty_context()
    print("test_build_prompt_empty_context: PASS")
    print("\n全部通过 ✓")

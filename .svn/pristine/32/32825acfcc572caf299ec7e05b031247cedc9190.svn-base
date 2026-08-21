"""SubAgent 角色细化单测:验证各角色 prompt 注入对应表 schema。"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from agent.excel.cli_interface import StubCodeMakerCLI
from agent.excel.subagent.roles import (
    DialogFillAgent, ItemNpcFillAgent, ButterflyEventFillAgent, GenericFillAgent,
)
from agent.excel.pipeline.types import DocIntent


RES = Path(r"c:\Users\wuzhixian\Desktop\kk\resources")


def test_dialog_agent_injects_interaction_schema():
    cli = StubCodeMakerCLI(RES)
    sa = DialogFillAgent(cli=cli)
    doc = DocIntent(source_path="t.md", symbol_map={"<dlg>": "对话"})
    prompt = sa._build_prompt("任务", {"doc_intent": doc})
    assert "对话配表专家" in prompt
    assert "interaction" in prompt.lower() or "Interaction" in prompt
    assert "InteractionConv" in prompt
    assert "<dlg>: 对话" in prompt


def test_item_npc_agent_injects_multi_schema():
    cli = StubCodeMakerCLI(RES)
    sa = ItemNpcFillAgent(cli=cli)
    doc = DocIntent(source_path="t.md", symbol_map={"<npc>": "NPC"})
    prompt = sa._build_prompt("任务", {"doc_intent": doc})
    assert "NPC+道具+显隐+奇遇" in prompt
    assert "entity_prefab" in prompt.lower()
    assert "spawn_world_entity" in prompt.lower()
    assert "item" in prompt.lower()


def test_butterfly_agent_injects_task_schema():
    cli = StubCodeMakerCLI(RES)
    sa = ButterflyEventFillAgent(cli=cli)
    doc = DocIntent(source_path="t.md")
    prompt = sa._build_prompt("任务", {"doc_intent": doc})
    assert "任务主表族" in prompt
    assert "task" in prompt.lower()


def test_generic_agent_no_schema():
    cli = StubCodeMakerCLI(RES)
    sa = GenericFillAgent(cli=cli)
    prompt = sa._build_prompt("任务X", {"doc_intent": DocIntent(source_path="t.md")})
    assert "任务X" in prompt
    assert "sql_or_ops" in prompt  # 继承 LLMSubAgent 输出格式


def test_agent_no_cli_skips_schema():
    """无 cli 时不注入 schema,不报错。"""
    sa = DialogFillAgent(cli=None)
    prompt = sa._build_prompt("任务", {"doc_intent": DocIntent(source_path="t.md")})
    assert "对话配表专家" in prompt
    assert "sql_or_ops" in prompt


if __name__ == "__main__":
    test_dialog_agent_injects_interaction_schema()
    print("test_dialog_agent: PASS")
    test_item_npc_agent_injects_multi_schema()
    print("test_item_npc_agent: PASS")
    test_butterfly_agent_injects_task_schema()
    print("test_butterfly_agent: PASS")
    test_generic_agent_no_schema()
    print("test_generic_agent: PASS")
    test_agent_no_cli_skips_schema()
    print("test_agent_no_cli: PASS")
    print("\n全部通过 ✓")

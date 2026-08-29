from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.core.cross_table_splitter import CrossTableIntentSplitter


def test_pet_natural_evolve_chain_fallback_builds_relation():
    text = (
        "新增灵兽饕餮，灵兽model_id是1020，灵兽类型是神兽，"
        "并配置一条进化链：饕餮进化成饕餮王，进化等级是1。"
    )

    intents = CrossTableIntentSplitter().split(text)

    assert any(it.table_hint == "pet" and it.produces == "new_pet_id" for it in intents)
    assert any(it.table_hint == "pet" and it.produces == "new_pet_2_id" for it in intents)
    evolve = [it for it in intents if it.table_hint == "pet_evolve"]
    assert len(evolve) == 1
    assert evolve[0].fields["pet_id"] == "<new_pet_id>"
    assert evolve[0].fields["evolved_pet_id"] == "<new_pet_2_id>"
    assert evolve[0].fields["level"] == "1"

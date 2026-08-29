from __future__ import annotations

from pathlib import Path

from agent.excel.locator.alias_mapping import AliasMapping


def test_lookup_in_text_returns_duplicate_alias_targets():
    am = AliasMapping(
        mapping={"神通": "ability.xlsx"},
        multi_mapping={"神通": ["ability.xlsx", "school/school_ability.xlsx"]},
    )

    hits = am.lookup_in_text("新增一个门派神通")

    assert ("神通", "ability.xlsx") in hits
    assert ("神通", "school/school_ability.xlsx") in hits


def test_files_for_stem_checks_multi_mapping():
    am = AliasMapping(
        mapping={"神通": "ability.xlsx"},
        multi_mapping={"神通": ["ability.xlsx", "school/school_ability.xlsx"]},
    )

    assert am.files_for_stem("school_ability") == ["神通"]

from __future__ import annotations

from agent.excel.core.pipeline.schema_mapper import (
    build_schema_field_map,
    remap_fields_to_schema,
)


def test_build_schema_field_map_accepts_row1_row2_and_dot_tail():
    mapping = build_schema_field_map(
        ["模板ID", "标题", "体力资质"],
        ["template_id:int", "title:string", "aptitude_base.StrPotCon:int"],
    )

    assert mapping["模板id"] == "template_id"
    assert mapping["templateid"] == "template_id"
    assert mapping["标题"] == "title"
    assert mapping["title"] == "title"
    assert mapping["strpotcon"] == "aptitude_base.StrPotCon"


def test_remap_fields_to_schema_reports_renames_and_unmapped():
    fields, report = remap_fields_to_schema(
        {"模板ID": 30020, "title": "公告", "unknown_col": 1},
        ["模板ID", "标题"],
        ["template_id:int", "title:string"],
    )

    assert fields == {
        "template_id": 30020,
        "title": "公告",
        "unknown_col": 1,
    }
    assert report["renames"] == {"模板ID": "template_id"}
    assert report["unmapped"] == ["unknown_col"]
    assert report["collisions"] == []

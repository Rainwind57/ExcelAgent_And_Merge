"""建议4 schema-first 字段划分单测（纯函数，0 LLM，确定性）。"""
from agent.excel.core.pipeline.field_partition import partition_fields_by_schema


HEADERS = ["reward_id:int", "名称:string", "数量:int"]


def test_known_fields_by_normalized_header():
    known, unknown = partition_fields_by_schema(
        {"reward_id": 1, "名称": "包A", "数量": 5}, HEADERS)
    assert set(known) == {"reward_id", "名称", "数量"}
    assert unknown == {}


def test_unknown_field_goes_to_unknown():
    known, unknown = partition_fields_by_schema(
        {"reward_id": 1, "图标路径": "Icon_x"}, HEADERS)
    assert "reward_id" in known
    assert "图标路径" in unknown


def test_header_suffix_normalized_match():
    known, unknown = partition_fields_by_schema({"名称": "x"}, HEADERS)
    assert "名称" in known and not unknown


def test_dotted_key_treated_known():
    known, unknown = partition_fields_by_schema(
        {"effect.data.3006.conv_id": "<x>"}, HEADERS)
    assert "effect.data.3006.conv_id" in known
    assert unknown == {}


def test_pure_digit_key_is_unknown():
    known, unknown = partition_fields_by_schema({"42": "碎片"}, HEADERS)
    assert "42" in unknown
    assert not known


def test_empty_key_is_unknown():
    known, unknown = partition_fields_by_schema({"": "v", "  ": "w"}, HEADERS)
    assert set(unknown) == {"", "  "}


def test_empty_fields():
    known, unknown = partition_fields_by_schema({}, HEADERS)
    assert known == {} and unknown == {}

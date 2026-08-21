"""schema_bundle 单测（§2.2 lazy schema 拉取 + data_getter 构造器）。

测试 build_data_getter + helpers（_stem_to_path/_read_existing_values/_rows_to_dicts）。
mock agent + cli（不依赖真实表文件）。

运行: python -m pytest server/tests/test_schema_bundle.py -v
"""
from __future__ import annotations

import os
import sys
import types
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.parser.nl_parser import NLIntent
from agent.excel.schema_bundle import (
    _read_existing_values, _rows_to_dicts, _stem_to_path, build_data_getter,
)


def _ns(**kw):
    return types.SimpleNamespace(**kw)


# ── _stem_to_path ─────────────────────────────────────────────


class TestStemToPath:
    def test_via_resolver_resolve(self):
        agent = _ns(_table_resolver=_ns(resolve=lambda s: f"/res/{s}.xlsx"), cli=None)
        p = _stem_to_path(agent, "pet")
        assert p is not None
        assert str(p).endswith("pet.xlsx")

    def test_via_resolver_find_path(self):
        agent = _ns(resolver=_ns(find_path=lambda s: f"/fp/{s}.xlsx"), cli=None)
        p = _stem_to_path(agent, "pet")
        assert p is not None and str(p).endswith("pet.xlsx")

    def test_via_cli_list_tables(self):
        t = _ns(stem="pet", path="/tmp/pet.xlsx")
        agent = _ns(cli=_ns(list_tables=lambda: [t]))
        p = _stem_to_path(agent, "pet")
        assert p is not None and str(p).endswith("pet.xlsx")

    def test_cli_list_tables_no_match(self):
        t = _ns(stem="other", path="/tmp/other.xlsx")
        agent = _ns(cli=_ns(list_tables=lambda: [t]))
        assert _stem_to_path(agent, "pet") is None

    def test_empty_stem(self):
        assert _stem_to_path(_ns(), "") is None

    def test_no_resolver_no_cli(self):
        assert _stem_to_path(_ns(), "pet") is None

    def test_resolver_exception_fallback_cli(self):
        def _boom(s):
            raise RuntimeError("boom")
        t = _ns(stem="pet", path="/tmp/pet.xlsx")
        agent = _ns(_table_resolver=_ns(resolve=_boom),
                    cli=_ns(list_tables=lambda: [t]))
        p = _stem_to_path(agent, "pet")
        assert p is not None and str(p).endswith("pet.xlsx")


# ── _read_existing_values ─────────────────────────────────────


class TestReadExistingValues:
    def test_existing_values_computed(self):
        headers = ["pet_id", "名称"]
        rows = [[1, "朱雀"], [2, "白虎"], [1, "朱雀"]]  # pet_id 1 重复
        cli = _ns(read_sheet=lambda p, s: rows)
        existing = _read_existing_values(cli, "pet.xlsx", "Pet", headers)
        assert existing["pet_id"] == {1, 2}  # rows 含 1,2,1 → {1,2}
        assert existing["名称"] == {"朱雀", "白虎"}

    def test_empty_rows(self):
        cli = _ns(read_sheet=lambda p, s: [])
        assert _read_existing_values(cli, "p", "s", ["x"]) == {}

    def test_none_values_skipped(self):
        headers = ["col"]
        rows = [[1], [None], [2]]
        cli = _ns(read_sheet=lambda p, s: rows)
        existing = _read_existing_values(cli, "p", "s", headers)
        assert existing["col"] == {1, 2}

    def test_empty_string_skipped(self):
        headers = ["col"]
        rows = [["a"], [""], ["b"]]
        cli = _ns(read_sheet=lambda p, s: rows)
        existing = _read_existing_values(cli, "p", "s", headers)
        assert existing["col"] == {"a", "b"}

    def test_no_cli(self):
        assert _read_existing_values(None, "p", "s", ["x"]) == {}

    def test_no_path(self):
        cli = _ns(read_sheet=lambda p, s: [])
        assert _read_existing_values(cli, None, "s", ["x"]) == {}

    def test_no_sheet(self):
        cli = _ns(read_sheet=lambda p, s: [])
        assert _read_existing_values(cli, "p", "", ["x"]) == {}

    def test_read_sheet_exception(self):
        def _boom(p, s):
            raise RuntimeError("boom")
        cli = _ns(read_sheet=_boom)
        assert _read_existing_values(cli, "p", "s", ["x"]) == {}

    def test_col_type_suffix_stripped(self):
        """表头含 :type 后缀 → lowered 去后缀作 key。"""
        headers = ["pet_id:int", "名称:string"]
        rows = [[1, "朱雀"]]
        cli = _ns(read_sheet=lambda p, s: rows)
        existing = _read_existing_values(cli, "p", "s", headers)
        assert "pet_id" in existing  # 去 :int
        assert "名称" in existing


# ── _rows_to_dicts ─────────────────────────────────────────────


class TestRowsToDicts:
    def test_basic(self):
        headers = ["pet_id", "名称"]
        rows = [[1, "朱雀"], [2, "白虎"]]
        out = _rows_to_dicts(headers, rows)
        assert out == [{"pet_id": 1, "名称": "朱雀"},
                       {"pet_id": 2, "名称": "白虎"}]

    def test_empty_headers(self):
        assert _rows_to_dicts([], [[1]]) == []

    def test_empty_rows(self):
        assert _rows_to_dicts(["x"], []) == []

    def test_row_shorter_than_headers(self):
        """行长度 < headers → 仅填有值的列。"""
        headers = ["a", "b", "c"]
        rows = [[1, 2]]  # 缺 c
        out = _rows_to_dicts(headers, rows)
        assert out == [{"a": 1, "b": 2}]

    def test_col_type_suffix_stripped(self):
        headers = ["pet_id:int"]
        rows = [[1]]
        out = _rows_to_dicts(headers, rows)
        assert out == [{"pet_id": 1}]


# ── build_data_getter ─────────────────────────────────────────


class TestBuildDataGetter:
    def _make_agent(self, headers, rows, stem="pet", path="/tmp/pet.xlsx"):
        t = _ns(stem=stem, path=path)
        return _ns(cli=_ns(
            list_tables=lambda: [t],
            read_header=lambda p, s: headers,
            read_sheet=lambda p, s: rows,
        ))

    def test_data_getter_returns_full_dict(self):
        headers = ["pet_id", "名称"]
        rows = [[1, "朱雀"]]
        agent = self._make_agent(headers, rows)
        dg = build_data_getter(agent)
        intent = NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                         raw="x", extras={"fields": {"pet_id": 1}})
        data = dg(intent)
        assert data["stem"] == "pet"
        assert data["sheet"] == "Pet"
        assert data["path"] is not None
        assert "pet_id" in data["existing_values"]
        assert data["existing_values"]["pet_id"] == {1}
        assert len(data["result_rows"]) == 1
        assert data["result_rows"][0]["pet_id"] == 1
        assert data["cli"] is agent.cli

    def test_data_getter_no_path_returns_empty(self):
        agent = _ns(cli=_ns(list_tables=lambda: [],
                          read_header=lambda p, s: [],
                          read_sheet=lambda p, s: []))
        dg = build_data_getter(agent)
        intent = NLIntent(action="set", table_hint="unknown", sheet_hint="X",
                         raw="x", extras={"fields": {}})
        data = dg(intent)
        assert data["path"] is None
        assert data["existing_values"] == {}
        assert data["result_rows"] == []

    def test_data_getter_no_cli_returns_empty(self):
        agent = _ns(cli=None)
        dg = build_data_getter(agent)
        intent = NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                         raw="x", extras={"fields": {}})
        data = dg(intent)
        assert data["path"] is None
        assert data["existing_values"] == {}
        assert data["result_rows"] == []

    def test_data_getter_read_exception_degrades(self):
        def _boom_header(p, s):
            raise RuntimeError("header boom")
        t = _ns(stem="pet", path="/tmp/pet.xlsx")
        agent = _ns(cli=_ns(list_tables=lambda: [t],
                          read_header=_boom_header,
                          read_sheet=lambda p, s: []))
        dg = build_data_getter(agent)
        intent = NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                         raw="x", extras={"fields": {}})
        data = dg(intent)
        # 异常降级,不崩
        assert data["existing_values"] == {}
        assert data["result_rows"] == []

    def test_data_getter_lazy_per_intent(self):
        """data_getter 按需 lazy（每 intent 调用一次读表）。"""
        headers = ["pet_id"]
        rows = [[1]]
        agent = self._make_agent(headers, rows)
        read_calls = [0]
        orig_read_sheet = agent.cli.read_sheet

        def _count(p, s):
            read_calls[0] += 1
            return orig_read_sheet(p, s)
        agent.cli.read_sheet = _count
        dg = build_data_getter(agent)
        it1 = NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                      raw="x", extras={"fields": {}})
        it2 = NLIntent(action="set", table_hint="pet", sheet_hint="Pet",
                      raw="y", extras={"fields": {}})
        dg(it1)
        dg(it2)
        assert read_calls[0] == 2  # 每 intent read_sheet 1 次（复用 rows 给 existing+result）


# ── §2.2 HTTP 化待后续（文档化）──────────────────────────────


class TestSchemaBundleHttpPending:
    """§2.2 HTTP schema_bundle 待 excel-agent 独立服务部署时做。

    现状同进程 cli 直读更快（HTTP 自调用浪费）。R21 HTTP API 已落地
    （routers/tables.py:68 ?include_columns=1）,独立部署时可改 build_data_getter
    内部走 HTTP GET 替代 cli.read_header/read_sheet。
    """

    def test_module_docstring_documents_http_pending(self):
        from agent.excel import schema_bundle
        doc = schema_bundle.__doc__ or ""
        assert "HTTP" in doc
        assert "独立服务" in doc or "同进程" in doc

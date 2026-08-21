"""cascade_planner 单元测试 —— SET 改主键级联影响推理。

验证 #1 P0 数据风险防护能力（baseline 无此能力）：
- table_relations.json 关系图加载
- _find_fk_columns_in_relation 精确外键查询
- _stem_matches_relation_to 被引用方判定
- preview_cascade_set_pk 预览级联影响（精确关系图 + 语义回退）
- apply_cascade_set_pk 执行级联更新
- 异常降级（preview 失败不阻断）
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.excel.cascade_planner import (
    _load_table_relations, _find_fk_columns_in_relation,
    _stem_matches_relation_to, preview_cascade_set_pk, apply_cascade_set_pk,
)


# ── 关系图加载 ──────────────────────────────────────────

def test_table_relations_loaded():
    """table_relations.json 加载成功，含 foreign_key 关系。"""
    rels = _load_table_relations()
    assert len(rels) >= 15  # 实际 16~17
    assert all(r.get("relation_type") == "foreign_key" for r in rels)


# ── 外键查询 ────────────────────────────────────────────

def test_find_fk_refs_pet():
    """pet.灵兽id 被 pet_evolve 的 宠物id + 进化后的灵兽ID 引用。"""
    fk = _find_fk_columns_in_relation("pet", "灵兽id")
    assert len(fk) == 2
    paths = {(f[0], f[1], f[2]) for f in fk}
    assert ("pet/pet_evolve.xlsx", "PetEvolveData", "宠物id") in paths
    assert ("pet/pet_evolve.xlsx", "PetEvolveData", "进化后的灵兽ID") in paths


def test_find_fk_refs_fabao():
    """fabao.法宝id 被 bilingual_sample.fabao_id 引用。"""
    fk = _find_fk_columns_in_relation("fabao", "法宝id")
    assert len(fk) == 1
    assert fk[0] == ("bilingual_sample/bilingual_sample.xlsx", "Fabao", "fabao_id")


def test_find_fk_refs_nonexistent_table():
    """不存在的表返回空列表。"""
    fk = _find_fk_columns_in_relation("nonexistent_table_xyz", "id")
    assert fk == []


def test_find_fk_refs_nonexistent_column():
    """存在的表但无引用列返回空。"""
    fk = _find_fk_columns_in_relation("pet", "不存在的列xyz")
    assert fk == []


# ── 被引用方判定 ────────────────────────────────────────

def test_stem_matches_relation_to_pet():
    """关系条 to pet/pet.xlsx.灵兽id 匹配 stem=pet col=灵兽id。"""
    rel = {"to_path": "pet/pet.xlsx", "to_sheet": "Pet", "to_column": "灵兽id"}
    assert _stem_matches_relation_to("pet", "灵兽id", rel) is True


def test_stem_matches_relation_to_fabao():
    """fabao.法宝id 匹配。"""
    rel = {"to_path": "fabao/fabao.xlsx", "to_sheet": "Fabao", "to_column": "法宝id"}
    assert _stem_matches_relation_to("fabao", "法宝id", rel) is True


def test_stem_matches_wrong_stem():
    """stem 不匹配返回 False。"""
    rel = {"to_path": "pet/pet.xlsx", "to_sheet": "Pet", "to_column": "灵兽id"}
    assert _stem_matches_relation_to("fabao", "灵兽id", rel) is False


def test_stem_matches_wrong_column():
    """列名不匹配返回 False。"""
    rel = {"to_path": "pet/pet.xlsx", "to_sheet": "Pet", "to_column": "灵兽id"}
    assert _stem_matches_relation_to("pet", "攻击力", rel) is False


def test_stem_matches_underscore_normalization():
    """列名带下划线/空格/大小写差异应归一匹配。"""
    rel = {"to_path": "item/item.xlsx", "to_sheet": "Item", "to_column": "item_id"}
    assert _stem_matches_relation_to("item", "Item ID", rel) is True
    assert _stem_matches_relation_to("item", "itemid", rel) is True


# ── preview_cascade_set_pk mock 场景 ─────────────────────

class _MockWS:
    """模拟 openpyxl worksheet：支持 cell(r, c).value + _rows 存储。"""
    def __init__(self, rows):
        self._rows = rows  # rows[0]=header, rows[1+]=data

    def cell(self, r, c):
        cell = MagicMock()
        if r - 1 < len(self._rows) and c - 1 < len(self._rows[r - 1]):
            cell.value = self._rows[r - 1][c - 1]
        else:
            cell.value = None
        return cell


class _MockCLI:
    """模拟 cli：内存文件系统 + 所需方法。"""
    def __init__(self, files):
        # files: {path_str: {sheet: _MockWS}}
        self._files = files
        self.data_start_row = 2

    @staticmethod
    def _key(path) -> str:
        """归一路径 key：Windows 反斜杠 → 正斜杠，匹配 _files key。"""
        return str(path).replace("\\", "/")

    def _load(self, path):
        return self._files.get(self._key(path), {})

    def _last_data_row(self, ws, start):
        return len(ws._rows) if hasattr(ws, "_rows") else start

    def get_sheets(self, path):
        return list(self._files.get(self._key(path), {}).keys())

    def read_header(self, path, sheet):
        return self._files[self._key(path)][sheet]._rows[0]

    def read_cell(self, path, sheet, row, col):
        r = MagicMock()
        r.ok = True
        r.data = self._files[self._key(path)][sheet].cell(row, col).value
        return r

    def write_cell(self, path, sheet, row, col, value):
        ws = self._files[self._key(path)][sheet]
        while row - 1 >= len(ws._rows):
            ws._rows.append([None] * col)
        while col - 1 >= len(ws._rows[row - 1]):
            ws._rows[row - 1].append(None)
        ws._rows[row - 1][col - 1] = value
        r = MagicMock()
        r.ok = True
        return r

    def read_sheet(self, path, sheet):
        return self._files[self._key(path)][sheet]._rows


def _build_pet_cascade_scenario():
    """构造 pet + pet_evolve 场景：改 pet.Pet.灵兽id 1→999。

    pet/pet.xlsx Pet sheet:
        header: [灵兽id, 灵兽名称, 攻击力]
        data:   row2=[1, 朱雀, 100], row3=[2, 白虎, 200]
    pet/pet_evolve.xlsx PetEvolveData sheet:
        header: [进化id, 宠物id, 进化后的灵兽ID]
        data:   row2=[101, 1, 2], row3=[102, 1, 3], row4=[103, 2, 1]
    改 pet.Pet row2 灵兽id 1→999:
        - pet_evolve 宠物id==1 的行: row2, row3
        - pet_evolve 进化后的灵兽ID==1 的行: row4
    """
    pet_path = "pet/pet.xlsx"
    evolve_path = "pet/pet_evolve.xlsx"
    files = {
        pet_path: {
            "Pet": _MockWS([
                ["灵兽id", "灵兽名称", "攻击力"],
                [1, "朱雀", 100],
                [2, "白虎", 200],
            ]),
        },
        evolve_path: {
            "PetEvolveData": _MockWS([
                ["进化id", "宠物id", "进化后的灵兽ID"],
                [101, 1, 2],
                [102, 1, 3],
                [103, 2, 1],
            ]),
        },
    }
    return _MockCLI(files), pet_path, evolve_path


def test_preview_cascade_set_pk_pet(monkeypatch):
    """改 pet.Pet.灵兽id 1→999 预览：3 处引用行受影响（2 宠物id + 1 进化后灵兽ID）。"""
    cli, pet_path, evolve_path = _build_pet_cascade_scenario()

    # mock _resolve_workspace_path 返回 mock 路径
    monkeypatch.setattr(
        "agent.excel.cascade_planner._resolve_workspace_path",
        lambda rel: Path(rel),
    )
    # mock _sheet_exists 用 cli
    monkeypatch.setattr(
        "agent.excel.cascade_planner._sheet_exists",
        lambda c, p, s: s in cli._files.get(str(p).replace("\\", "/"), {}),
    )

    preview = preview_cascade_set_pk(
        cli, Path(pet_path), "Pet", 2, "灵兽id", 1, 999, "pet")

    assert preview["count"] == 3
    assert preview["confidence"] == "high"  # 精确关系图命中
    affected_sheets = [a["sheet"] for a in preview["affected"]]
    assert all(s == "PetEvolveData" for s in affected_sheets)
    # 验证 old_value=1, suggested_value=999
    for a in preview["affected"]:
        assert a["old_value"] == 1
        assert a["suggested_value"] == 999


def test_preview_cascade_set_pk_no_refs():
    """改无引用的 PK → count=0, confidence=none。"""
    cli, pet_path, _ = _build_pet_cascade_scenario()
    # 改白虎 id=2（无 evolve 引用 id==2 的宠物id... 实际 row4 进化后灵兽ID==1，row3 宠物id==1）
    # row2/3 宠物id==1, row4 进化后==1。改 id=2：宠物id==2 无，进化后==2 有 row2
    # 所以改 2 仍有 1 处引用。改一个确实无引用的：用 id=999（不存在）
    from unittest.mock import patch
    with patch("agent.excel.cascade_planner._resolve_workspace_path", lambda rel: Path(rel)), \
         patch("agent.excel.cascade_planner._sheet_exists", lambda c, p, s: s in cli._files.get(str(p).replace("\\", "/"), {})):
        preview = preview_cascade_set_pk(
            cli, Path(pet_path), "Pet", 2, "灵兽id", 999, 1000, "pet")
    # old_val=999 在 evolve 里无匹配 → count=0
    assert preview["count"] == 0
    assert preview["confidence"] == "none"


def test_apply_cascade_set_pk_executes_updates(monkeypatch):
    """apply 执行级联更新：引用行的外键值被改为新值。"""
    cli, pet_path, evolve_path = _build_pet_cascade_scenario()
    monkeypatch.setattr(
        "agent.excel.cascade_planner._resolve_workspace_path",
        lambda rel: Path(rel),
    )
    monkeypatch.setattr(
        "agent.excel.cascade_planner._sheet_exists",
        lambda c, p, s: s in cli._files.get(str(p).replace("\\", "/"), {}),
    )

    preview = preview_cascade_set_pk(
        cli, Path(pet_path), "Pet", 2, "灵兽id", 1, 999, "pet")
    assert preview["count"] == 3

    # 执行级联更新
    results = apply_cascade_set_pk(cli, preview["affected"])
    assert len(results) == 3
    assert all(r["ok"] for r in results)

    # 验证引用行外键值已改为 999
    evolve_ws = cli._files[evolve_path]["PetEvolveData"]
    # row2 宠物id (col2) 应=999
    assert evolve_ws.cell(2, 2).value == 999
    # row3 宠物id (col2) 应=999
    assert evolve_ws.cell(3, 2).value == 999
    # row4 进化后的灵兽ID (col3) 应=999
    assert evolve_ws.cell(4, 3).value == 999


def test_apply_cascade_set_pk_handles_write_failure():
    """write_cell 失败 → result ok=False, 不崩。"""
    cli = MagicMock()
    cli.write_cell.side_effect = RuntimeError("write boom")
    affected = [{"path": "pet/pet.xlsx", "sheet": "Pet", "row": 2, "field": "灵兽id",
                 "old_value": 1, "suggested_value": 999}]
    results = apply_cascade_set_pk(cli, affected)
    assert len(results) == 1
    assert results[0]["ok"] is False
    assert "write boom" in results[0]["error"]


def test_preview_cascade_set_pk_exception_degrades():
    """preview 异常 → 降级返回 none confidence，不阻断。"""
    cli = MagicMock()
    cli.read_header.side_effect = RuntimeError("read boom")
    # _load 返回 {} 触发后续异常路径
    cli._load.return_value = {}
    cli.get_sheets.return_value = []
    cli._last_data_row.return_value = 2
    cli.data_start_row = 2
    preview = preview_cascade_set_pk(
        cli, Path("pet/pet.xlsx"), "Pet", 2, "灵兽id", 1, 999, "pet")
    assert preview["count"] == 0
    assert preview["confidence"] == "none"


# ── 跨表级联影响图完整性 ────────────────────────────────

def test_fk_graph_covers_core_tables():
    """核心表（pet/fabao/interaction/item）的外键引用都能查到。"""
    # pet 被引用
    assert len(_find_fk_columns_in_relation("pet", "灵兽id")) >= 2
    # fabao 被引用
    assert len(_find_fk_columns_in_relation("fabao", "法宝id")) >= 1
    # interaction 被引用（InteractionConv/Option）
    assert len(_find_fk_columns_in_relation("interaction", "编号")) >= 1

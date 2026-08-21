"""级联规则解析器：基于 table_relations.json 关系图谱提供 add 级联查询。

数据驱动替代 cross_table_splitter 的硬编码级联：splitter 的 _build_* 硬编码
覆盖 task_chain 已知模式（NPC/item/pet/mail/quest），本模块为未命中模式
提供关系图谱驱动的级联提示，供 LLM 上下文增强与未来自动级联扩展。

数据源：table_relations.json（RelationGraph.load），不依赖 cascade_rules.yaml
的 cascade_on_add（该文件由 skill_updater 自动生成，手改易被覆盖）。

查询接口:
  get_add_dependencies(stem): add 该表时外键依赖的目标表（引用完整性）
      如 add pet_evolve 依赖 pet（宠物id 引用 灵兽id）
  get_referencing_tables(stem): 引用该表的子表（add 后可能需级联建行）
      如 add entity_prefab 后 spawn_world_entity 可能引用它
  get_cascade_hints(stem): 合并提示，供 LLM 上下文注入

task_chain 用例对应关系:
  用例1 entity_prefab → interaction(depends) + spawn_world_entity(referenced_by)
  用例5 item → reward(depends, via Chest.reward ID)
  用例8 pet → pet_evolve(referenced_by, 进化表引用灵兽id)
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from .table_relations import RelationGraph


# O11 stem → results 缓存(进程内,RelationGraph.load 已 mtime 缓存,
# 此层再按 stem 索引避免每调用重扫 relations)。
_dep_cache: dict[str, list[dict]] = {}
_ref_cache: dict[str, list[dict]] = {}


def _stem_of(path: str) -> str:
    """relation path → stem：'entity_prefab/entity_prefab.xlsx' → 'entity_prefab'。"""
    return Path(path.replace("\\", "/")).stem


def get_add_dependencies(stem: str) -> list[dict]:
    """add stem 表时，该表外键引用的目标表列表。

    返回 [{target_stem, source_col, target_col, sheet}]。
    add stem 行时这些目标表需存在对应行（引用完整性约束）。
    同 (target, col) 去重。O11:stem 级缓存。
    """
    if stem in _dep_cache:
        return _dep_cache[stem]
    g = RelationGraph.load()
    out: list[dict] = []
    seen: set = set()
    for r in g.relations:
        if _stem_of(r.from_path) != stem:
            continue
        to_stem = _stem_of(r.to_path)
        if to_stem == stem:
            continue
        key = (to_stem, r.from_column, r.to_column)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "target_stem": to_stem,
            "source_col": r.from_column,
            "target_col": r.to_column,
            "sheet": r.to_sheet,
        })
    _dep_cache[stem] = out
    return out


def get_referencing_tables(stem: str) -> list[dict]:
    """引用 stem 表作为外键目标的子表列表。

    返回 [{source_stem, source_col, target_col, sheet}]。
    add stem 行后，这些子表可能需级联建行引用新 id（如 add entity_prefab 后
    spawn_world_entity 引用它）。供 LLM 判断是否需同步建。O11:stem 级缓存。
    """
    if stem in _ref_cache:
        return _ref_cache[stem]
    g = RelationGraph.load()
    out: list[dict] = []
    seen: set = set()
    for r in g.relations:
        if _stem_of(r.to_path) != stem:
            continue
        from_stem = _stem_of(r.from_path)
        if from_stem == stem:
            continue
        key = (from_stem, r.from_column, r.to_column)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "source_stem": from_stem,
            "source_col": r.from_column,
            "target_col": r.to_column,
            "sheet": r.from_sheet,
        })
    _ref_cache[stem] = out
    return out


def get_cascade_hints(stem: str) -> dict:
    """合并级联提示：add stem 时需关注的所有关联表。

    返回 {depends_on: [...], referenced_by: [...]}，供 LLM 上下文注入。
    两列表明：add 此表时哪些表需已存在（depends_on），哪些表可能需同步建
    （referenced_by）。LLM 据此决定是否拆分多 op。
    """
    return {
        "depends_on": get_add_dependencies(stem),
        "referenced_by": get_referencing_tables(stem),
    }


__all__ = ["get_add_dependencies", "get_referencing_tables", "get_cascade_hints"]

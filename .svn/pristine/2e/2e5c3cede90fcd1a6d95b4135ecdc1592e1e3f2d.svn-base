"""多表关联查询支持：维护 table_relations.json 关系图谱。

记录表间外键关联（如 pet_evolve.xlsx 的 宠物id 列关联 pet.xlsx 的 灵兽id 列）。
当用户查询涉及跨表语义时（如"灵兽的进化数据"），定位器先定位主表，
再通过关系图谱找到关联表，把两张表的结构同时注入 LLM 上下文。

关系文件持久化在 agent/table_relations.json，缺失时返回空图谱并降级运行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional


def _relations_path() -> Path:
    """table_relations.json 路径（excel/table_relations.json）。

    本模块位于 excel/core/ 子包，但关系图 JSON 生成在 excel/ 父级。
    原 `Path(__file__).parent` 误指 core/table_relations.json（不存在）→
    RelationGraph 空、LocatorAgent._expand_by_fk 与 _collect_fk_edges 拿不到边
    → interaction/spawn_world_entity 等关联表无法补进候选（dialog 全丢）。
    逐级向上（本目录→父 excel/→祖父）找首个存在处，找不到回退父级触发降级。
    """
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        p = cand / "table_relations.json"
        if p.exists():
            return p
    return here.parent / "table_relations.json"


def _runtime_relations_path() -> Path:
    """D5: 运行时关系文件（skill_updater 写入的 co_occur 关系，excel/skills/L2_runtime/）。"""
    here = Path(__file__).resolve().parent
    for base in (here, here.parent, here.parent.parent):
        p = base / "skills" / "L2_runtime" / "table_relations.runtime.json"
        if p.exists():
            return p
    return here.parent / "skills" / "L2_runtime" / "table_relations.runtime.json"


@dataclass
class TableRelation:
    """单条表间关联关系。

    Attributes:
        from_path: 源表相对 workspace 的路径（如 pet_evolve.xlsx）
        from_sheet: 源 sheet 名
        from_column: 源表中外键列名
        to_path: 目标表路径（如 pet.xlsx）
        to_sheet: 目标 sheet 名
        to_column: 目标表被引用的列名（如 灵兽id）
        relation_type: 关系类型，默认 "foreign_key"
        description: 关系描述（可选）
    """
    from_path: str
    from_sheet: str
    from_column: str
    to_path: str
    to_sheet: str
    to_column: str
    relation_type: str = "foreign_key"
    description: str = ""


class RelationGraph:
    """表间关系图谱：加载/查询/扩展聚焦表集合。

    用法：
      graph = RelationGraph.load()
      related = graph.expand(["pet/pet.xlsx"])  # 主表 + 直接关联表
      # 把 related 传给 LLMContextBuilder.build_context(focused=related)

    O11:load() 模块级缓存(按文件 mtime 失效),避免每指令/每调用重读 json+解析。
    """

    # O11 缓存:(mtime_key, RelationGraph)
    _cache: list = [None, None]  # [mtime_key, graph]

    def __init__(self, relations: list[TableRelation] | None = None):
        self.relations: list[TableRelation] = relations or []

    @staticmethod
    def _cache_key() -> Optional[tuple]:
        """组合静态+运行时文件 mtime 作缓存键;文件不存在跳过。"""
        try:
            sp = _relations_path()
            rp = _runtime_relations_path()
            return (
                (sp.name, sp.stat().st_mtime) if sp.exists() else None,
                (rp.name, rp.stat().st_mtime) if rp.exists() else None,
            )
        except Exception:
            return None

    @classmethod
    def load(cls) -> "RelationGraph":
        """从 table_relations.json 加载 + D5 merge 运行时 table_relations.runtime.json。

        静态优先（同关系静态覆盖运行时）。文件缺失或损坏返回空图谱。
        O11:命中缓存直接返,避免每指令重读 json+解析。
        """
        # O11 命中缓存
        ck = cls._cache_key()
        if ck is not None and cls._cache[0] == ck and cls._cache[1] is not None:
            return cls._cache[1]

        out: list[TableRelation] = []
        # 1) 静态 table_relations.json
        p = _relations_path()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                raw = data.get("relations", []) if isinstance(data, dict) else []
                for r in raw:
                    try:
                        out.append(TableRelation(
                            from_path=r["from_path"], from_sheet=r["from_sheet"],
                            from_column=r["from_column"], to_path=r["to_path"],
                            to_sheet=r["to_sheet"], to_column=r["to_column"],
                            relation_type=r.get("relation_type", "foreign_key"),
                            description=r.get("description", ""),
                        ))
                    except (KeyError, TypeError):
                        continue
            except Exception:
                pass
        # 2) D5 运行时 table_relations.runtime.json（静态优先：去重时静态覆盖运行时）
        rp = _runtime_relations_path()
        if rp.exists():
            try:
                rdata = json.loads(rp.read_text(encoding="utf-8"))
                rraw = rdata.get("relations", []) if isinstance(rdata, dict) else []
                existing = {(r.from_path, r.from_column, r.to_path, r.to_column)
                            for r in out}
                for r in rraw:
                    try:
                        tr = TableRelation(
                            from_path=r["from_path"], from_sheet=r["from_sheet"],
                            from_column=r["from_column"], to_path=r["to_path"],
                            to_sheet=r["to_sheet"], to_column=r["to_column"],
                            relation_type=r.get("relation_type", "co_occur"),
                            description=r.get("description", ""),
                        )
                        key = (tr.from_path, tr.from_column, tr.to_path, tr.to_column)
                        if key not in existing:  # 静态优先
                            out.append(tr)
                            existing.add(key)
                    except (KeyError, TypeError):
                        continue
            except Exception:
                pass
        graph = cls(relations=out)
        # O11 写缓存
        if ck is not None:
            cls._cache[0] = ck
            cls._cache[1] = graph
        return graph

    def save(self) -> None:
        """持久化到 table_relations.json。"""
        _relations_path().write_text(
            json.dumps({"relations": [asdict(r) for r in self.relations]},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ── 增删 ─────────────────────────────────────────────────

    def add_relation(self, from_path: str, from_sheet: str, from_column: str,
                     to_path: str, to_sheet: str, to_column: str,
                     relation_type: str = "foreign_key",
                     description: str = "") -> TableRelation:
        """新增一条关系（不去重，调用方自行避免重复）。"""
        r = TableRelation(from_path, from_sheet, from_column,
                          to_path, to_sheet, to_column, relation_type, description)
        self.relations.append(r)
        return r

    def remove_relation(self, from_path: str, from_column: str,
                        to_path: str, to_column: str) -> int:
        """删除匹配的关系，返回删除条数。"""
        before = len(self.relations)
        self.relations = [
            r for r in self.relations
            if not (r.from_path == from_path and r.from_column == from_column
                    and r.to_path == to_path and r.to_column == to_column)
        ]
        return before - len(self.relations)

    # ── 查询 ─────────────────────────────────────────────────

    def relations_from(self, path: str) -> list[TableRelation]:
        """从 path 出发的关系（path 作为源表）。"""
        return [r for r in self.relations if r.from_path == path]

    def relations_to(self, path: str) -> list[TableRelation]:
        """指向 path 的关系（path 作为目标表）。"""
        return [r for r in self.relations if r.to_path == path]

    def related_paths(self, path: str, direction: str = "both") -> list[str]:
        """返回与 path 直接关联的表路径（不含 path 自身）。

        Args:
            direction: "out" 仅出向、"in" 仅入向、"both" 双向（默认）。
        """
        out: list[str] = []
        seen: set[str] = set()
        if direction in ("out", "both"):
            for r in self.relations_from(path):
                if r.to_path != path and r.to_path not in seen:
                    seen.add(r.to_path); out.append(r.to_path)
        if direction in ("in", "both"):
            for r in self.relations_to(path):
                if r.from_path != path and r.from_path not in seen:
                    seen.add(r.from_path); out.append(r.from_path)
        return out

    def expand(self, paths: list[str]) -> list[str]:
        """扩展聚焦表集合：原表 + 各自直接关联表，去重保序（原表在前）。

        用于 LLM 上下文构建时把跨表关联结构一并注入。
        """
        out: list[str] = []
        seen: set[str] = set()
        for p in paths:
            if p and p not in seen:
                seen.add(p); out.append(p)
            for rp in self.related_paths(p, direction="both"):
                if rp not in seen:
                    seen.add(rp); out.append(rp)
        return out

    def get_related_tables(self, stem: str) -> list[tuple[str, str]]:
        """D5 运行期接口：按 stem 返回关联表列表 [(related_stem, relation_type)]。

        stem 是表名（如 "entity_prefab"），内部匹配 path 含该 stem 的关系。
        relation_type: foreign_key / co_occur / one_to_one / one_to_many 等。
        供 _run_add 注入 LLM 拆表 prompt 辅助提示。
        """
        out: list[tuple[str, str]] = []
        seen: set[str] = set()
        for r in self.relations:
            # path 形如 "entity_prefab/entity_prefab.xlsx"，stem = Path(path).stem
            from_stem = Path(r.from_path).stem
            to_stem = Path(r.to_path).stem
            if from_stem == stem and to_stem != stem and to_stem not in seen:
                seen.add(to_stem)
                out.append((to_stem, r.relation_type))
            elif to_stem == stem and from_stem != stem and from_stem not in seen:
                seen.add(from_stem)
                out.append((from_stem, r.relation_type))
        return out

    def relations_between(self, path_a: str, path_b: str) -> list[TableRelation]:
        """返回两张表之间的直接关系（双向）。"""
        return [
            r for r in self.relations
            if (r.from_path == path_a and r.to_path == path_b)
            or (r.from_path == path_b and r.to_path == path_a)
        ]


if __name__ == "__main__":
    g = RelationGraph.load()
    if not g.relations:
        # 种子示例关系（基于 resources/pet/ 下实际列名）
        g.add_relation("pet/pet_evolve.xlsx", "PetEvolveData", "宠物id",
                       "pet/pet.xlsx", "Pet", "灵兽id",
                       description="灵兽进化表通过宠物id关联灵兽主表")
        g.add_relation("pet/pet_evolve.xlsx", "PetEvolveData", "进化后的灵兽ID",
                       "pet/pet.xlsx", "Pet", "灵兽id",
                       description="进化结果指向灵兽主表")
        g.save()
        print("已写入种子关系")
    print(f"关系总数: {len(g.relations)}")
    for r in g.relations:
        print(f"  {r.from_path}.{r.from_sheet}.{r.from_column} -> "
              f"{r.to_path}.{r.to_sheet}.{r.to_column}")
    print("expand(['pet/pet.xlsx']):", g.expand(["pet/pet.xlsx"]))

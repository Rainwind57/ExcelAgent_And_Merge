"""语义别名管理（层1）：维护 alias_mapping.json，把自然语言别名映射到具体文件。

例：{"灵兽": "pet.xlsx", "宠物": "pet.xlsx", "神通": "ability.xlsx"}

用途：表格定位器（层3）的别名匹配级（置信度 90%），支持自然语言反查文件。
文件持久化在 agent/alias_mapping.json，缺失时返回空映射并降级运行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


def _alias_path() -> Path:
    """alias_mapping.json 路径。

    本模块位于 excel/locator/ 子包，但 alias_mapping.json 生成在 excel/ 父级。
    原 `Path(__file__).parent` 误指 locator/alias_mapping.json（不存在）→别名全空、
    表路由 miss（quest/reward 不可发现的根因之一）。逐级向上找首个存在处。
    """
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent):
        p = cand / "alias_mapping.json"
        if p.exists():
            return p
    return here.parent / "alias_mapping.json"


def _hints_path() -> Path:
    """index_builder_hints.yaml 路径（excel/skills/，权威 stem→域来源）。"""
    here = Path(__file__).resolve().parent  # locator/
    return here.parent / "skills" / "index_builder_hints.yaml"  # excel/skills/


def _resources_dir() -> Path:
    """resources/ 目录（扫描 xlsx 取 stem→file 反查）。向上逐级找，兼容不同启动 CWD。"""
    here = Path(__file__).resolve().parent
    for cand in (here, here.parent, here.parent.parent,
                 here.parent.parent.parent, here.parent.parent.parent.parent):
        r = cand / "resources"
        if r.is_dir():
            return r
    return here.parent.parent.parent.parent / "resources"


def _load_stem_to_domain() -> dict[str, str]:
    """从 index_builder_hints.yaml 加载 stem_to_domain（权威别名来源）。"""
    p = _hints_path()
    if not p.exists():
        return {}
    try:
        import yaml
    except ImportError:
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    d = data.get("stem_to_domain", {}) or {}
    return {str(k): str(v) for k, v in d.items() if v}


@dataclass
class AliasMapping:
    """别名 → 文件路径映射。

    Attributes:
        mapping: {alias: file_path}，file_path 为相对 workspace 的路径（如 pet.xlsx）
    """
    mapping: dict[str, str] = field(default_factory=dict)

    @classmethod
    def autogenerate(cls, resources_dir: Path | None = None) -> "AliasMapping":
        """从 index_builder_hints.yaml 的 stem_to_domain + resources/ 扫描自动派生别名。

        权威来源是 stem_to_domain（人工只维护该 yaml）。每个 stem 的域标签作为
        自然语言别名指向该 stem 的 xlsx 文件。歧义词（如「奖励」同时是 domain 与
        多表 header）由 LocatorAgent._llm_resolve 运行时裁决，本层只产候选。
        """
        stem2domain = _load_stem_to_domain()
        if not stem2domain:
            return cls()
        res = Path(resources_dir) if resources_dir else _resources_dir()
        if not res.is_dir():
            return cls()
        # 扫描 xlsx → {stem: relative_path}（不打开文件，轻量；首个同名优先）
        stem_to_path: dict[str, str] = {}
        for p in sorted(res.rglob("*.xlsx")):
            if p.name.startswith("~$"):
                continue
            stem = p.stem
            if stem not in stem_to_path:
                stem_to_path[stem] = str(p.relative_to(res)).replace("\\", "/")
        mapping: dict[str, str] = {}
        for stem, domain in stem2domain.items():
            fp = stem_to_path.get(stem)
            if fp:
                mapping[domain] = fp
        return cls(mapping=mapping)

    @classmethod
    def load(cls) -> "AliasMapping":
        """加载别名映射：autogenerate()（hints 权威来源）作 baseline + alias_mapping.json 作手工覆盖。

        json 覆盖 autogen（手工细化优先，如「进化」→pet_evolve.xlsx 是 autogen 之外的
        特定词）。json 缺失/损坏时仅用 autogen。两者皆空返回空映射降级运行。
        """
        base = cls.autogenerate()
        p = _alias_path()
        if not p.exists():
            return base
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return base
        # 容忍两种结构：{alias: file} 或 {"aliases": {alias: file}}
        raw = data.get("aliases", data) if isinstance(data, dict) else {}
        json_map = {str(k): str(v) for k, v in raw.items() if v}
        merged = dict(base.mapping)
        merged.update(json_map)  # json 覆盖 autogen
        return cls(mapping=merged)

    def save(self) -> None:
        """持久化到 alias_mapping.json。"""
        _alias_path().write_text(
            json.dumps({"aliases": self.mapping}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def set(self, alias: str, file_path: str) -> None:
        """新增/更新一条别名映射。"""
        if alias and file_path:
            self.mapping[alias] = file_path

    def remove(self, alias: str) -> bool:
        """删除一条别名映射，返回是否实际删除。"""
        if alias in self.mapping:
            del self.mapping[alias]
            return True
        return False

    def lookup(self, alias: str) -> Optional[str]:
        """精确别名 → 文件路径；未命中返回 None。"""
        return self.mapping.get(alias)

    def lookup_in_text(self, text: str) -> list[tuple[str, str]]:
        """在自然语言文本中反查别名，返回所有命中的 (alias, file_path) 列表。

        按别名长度降序返回（越长越具体，定位器优先采纳）。
        """
        if not text:
            return []
        hits: list[tuple[str, str]] = []
        for alias, fp in self.mapping.items():
            if alias and alias in text:
                hits.append((alias, fp))
        hits.sort(key=lambda x: len(x[0]), reverse=True)
        return hits

    def files_for_stem(self, stem: str) -> list[str]:
        """返回所有指向指定 stem（文件名无后缀）的别名。"""
        return [a for a, fp in self.mapping.items() if Path(fp).stem == stem]


if __name__ == "__main__":
    auto = AliasMapping.autogenerate()
    am = AliasMapping.load()
    print(f"autogen(hints stem_to_domain): {len(auto.mapping)} 条")
    print(f"merged(load = autogen + json 覆盖): {len(am.mapping)} 条")
    # json-only 条目（autogen 没产出、靠手工 json 补的）→ 后续可迁移进 hints
    json_only = {k: v for k, v in am.mapping.items() if k not in auto.mapping}
    print(f"json-only（手工维护、可迁移进 hints 的词）: {len(json_only)} 条")
    for k, v in sorted(json_only.items()):
        print(f"  {k} → {v}")
    print("\nlookup 试:「查看灵兽饕餮的数据」→", am.lookup_in_text("查看灵兽饕餮的数据"))

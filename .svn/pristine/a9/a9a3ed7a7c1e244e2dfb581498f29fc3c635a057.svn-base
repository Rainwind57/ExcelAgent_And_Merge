"""辅助文件加载器：从 agent/skills/*.yaml 加载定位辅助配置。

辅助文件分三类，均放在 agent/skills/ 下：
  - column_aliases.yaml   列名别名（自然语言 → 列名），消除滑窗切分错误
  - row_aliases.yaml      行定位别名/匹配规则（自然语言值 → 单元格匹配策略）
  - table_context.yaml    表/sheet 上下文关键词（消歧多 sheet）

加载失败或文件缺失不抛异常，返回空配置，保证 agent 可降级运行。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

# agent/excel/skills/ 目录的绝对路径
_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"

# T12: 四层目录结构 — L1 自动派生文件迁移到 L1_derived/

# L1 自动生成的 yaml 文件清单（regenerate_skills 写盘目标 + _load_yaml 回退查找）
_L1_FILES = {
    "column_aliases.yaml",
    "row_aliases.yaml",
    "table_context.yaml",
    "value_constraints.yaml",
    "merge_strategies.yaml",
    "cascade_rules.yaml",
    "enum_mappings.yaml",
}

# yaml 解析结果缓存：name -> (mtime, data)，文件重写后按 mtime 自动失效
_YAML_CACHE: dict[str, tuple[float, dict]] = {}


def _load_yaml(name: str) -> dict:
    """加载 agent/skills/ 下的 YAML 文件，返回 dict。
    文件不存在或 yaml 不可用时返回空 {}，保证降级运行。

    T12: L1 自动派生文件优先从 L1_derived/ 加载（重组后位置），
    回退到根目录（兼容迁移前/未重组环境/测试 monkeypatch _SKILLS_DIR）。
    _L1_derived 路径随 _SKILLS_DIR 动态计算，保证 monkeypatch 生效。

    缓存：按 name + 文件 mtime 缓存解析结果，避免每次 parse 读盘两 yaml。
    """
    if not _HAS_YAML:
        return {}
    # 解析目标路径：L1 文件优先 L1_derived/，回退根目录
    resolved = None
    if name in _L1_FILES:
        p_l1 = _SKILLS_DIR / "L1_derived" / name
        if p_l1.exists():
            resolved = p_l1
    if resolved is None:
        p = _SKILLS_DIR / name
        if p.exists():
            resolved = p
    if resolved is None:
        return {}
    try:
        mtime = resolved.stat().st_mtime
    except Exception:
        mtime = 0.0
    cached = _YAML_CACHE.get(name)
    if cached is not None and cached[0] == mtime and mtime > 0:
        return cached[1]
    try:
        data = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("%s 加载失败（已降级为空配置）", name, exc_info=True)
        return {}
    _YAML_CACHE[name] = (mtime, data)
    return data


def _load_runtime_column_aliases() -> dict:
    """加载 L2_runtime/column_aliases.runtime.yaml 的 columns 段。
    文件不存在/无 yaml → 返回 {}。该文件由 skill_updater 从 evidence promote 生成，
    不会被 schema_infer.regenerate_skills 覆盖（独立子目录）。"""
    if not _HAS_YAML:
        return {}
    p = _SKILLS_DIR / "L2_runtime" / "column_aliases.runtime.yaml"
    if not p.exists():
        return {}
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except Exception:
        logger.warning("L2_runtime column_aliases 加载失败（已降级为空配置）", exc_info=True)
        return {}
    return data.get("columns") or {}


@dataclass
class ColumnAliasConfig:
    """列别名配置：每张表的每个 sheet 可定义 列别名 → 列名。

    结构: { table_stem: { sheet_name: { alias: column_name, ... }, ... }, ... }
    通配 sheet 用 "*"。
    """
    # { table_stem: { sheet_name: { alias: column_name } } }
    # table_stem = 表名（不含扩展名）, sheet_name 可用 "*" 表通配
    mapping: dict[str, dict[str, dict[str, str]]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "ColumnAliasConfig":
        """加载列别名配置：manual（column_aliases.yaml，自动生成+人工）优先，
        runtime（L2_runtime/column_aliases.runtime.yaml，skill_updater promote）补充。

        合并优先级（D7.6）：manual 任一层覆盖 runtime 同 key，runtime 仅补 manual 未覆盖的别名。
        """
        data = _load_yaml("column_aliases.yaml")
        mapping = data.get("columns", {}) or {}
        runtime = _load_runtime_column_aliases()
        if runtime:
            for tbl, sheets in runtime.items():
                base_tbl = mapping.setdefault(tbl, {})
                for sheet, alias_map in sheets.items():
                    base_sheet = base_tbl.setdefault(sheet, {})
                    for alias, col in alias_map.items():
                        # manual 已有该 alias → 跳过（人工优先）
                        if alias not in base_sheet:
                            base_sheet[alias] = col
        return cls(mapping=mapping)

    def resolve(self, table_stem: str, sheet: str, alias: str) -> Optional[str]:
        """查表+sheet+别名 → 列名。查找顺序：先精确 table_stem，失败回退全局通配表 "*"；
        每个表内先查精确 sheet_name，查不到再查通配 sheet "*"。"""
        # 若指定 table_stem 不存在，回退到通配表 "*" 的配置
        tbl = self.mapping.get(table_stem) or self.mapping.get("*") or {}
        # 依次查精确 sheet → 通配 sheet
        for key in (sheet, "*"):
            sheet_map = tbl.get(key)
            if sheet_map and alias in sheet_map:
                return sheet_map[alias]
        return None

    def all_aliases(self, table_stem: str, sheet: str) -> dict[str, str]:
        """返回该 table_stem + sheet 下所有别名→列名映射。

        合并顺序（越往后越优先）：
          ① 通配表 "*" → 通配 sheet "*"
          ② 通配表 "*" → 精确 sheet
          ③ 精确表 → 通配 sheet "*"
          ④ 精确表 → 精确 sheet（最高优先级）
        与 resolve() / rules_for() 保持一致的通配回退逻辑。
        """
        out: dict[str, str] = {}
        # ①② 通配表 "*" 的别名（基础别名，如 "名"→"名称"）
        wildcard_tbl = self.mapping.get("*", {})
        for key in ("*", sheet):
            sheet_map = wildcard_tbl.get(key)
            if sheet_map:
                out.update(sheet_map)
        # ③④ 精确表的别名覆盖通配
        tbl = self.mapping.get(table_stem, {})
        for key in ("*", sheet):
            sheet_map = tbl.get(key)
            if sheet_map:
                out.update(sheet_map)
        return out

    def all_aliases_with_source(self, table_stem: str, sheet: str) -> dict[str, tuple[str, str]]:
        """同 all_aliases，但值带 source 标记：返回 {alias: (col, source)}。

        source ∈ {"manual", "runtime"}，供 evidence 记录溯源。
        manual 条目来自 column_aliases.yaml（自动生成+人工），runtime 条目来自
        L2_runtime/column_aliases.runtime.yaml（skill_updater promote）。
        合并优先级与 all_aliases 一致：manual 覆盖 runtime 同 key。

        注：self.mapping 已是 manual+runtime 合并结果，无法区分来源，故此处
        重新加载两份原始 yaml 做带源合并。
        """
        manual_map = _load_yaml("column_aliases.yaml").get("columns", {}) or {}
        runtime = _load_runtime_column_aliases()
        out: dict[str, tuple[str, str]] = {}
        # runtime 先铺底（低优先级）
        for tbl_key in ("*", table_stem):
            rt_tbl = runtime.get(tbl_key, {})
            for sheet_key in ("*", sheet):
                rt_sheet = rt_tbl.get(sheet_key, {})
                for alias, col in rt_sheet.items():
                    out[alias] = (col, "runtime")
        # manual 覆盖
        wildcard_tbl = manual_map.get("*", {})
        for key in ("*", sheet):
            sheet_map = wildcard_tbl.get(key)
            if sheet_map:
                for alias, col in sheet_map.items():
                    out[alias] = (col, "manual")
        tbl = manual_map.get(table_stem, {})
        for key in ("*", sheet):
            sheet_map = tbl.get(key)
            if sheet_map:
                for alias, col in sheet_map.items():
                    out[alias] = (col, "manual")
        return out


@dataclass
class RowAliasConfig:
    """行定位别名/匹配规则。

    结构: { table_stem: { sheet_name: [ {locator_column, aliases:[...], match:"exact"|"contains"|"startswith"}, ... ] } }

    说明:
      - locator_column: 用哪列定位行（如 "名称"）
      - aliases: 该列已知的行值别名映射 {别名: 真实值}，如 {"御剑": "御剑·养剑"} 不需要，
                 真实值直接在表里。aliases 用于把用户说法归一到真实值。
      - match: 单元格匹配策略
                 exact     完全相等（默认）
                 contains  cell 包含 value（"御剑" 命中 "御剑·养剑"）
                 startswith cell 以 value 开头
    """
    # { table_stem: { sheet_name: [ {locator_column, aliases, match}, ... ] } }
    # table_stem/ sheet_name 均可用 "*" 表通配
    mapping: dict[str, dict[str, list[dict]]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "RowAliasConfig":
        """从 agent/skills/row_aliases.yaml 加载行定位别名/匹配规则。"""
        data = _load_yaml("row_aliases.yaml")
        return cls(mapping=data.get("tables", {}))

    def rules_for(self, table_stem: str, sheet: str) -> list[dict]:
        """返回该表+sheet 的行定位规则列表。
        合并顺序（越往后越优先）：通配表通配sheet → 通配表精确sheet → 精确表通配sheet → 精确表精确sheet。
        使用 list.extend 串联，调用方按顺序遍历，后出现的规则会覆盖先出现的。"""
        rules = []
        # ① 全局通配表 "*" → 通配 sheet "*"（最低优先级）
        wildcard_tbl = self.mapping.get("*", {})
        rules.extend(wildcard_tbl.get("*", []))
        # ② 全局通配表 "*" → 精确 sheet（覆盖①）
        rules.extend(wildcard_tbl.get(sheet, []))
        # ③ 精确表 → 通配 sheet "*"（覆盖①②）
        tbl = self.mapping.get(table_stem, {})
        rules.extend(tbl.get("*", []))
        # ④ 精确表 → 精确 sheet（最高优先级，覆盖①②③）
        rules.extend(tbl.get(sheet, []))
        return rules


@dataclass
class TableContextConfig:
    """表/sheet 上下文关键词，用于多 sheet 消歧。

    结构: { table_stem: { sheet_name: { keywords: [...], description: "..." } } }
    当自然语言命中某 sheet 的 keywords 时，优先选该 sheet。
    """
    # { table_stem: { sheet_name: { keywords: [...], description: "..." } } }
    mapping: dict[str, dict[str, dict]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "TableContextConfig":
        """从 agent/skills/table_context.yaml 加载表/sheet 上下文关键词。"""
        data = _load_yaml("table_context.yaml")
        return cls(mapping=data.get("tables", {}))

    def sheet_for_keywords(self, table_stem: str, text: str) -> Optional[str]:
        """根据自然语言文本匹配最相关的 sheet。
        查找逻辑：遍历该表每个 sheet 的 keywords 列表，对每个 sheet 取第一个命中的关键词；
        比较所有命中 sheet 的关键词长度，返回最长关键词命中的 sheet（越长越具体，消歧越准）。
        未命中任何关键词则返回 None。"""
        tbl = self.mapping.get(table_stem, {})
        best_sheet = None
        best_kw_len = 0
        for sheet_name, cfg in tbl.items():
            kws = cfg.get("keywords", [])
            for k in kws:
                if k in text:
                    # 关键词越长 → 匹配越具体 → 优先级越高
                    if len(k) > best_kw_len:
                        best_kw_len = len(k)
                        best_sheet = sheet_name
                    break  # 每 sheet 只取一个命中，不重复计数
        return best_sheet


@dataclass
class ParserConfig:
    """自然语言解析器配置（来自 parser_config.yaml）。"""
    # 引导动词：自然语言中动词部分，解析时用于分割"动作"与"目标"
    lead_verbs: tuple[str, ...] = ("给", "将", "把", "为", "让", "使", "对")
    # 非业务标记：sheet/表名中含这些词的，视为程序辅助 sheet，跳过业务解析
    non_business_markers: tuple[str, ...] = ("说明", "Sheet1", "程序用勿删", "程序用", "勿删", "备注", "CONFIG")

    @classmethod
    def load(cls) -> "ParserConfig":
        """从 agent/skills/parser_config.yaml 加载自然语言解析器配置。"""
        data = _load_yaml("parser_config.yaml")
        return cls(
            lead_verbs=tuple(data.get("lead_verbs", cls.lead_verbs)),
            non_business_markers=tuple(data.get("non_business_markers", cls.non_business_markers)),
        )


@dataclass
class ShortFormConfig:
    """列名短形式配置：真实列名 → 其短形式列表。

    结构: { table_stem: { sheet_name: { real_col: [short_forms] } } }
    通配表/sheet 用 "*"。用户输入命中短形式时，扩展为真实列名变体再匹配，
    解决"名"→"名称"、"描"→"描述"等短输入匹配弱的问题。
    """
    mapping: dict[str, dict[str, dict[str, list[str]]]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "ShortFormConfig":
        """从 agent/skills/column_short_form.yaml 加载短形式别名库。"""
        data = _load_yaml("column_short_form.yaml")
        return cls(mapping=data.get("columns", {}))

    def reverse_map(self, table_stem: str, sheet: str) -> dict[str, str]:
        """返回 {short_form: real_col} 反向映射，合并通配+精确，精确优先覆盖通配。

        合并顺序（后覆盖前）：
          ① 通配表 "*" → 通配 sheet "*"
          ② 通配表 "*" → 精确 sheet
          ③ 精确表 → 通配 sheet "*"
          ④ 精确表 → 精确 sheet（最高优先级）
        """
        out: dict[str, str] = {}
        for tbl_key in ("*", table_stem):
            tbl = self.mapping.get(tbl_key, {})
            for sheet_key in ("*", sheet):
                sheet_map = tbl.get(sheet_key, {})
                for real_col, shorts in sheet_map.items():
                    for short in shorts:
                        out[short] = real_col
        return out


@dataclass
class SheetAliasConfig:
    """Sheet 别名配置：自然语言/sheet 提示 → 真实 sheet 名。

    结构: { table_stem: { alias: sheet_name } }，table_stem 可用 "*" 通配。
    用于多 sheet 消歧：用户说的"灵兽表""基础表"等别名 → 真实 sheet 名。
    """
    mapping: dict[str, dict[str, str]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "SheetAliasConfig":
        """从 agent/skills/sheet_aliases.yaml 加载 sheet 别名配置。"""
        data = _load_yaml("sheet_aliases.yaml")
        return cls(mapping=data.get("sheets", {}))

    def aliases_for(self, table_stem: str) -> dict[str, str]:
        """返回该表所有 {alias: sheet_name}，合并通配表 + 精确表，精确覆盖通配。"""
        out: dict[str, str] = {}
        for key in ("*", table_stem):
            tbl = self.mapping.get(key, {})
            if tbl:
                out.update(tbl)
        return out

    def resolve(self, table_stem: str, alias: str) -> Optional[str]:
        """查 alias → 真实 sheet 名。精确命中优先，否则子串模糊命中。"""
        if not alias:
            return None
        alias_s = str(alias).strip()
        tbl = self.aliases_for(table_stem)
        if alias_s in tbl:
            return tbl[alias_s]
        for a, sn in tbl.items():
            if a and (alias_s in a or a in alias_s):
                return sn
        return None

    def sheet_for_text(self, table_stem: str, text: str) -> Optional[str]:
        """文本中包含的最长别名 → 真实 sheet 名。

        遍历该表所有别名，找出在 text 中出现的最长别名对应的 sheet。
        越长越具体，消歧越准（与 TableContextConfig.sheet_for_keywords 同语义）。
        用于多 sheet 消歧：自然语言含"迎新词""职位权限"等业务别名时直接定 sheet。
        """
        if not text:
            return None
        tbl = self.aliases_for(table_stem)
        best_sheet = None
        best_len = 0
        for alias, sn in tbl.items():
            if alias and alias in text and len(alias) > best_len:
                best_len = len(alias)
                best_sheet = sn
        return best_sheet


@dataclass
class AntiPatternConfig:
    """L3 反模式配置：从 L3_anti_patterns/anti_patterns.yaml 加载（D8.1）。

    反模式类型：
      - ambiguous_column  该列 contains 匹配必歧义 → 强制 exact（force_exact）
      - type_constraint   写值被拒的列 → 写非数字前强制确认（require_confirm）
      - failed_operation  needs_confirm 后用户放弃 → 直接拒绝（block_dry_run）

    status：
      - active          生效，agent 定位前查 L3 改变行为
      - pending_review  待人工激活（failed_operation 自动升级默认此态）
      - dormant         30 天无新触发，降级但不删（T9 衰减）

    查询返回匹配且 status=active 的条目；其余状态调用方不强制。
    """
    patterns: list[dict] = field(default_factory=list)

    @classmethod
    def load(cls) -> "AntiPatternConfig":
        """从 L3_anti_patterns/anti_patterns.yaml 加载反模式库。文件缺失返回空。"""
        if not _HAS_YAML:
            return cls()
        p = _SKILLS_DIR / "L3_anti_patterns" / "anti_patterns.yaml"
        if not p.exists():
            return cls()
        try:
            data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        except Exception:
            logger.warning("anti_patterns 加载失败（已降级为空配置）", exc_info=True)
            return cls()
        return cls(patterns=list(data.get("anti_patterns") or []))

    def lookup(self, table_stem: str, sheet: str, column: str = "",
               operation: str = "", input_text: str = "") -> Optional[dict]:
        """查 (table, sheet, column) 或 (table, sheet, operation) 的反模式。
        只返回 status=active 的条目。未命中返回 None。

        匹配顺序：
        1. 精确匹配（ambiguous_column / type_constraint / failed_operation）
        2. semantic_pattern 关键词匹配（input_text 含 trigger_pattern 任一关键词）
        """
        # 第一轮：精确匹配
        for it in self.patterns:
            if it.get("status") != "active":
                continue
            if it.get("table_stem") != table_stem or it.get("sheet") != sheet:
                continue
            if column and it.get("type") in ("ambiguous_column", "type_constraint") \
                    and it.get("column") == column:
                return it
            if operation and it.get("type") == "failed_operation" \
                    and it.get("operation") == operation:
                return it
        # 第二轮：semantic_pattern 关键词匹配（精确未命中时）
        if input_text:
            for it in self.patterns:
                if it.get("status") != "active":
                    continue
                if it.get("type") != "semantic_pattern":
                    continue
                it_stem = it.get("table_stem", "")
                it_sheet = it.get("sheet", "")
                if it_stem and it_stem != table_stem:
                    continue
                if it_sheet and it_sheet != sheet:
                    continue
                tp = it.get("trigger_pattern", "")
                if not tp:
                    continue
                for kw in tp.split(","):
                    kw = kw.strip()
                    if kw and kw in input_text:
                        return it
        return None

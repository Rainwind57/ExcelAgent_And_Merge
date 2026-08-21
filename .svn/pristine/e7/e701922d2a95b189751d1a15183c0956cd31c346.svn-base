"""Excel 结构自发现：从 Excel 文件自动读取表头、类型、约束等元信息。

约定（根据实际 Excel 格式）：
  Row 1: 表头（中文列名）
  Row 2: 类型标注（name:type 格式，如 pet_id:int, name:string, move_speed:float）
  Row 3: 约束标注（如 required:1）
  Row 4: 默认值
  Row 5+: 数据行

不依赖任何硬编码的表名/列名，全部从文件自动提取。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import openpyxl


@dataclass
class ColumnMeta:
    """单个列的元信息。"""
    index: int                      # 0-based 列索引
    header: str                     # 原始表头（Row 1）
    clean_name: str                 # 清理后的列名（去括号、去换行）
    en_field: str = ""              # Row2 英文字段名（去类型后缀，如 effect.key / prefab_id）
    col_type: str = ""              # 推断的类型：int, float, string, bool, unknown
    is_required: bool = False       # 是否必填（Row 3 required:1）
    default_value: Any = None       # 默认值（Row 4）
    is_id_column: bool = False      # 是否为 ID/编号类列
    is_name_column: bool = False    # 是否为名称类列
    ref_table: str = ""             # 推断的外键引用表名（从列名模式推断）


@dataclass
class SheetMeta:
    """单个 Sheet 的结构元信息。"""
    name: str
    headers: List[str] = field(default_factory=list)
    columns: List[ColumnMeta] = field(default_factory=list)
    data_start_row: int = 5
    row_count: int = 0

    @property
    def id_columns(self) -> List[ColumnMeta]:
        return [c for c in self.columns if c.is_id_column]

    @property
    def name_columns(self) -> List[ColumnMeta]:
        return [c for c in self.columns if c.is_name_column]

    @property
    def required_columns(self) -> List[ColumnMeta]:
        return [c for c in self.columns if c.is_required]


@dataclass
class TableMeta:
    """单个 Excel 文件的元信息。"""
    stem: str                       # 文件名（不含扩展名）
    path: Path                      # 文件路径
    sheets: Dict[str, SheetMeta] = field(default_factory=dict)

    @property
    def data_sheets(self) -> Dict[str, SheetMeta]:
        """排除 CONFIG 等辅助 sheet。"""
        return {k: v for k, v in self.sheets.items()
                if k.upper() != 'CONFIG' and not k.startswith('说明')}


def _clean_header(raw: Any) -> str:
    """清理表头：去括号注释、去换行、去类型后缀。"""
    if raw is None:
        return ""
    s = str(raw)
    s = s.replace("\n", "").replace("\r", "")
    s = re.sub(r"[（(].*?[）)]", "", s)
    s = s.split(":")[0]
    return s.strip()


def _parse_type_annotation(raw: Any) -> Tuple[str, Dict[str, str]]:
    """解析类型标注（Row 2），返回 (base_type, {key:value, ...})。

    示例：
      "pet_id:int" → ("int", {})
      "name:string" → ("string", {})
      "droppable:bool" → ("bool", {})
      None → ("unknown", {})
    """
    if raw is None:
        return "unknown", {}
    s = str(raw).strip()
    if not s:
        return "unknown", {}

    # 用最后一个 : 分割类型
    parts = s.rsplit(":", 1)
    if len(parts) == 2:
        type_str = parts[1].lower().strip()
        # 可能有嵌套属性如 attributes.HPMaxCon:int
        return type_str, {}
    return "unknown", {}


def _parse_constraint(raw: Any) -> Dict[str, str]:
    """解析约束标注（Row 3），返回 {key: value, ...}。

    示例：
      "required:1" → {"required": "1"}
      None → {}
    """
    if raw is None:
        return {}
    s = str(raw).strip()
    if not s or ":" not in s:
        return {}
    key, val = s.split(":", 1)
    return {key.strip().lower(): val.strip()}


_ID_PATTERNS = [
    r'_id$', r'id$', r'编号$', r'^id$',
]
_NAME_PATTERNS = [
    r'名称$', r'名字$', r'^名称$', r'^名字$', r'^名$', r'^name$',
]
_REF_PATTERNS = [
    (r'^(.+)_id$', 1),     # pet_id → pet
    (r'^(.+)编号$', 1),     # 物品编号 → 物品
    (r'^(.+)id$', 1),       # 道具id → 道具
    # D5 扩展：覆盖实际列名格式（含空格/大写 ID/prefab id/数字冒号前缀）
    (r'^(.+?)\s*ID$', 1),          # 实体Prefab ID → 实体Prefab / 对话ID → 对话
    (r'^(.+?)\s*[Pp]refab\s*[Ii][Dd]$', 1),  # 实体Prefab ID → 实体
    (r'^\d+\s*:\s*(.+?)\s*[Ii][Dd]$', 1),     # 3006:对话ID → 对话 / 3004:spawn ID → spawn
    (r'^(.+?)\s*[Ii][Dd]$', 1),    # 通用大小写归一（战斗ID/奖励ID 等）
]


def _infer_ref_table(clean: str, raw: Any = None) -> str:
    """D5 扩展 ref_table 推断：先 clean 匹配，再 raw 归一化匹配。

    覆盖 _clean_header split(":") 后丢失主体的场景（如 3006:对话ID → clean=3006，
    raw=3006:对话ID → 归一化取对话ID → 匹配对话）。
    """
    for pat, group in _REF_PATTERNS:
        m = re.match(pat, clean)
        if m:
            return m.group(group)
    # clean 未命中：用 raw 归一化重试（数字前缀格式）
    if raw is not None:
        raw_str = str(raw).strip()
        # 3006:对话ID / 3004: spawn ID → 取冒号后主体
        if ":" in raw_str:
            after = raw_str.split(":", 1)[1].strip()
            for pat, group in _REF_PATTERNS:
                m = re.match(pat, after)
                if m:
                    return m.group(group)
    return ""


def _infer_column_meta(idx: int, header_raw: Any, type_raw: Any,
                       constraint_raw: Any, default_raw: Any) -> ColumnMeta:
    """从 Excel 行数据推断列的元信息。"""
    # 表头为空时回退用类型行前缀翻译（ability_id:int → 神通id），让无表头主键列可被寻址
    from ..locator.column_name_resolver import resolve_header_cell
    resolved = resolve_header_cell(header_raw, type_raw)
    header = str(resolved) if resolved is not None else ""
    clean = _clean_header(resolved) if resolved is not None else ""
    col_type, _ = _parse_type_annotation(type_raw)
    # en_field：Row2 英文字段名（去类型后缀），如 effect.key: int → effect.key
    # splitter 产出 effect.key / effect.data.N.* 等点分键，需桥接到 Row1 中文表头。
    en_field = ""
    if type_raw is not None:
        ts = str(type_raw).strip()
        if ":" in ts:
            en_field = ts.rsplit(":", 1)[0].strip()
    constraints = _parse_constraint(constraint_raw)
    is_required = constraints.get("required") == "1"

    # 判断 ID 列
    is_id = False
    for pat in _ID_PATTERNS:
        if re.search(pat, clean.lower()):
            is_id = True
            break
    if col_type == "int" and is_id:
        pass  # int + id 模式确认

    # 判断名称列
    is_name = False
    for pat in _NAME_PATTERNS:
        if re.search(pat, clean):
            is_name = True
            break

    # 推断外键引用（D5 扩展：用 raw 兜底匹配数字前缀格式）
    ref_table = _infer_ref_table(clean, resolved)

    return ColumnMeta(
        index=idx,
        header=header,
        clean_name=clean,
        en_field=en_field,
        col_type=col_type,
        is_required=is_required,
        default_value=default_raw,
        is_id_column=is_id,
        is_name_column=is_name,
        ref_table=ref_table,
    )


def scan_sheet(ws) -> Optional[SheetMeta]:
    """扫描单个 openpyxl worksheet，提取结构化元信息。

    跳过 CONFIG/说明类的辅助 sheet。
    """
    name = ws.title
    if name.upper() == 'CONFIG' or '说明' in name:
        return None

    # 读取前 5 行（0-based 读取）
    rows = list(ws.iter_rows(min_row=1, max_row=5, values_only=True))
    if not rows or not rows[0]:
        return None

    headers_raw = rows[0]
    types_raw = rows[1] if len(rows) > 1 else []
    constraints_raw = rows[2] if len(rows) > 2 else []
    defaults_raw = rows[3] if len(rows) > 3 else []

    # 清理尾部全 None 列
    max_col = len(headers_raw)
    while max_col > 0 and headers_raw[max_col - 1] is None:
        max_col -= 1

    if max_col == 0:
        return None

    # 构建列元信息
    columns = []
    headers = []
    for i in range(max_col):
        h = headers_raw[i]
        headers.append(str(h) if h is not None else "")
        t = types_raw[i] if i < len(types_raw) else None
        c = constraints_raw[i] if i < len(constraints_raw) else None
        d = defaults_raw[i] if i < len(defaults_raw) else None
        columns.append(_infer_column_meta(i, h, t, c, d))

    # 统计数据行数
    data_start = 5
    row_count = 0
    # 优化：用 iter_rows 批量读取，避免逐个 cell() 调用
    for row in ws.iter_rows(min_row=data_start, values_only=True):
        if any(v is not None for v in row[:max_col]):
            row_count += 1

    return SheetMeta(
        name=name,
        headers=headers,
        columns=columns,
        data_start_row=data_start,
        row_count=row_count,
    )


def scan_workbook(path: Path) -> TableMeta:
    """扫描单个 Excel 文件，返回 TableMeta。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    sheets = {}
    for ws in wb.worksheets:
        sm = scan_sheet(ws)
        if sm:
            sheets[sm.name] = sm
    wb.close()

    return TableMeta(
        stem=path.stem,
        path=path,
        sheets=sheets,
    )


def scan_directory(resources_dir: Path) -> Dict[str, TableMeta]:
    """扫描整个资源目录下所有 .xlsx 文件。

    Returns:
        {table_stem: TableMeta} 字典
    """
    tables = {}
    for p in sorted(Path(resources_dir).rglob("*.xlsx")):
        if p.name.startswith("~$"):
            continue
        try:
            tm = scan_workbook(p)
            if tm.data_sheets:
                tables[tm.stem] = tm
        except Exception:
            continue
    return tables


def discover_relationships(tables: Dict[str, TableMeta]) -> List[dict]:
    """自动发现表间的外键关系。

    方法：遍历所有表的 ID 列，在其它表中查找引用该 ID 的列。
    通过列名模式匹配：pet_id → pet 表的 id 列。

    Returns:
        [{source_table, source_sheet, source_col, target_table, target_sheet, target_col}, ...]
    """
    relationships = []

    # 索引：{table_stem: {sheet_name: [column_names]}}
    all_columns: Dict[str, Dict[str, List[ColumnMeta]]] = {}
    for stem, tm in tables.items():
        all_columns[stem] = {}
        for sn, sm in tm.data_sheets.items():
            all_columns[stem][sn] = sm.columns

    # 对每张表的每个 ID 列，找引用它的列
    for target_stem, tm in tables.items():
        for target_sn, target_sm in tm.data_sheets.items():
            for target_col in target_sm.id_columns:
                target_name = target_col.clean_name

                # 在所有其他表中搜索引用该 ID 的列
                for source_stem, source_tm in tables.items():
                    if source_stem == target_stem:
                        continue
                    for source_sn, source_sm in source_tm.data_sheets.items():
                        for source_col in source_sm.columns:
                            if source_col.ref_table == target_stem:
                                relationships.append({
                                    "source_table": source_stem,
                                    "source_sheet": source_sn,
                                    "source_col": source_col.clean_name,
                                    "source_col_idx": source_col.index,
                                    "target_table": target_stem,
                                    "target_sheet": target_sn,
                                    "target_col": target_name,
                                    "target_col_idx": target_col.index,
                                })

    return relationships


def generate_cascade_rules(tables: Dict[str, TableMeta]) -> dict:
    """从表结构自动生成级联规则。

    规则：
    - 如果表 B 的某个列引用了表 A 的 ID（如 spell_data.主动技能编号 → spell.common_spell.技能编号），
      则在表 A 新增行时，建议也更新表 B 中匹配的行。
    - 如果两个表共享同一 ID 列名（如 pet.Pet.灵兽id 和 pet_level.PetLevel.灵兽id），
      则在 pet 增删时级联 pet_level。

    Returns:
        YAML 格式的级联规则 dict
    """
    relationships = discover_relationships(tables)
    cascade = {"version": "1.0", "auto_generated": True, "rules": []}

    # 按源表分组
    by_source: Dict[str, list] = {}
    for rel in relationships:
        key = rel["source_table"]
        by_source.setdefault(key, []).append(rel)

    for source_stem, rels in by_source.items():
        rule = {
            "source_table": source_stem,
            "cascade_on_add": [],     # 新增时级联
            "cascade_on_delete": [],  # 删除时级联
            "cascade_on_update": [],  # 更新时级联（共享列值同步）
        }

        for rel in rels:
            entry = {
                "target_table": rel["target_table"],
                "source_col": rel["source_col"],
                "target_col": rel["target_col"],
            }
            rule["cascade_on_delete"].append(entry)

        # 同 stem 的表间级联
        for other_stem, other_tm in tables.items():
            if other_stem.startswith(source_stem + "_") or source_stem.startswith(other_stem + "_"):
                # 共享相同前缀，检查是否有共享的 ID 列
                source_id_cols = set()
                for sn, sm in tables[source_stem].data_sheets.items():
                    for c in sm.id_columns:
                        source_id_cols.add(c.clean_name)

                for other_sn, other_sm in other_tm.data_sheets.items():
                    for oc in other_sm.id_columns:
                        if oc.clean_name in source_id_cols:
                            rule["cascade_on_add"].append({
                                "target_table": other_stem,
                                "target_sheet": other_sn,
                                "shared_id_col": oc.clean_name,
                                "note": f"新增 {source_stem} 时自动在 {other_stem}.{other_sn} 创建基础行",
                            })
                            rule["cascade_on_delete"].append({
                                "target_table": other_stem,
                                "target_sheet": other_sn,
                                "shared_id_col": oc.clean_name,
                                "note": f"删除 {source_stem} 时同时删除 {other_stem}.{other_sn} 对应行",
                            })

        if rule["cascade_on_add"] or rule["cascade_on_delete"] or rule["cascade_on_update"]:
            cascade["rules"].append(rule)

    return cascade


def generate_value_constraints(tables: Dict[str, TableMeta]) -> dict:
    """从表结构自动生成值约束配置。

    基于：
    - Row 2 类型标注 → int/float/string/bool 类型约束
    - Row 3 required:1 → 必填约束
    - 列名含"概率"/"rate" → 0-100 范围
    - ID 列 → 唯一性约束
    """
    constraints = {"version": "1.0", "auto_generated": True, "tables": {}}

    for stem, tm in tables.items():
        table_entry = {}
        for sn, sm in tm.data_sheets.items():
            sheet_rules = {"columns": {}}
            # unique 只标主键列：sheet 的首个 id 列且非外键引用列。
            # 外键（ref_table 非空）与次要 id 列会重复出现，标 unique 会误伤。
            pk_col = None
            for c in sm.id_columns:
                if not c.ref_table:
                    pk_col = c
                    break
            for col in sm.columns:
                col_rule = {}
                if col.col_type not in ("unknown", ""):
                    col_rule["type"] = col.col_type
                if col.is_required:
                    col_rule["required"] = True
                if col is pk_col:
                    col_rule["unique"] = True

                # 从列名推断范围
                cn = col.clean_name.lower()
                if any(kw in cn for kw in ("概率", "rate", "ratio", "chance")):
                    col_rule["min"] = 0
                    col_rule["max"] = 100
                    col_rule["type"] = "float"

                # 外键引用
                if col.ref_table and col.ref_table in tables:
                    col_rule["ref_table"] = col.ref_table

                if col_rule:
                    sheet_rules["columns"][col.clean_name] = col_rule

            if sheet_rules["columns"]:
                table_entry[sn] = sheet_rules

        if table_entry:
            constraints["tables"][stem] = table_entry

    return constraints


def generate_merge_strategies(tables: Dict[str, TableMeta]) -> dict:
    """从表结构自动生成合并策略配置。

    基于列类型推断最佳合并策略：
    - int（ID类）→ take_max（取最大值，新 ID 通常更大）
    - int（非ID）→ take_newer（取非基准版本）
    - float → take_newer
    - string（名称/描述类）→ take_longest（通常更完整）
    - bool → take_newer
    - 概率类 → take_newer_but_range_check
    """
    strategies = {"version": "1.0", "auto_generated": True, "default_strategy": "manual", "tables": {}}

    for stem, tm in tables.items():
        table_entry = {}
        for sn, sm in tm.data_sheets.items():
            sheet_rules = {}
            for col in sm.columns:
                cn = col.clean_name.lower()
                if col.is_id_column:
                    sheet_rules[col.clean_name] = {"strategy": "base_priority", "reason": "ID 列，优先保留基准版本值"}
                elif col.is_name_column:
                    sheet_rules[col.clean_name] = {"strategy": "take_longest", "reason": "名称列，取最长值（通常更完整）"}
                elif col.col_type in ("int", "float"):
                    if any(kw in cn for kw in ("概率", "rate", "ratio", "chance")):
                        sheet_rules[col.clean_name] = {"strategy": "range_check", "reason": "概率列，取值在 [0,100] 内的版本"}
                    else:
                        sheet_rules[col.clean_name] = {"strategy": "take_newer", "reason": "数值列，取非基准版本值"}
                elif col.col_type in ("string", "str"):
                    if any(kw in cn for kw in ("描述", "desc", "说明", "备注", "注")):
                        sheet_rules[col.clean_name] = {"strategy": "take_longest", "reason": "文本列，取最长值"}
                    else:
                        sheet_rules[col.clean_name] = {"strategy": "take_newer", "reason": "字符串列，取非基准版本值"}
                elif col.col_type == "bool":
                    sheet_rules[col.clean_name] = {"strategy": "take_newer", "reason": "布尔列，取非基准版本值"}

            if sheet_rules:
                table_entry[sn] = sheet_rules

        if table_entry:
            strategies["tables"][stem] = table_entry

    return strategies


# ============================================================
# 列别名 / 表上下文 / 行定位规则 —— 自动生成
# ============================================================

# 通用别名：对绝大多数表都成立的列名映射
_UNIVERSAL_ALIASES = {
    "名字": "名称", "名": "名称", "name": "名称",
    "描述": "描述", "介绍": "描述", "详情": "描述",
    "图标": "图标", "icon": "图标",
    "编号": "id", "no": "编号",
    "等级": "等级", "level": "等级", "lv": "等级",
    "id": "id", "ID": "id",
    "备注": "备注", "注释": "注释",
    "类型": "类型",
    "品质": "品质",
    "境界": "境界",
    "阶段": "阶段",
    "概率": "概率",
    "数值": "数值",
    "数量": "数量",
    "消耗": "消耗",
    "价格": "价格",
    "时间": "时间",
    "条件": "条件",
    "奖励": "奖励",
}

# 名称列的关键字列表（不区分大小写）
_NAME_COLUMN_KEYWORDS = {"名称", "名字", "名", "name"}


def generate_column_aliases(tables: Dict[str, TableMeta]) -> dict:
    """从表结构自动生成列别名配置。

    策略：
    1. 通配规则保留通用别名（名称/id/图标等）
    2. 每个表的每个 sheet，为所有列生成「列名 → 列名」自身映射
    3. 对于名称列（含"名称"/"名"等），额外添加对应的通用别名
    4. 对于 id 列，额外添加「id」通用别名

    自身映射保证 CLI 用户输入真实列名时不会因缺少别名而报 warning。
    """
    columns: dict = {
        "*": {"*": dict(_UNIVERSAL_ALIASES)},
    }

    for stem, tm in tables.items():
        table_entry: dict = {}
        for sn, sm in tm.data_sheets.items():
            sheet_aliases: dict = {}
            # 本 sheet 的 id 列数：仅当恰 1 个时才补泛「id」别名；
            # 多 id 列（如 ability 的 神通id/技能id/被动id）时泛 id 歧义，交 agent 消歧处理
            id_cols_in_sheet = [c for c in sm.columns if c.is_id_column]
            for col in sm.columns:
                cn = col.clean_name
                if not cn:
                    continue
                # 自身映射：确保真实列名可匹配
                sheet_aliases[cn] = cn
                # en_field → clean_name 桥接：Row2 英文字段名（如 effect.key /
                # effect.data.3006.conv_id / prefab_id）映射到 Row1 中文表头。
                # splitter 产出 en_field 风格的点分键，matcher 需此别名才能命中。
                if col.en_field and col.en_field != cn:
                    sheet_aliases[col.en_field] = cn
                # 下划线替换：如 "活动名称" → 也接受 "活动名称"
                cn_us = cn.replace("_", " ")
                if cn_us != cn:
                    sheet_aliases[cn_us] = cn
                # 名称类列：补充通用别名
                cn_lower = cn.lower()
                for nk in _NAME_COLUMN_KEYWORDS:
                    if nk in cn_lower and cn != "名称":
                        sheet_aliases[nk] = cn
                        break
                # id 类列：补充 id 通用别名（仅单 id 列时）
                if col.is_id_column and cn.lower() != "id" and len(id_cols_in_sheet) == 1:
                    sheet_aliases["id"] = cn
            if sheet_aliases:
                table_entry[sn] = sheet_aliases
        if table_entry:
            columns[stem] = table_entry

    return {"columns": columns}


def generate_table_context(tables: Dict[str, TableMeta]) -> dict:
    """从表结构自动生成表/sheet 上下文关键词。

    策略：
    1. 每个 sheet 的列名作为 keywords（用于消歧匹配）
    2. 选取前 5 个非通用列名作为代表性关键词
    3. description 用简短中文描述
    """
    result: dict = {}

    for stem, tm in tables.items():
        table_entry: dict = {}
        for sn, sm in tm.data_sheets.items():
            col_names = [c.clean_name for c in sm.columns if c.clean_name]
            # 过滤掉太通用的列（名称/id/描述/图标/备注），保留有辨识度的
            generic = {"名称", "名字", "名", "id", "ID", "描述", "图标", "备注", "注释", "类型"}
            distinctive = [cn for cn in col_names if cn not in generic]
            # 取前 8 个
            keywords = distinctive[:8]
            # 兜底：如果全被过滤了就用全部列名
            if not keywords:
                keywords = col_names[:8]

            description = f"{stem}.{sn}"
            table_entry[sn] = {
                "keywords": keywords,
                "description": description,
            }
        if table_entry:
            result[stem] = table_entry

    return {"tables": result}


def generate_row_aliases(tables: Dict[str, TableMeta]) -> dict:
    """从表结构自动生成行定位别名规则。

    策略：
    1. 通配：用名称列 contains 匹配（覆盖大部分场景）
    2. 每个 sheet 单独规则：
       - 有名称列 → locator_column=名称, match=contains
       - 有 id 列 → 追加 locator_column=id列, match=exact
    """
    tables_rules: dict = {
        "*": {
            "*": [{"locator_column": "名称", "match": "contains"}],
        }
    }

    for stem, tm in tables.items():
        table_entry: dict = {}
        for sn, sm in tm.data_sheets.items():
            rules = []
            # 名称列 → contains
            for nc in sm.name_columns:
                rules.append({"locator_column": nc.clean_name, "match": "contains"})
            # id 列 → exact
            for ic in sm.id_columns:
                rules.append({"locator_column": ic.clean_name, "match": "exact"})
            # 如果都没有，用第一列 exact
            if not rules and sm.columns:
                rules.append({"locator_column": sm.columns[0].clean_name, "match": "exact"})
            if rules:
                table_entry[sn] = rules
        if table_entry:
            tables_rules[stem] = table_entry

    return {"tables": tables_rules}


# ============================================================
# Skills YAML 重生成入口
# ============================================================
# 6 个自动生成的 skills yaml（column_aliases / row_aliases / table_context /
# value_constraints / merge_strategies / cascade_rules）均由本模块的 generate_*
# 产出。parser_config / column_short_form / sheet_aliases / index_builder_hints
# 为手维护，不在重生成范围。
#
# 用法: python -m agent.excel.schema_infer [resources_dir]
#      （resources_dir 缺省为项目根 resources/）


# ============================================================
# 枚举映射自动发现
# ============================================================

def _extract_enums_from_sheet(ws) -> list[tuple[str, list[dict]]]:
    """从单个 sheet 提取枚举映射，支持三种格式（D10 / 8.1-8.4）。

    返回 [(col_name, [{label, value}, ...]), ...]。

    格式1（横排，向后兼容）：A1=列名，B1+=labels，B2+=int values。
    格式2（竖排两列，任意行数）：A1=列名(表头)，A2+=label，B2+=int value。
    格式3（单列 key:value）：A1=列名(表头)，A2+="label:value" / "label=value" / "label：value"。

    同列名多格式命中时合并去重。
    """
    rows = list(ws.iter_rows(values_only=True))
    if not rows:
        return []
    # 说明 sheet 通常不大，但放宽任意行数（8.2），限 500 行防巨 sheet 拖慢
    rows = rows[:500]

    def _s(v) -> str:
        if v is None:
            return ""
        return str(v).strip()

    def _int(v):
        try:
            return int(v)
        except (ValueError, TypeError):
            return None

    results: list[tuple[str, list[dict]]] = []

    def _merge(col_name: str, entries: list[dict]) -> None:
        if not col_name or not entries:
            return
        # entries 内部按 label 去重（保留首次）
        seen: dict[str, dict] = {}
        for e in entries:
            if e["label"] not in seen:
                seen[e["label"]] = e
        entries = list(seen.values())
        for i, (c, e) in enumerate(results):
            if c == col_name:
                existing = {x["label"] for x in e}
                for ne in entries:
                    if ne["label"] not in existing:
                        e.append(ne)
                        existing.add(ne["label"])
                return
        results.append((col_name, entries))

    # 格式1: 横排（原格式）A1=col_name, B1+=labels, B2+=values
    if rows and rows[0]:
        col_name = _s(rows[0][0])
        if col_name:
            labels = [_s(h) for h in rows[0][1:] if _s(h)]
            values: list = []
            if len(rows) > 1 and rows[1]:
                for v in rows[1][1:]:
                    values.append(_int(v))
            if labels and any(v is not None for v in values):
                entries = [{"label": lbl, "value": val}
                           for lbl, val in zip(labels, values)
                           if lbl and val is not None]
                if entries:
                    _merge(col_name, entries)

    # 格式2: 竖排两列 A1=col_name(表头), A2+=label, B2+=int value
    if len(rows) >= 3 and rows[0] and rows[1]:
        col_name = _s(rows[0][0])
        row2_a = _s(rows[1][0])
        row2_b = _int(rows[1][1]) if len(rows[1]) > 1 else None
        # 第2行 A 列非空（label）+ B 列 int value；排除格式1（格式1 第2行 A 列空）
        if col_name and row2_a and row2_b is not None:
            entries = []
            for r in rows[1:]:
                if not r:
                    continue
                lbl = _s(r[0])
                val = _int(r[1]) if len(r) > 1 else None
                if lbl and val is not None:
                    entries.append({"label": lbl, "value": val})
            # 至少 2 条才算枚举（避免单行业务数据误判）
            if len(entries) >= 2:
                _merge(col_name, entries)

    # 格式3: 单列 key:value（A1=col_name, A2+="label:value"）
    if len(rows) >= 3 and rows[0]:
        col_name = _s(rows[0][0])
        if col_name and len(rows[1]) >= 1:
            a2 = _s(rows[1][0])
            sep = None
            for s in (":", "：", "="):
                if s in a2:
                    sep = s
                    break
            if sep:
                entries = []
                for r in rows[1:]:
                    if not r:
                        continue
                    a = _s(r[0])
                    if sep not in a:
                        continue
                    parts = a.split(sep, 1)
                    if len(parts) != 2:
                        continue
                    lbl = parts[0].strip()
                    val = _int(parts[1].strip())
                    if lbl and val is not None:
                        entries.append({"label": lbl, "value": val})
                if len(entries) >= 2:
                    _merge(col_name, entries)

    return results


# 说明类 sheet 关键词（8.1: 不再强制 title 含"说明"，放宽到多种提示）
_EXPLAIN_SHEET_KEYWORDS = ("说明", "枚举", "enum", "explain", "config",
                           "desc", "注释", "备注", "字典", "dict")


def _is_explain_sheet(title: str, data_sheet_names: set[str]) -> bool:
    """8.1: 判断 sheet 是否为说明/枚举类候选。

    title 含说明类关键词，或 sheet 不在业务 data_sheets 中（被 data_sheets 排除的辅助 sheet）。
    """
    if not title:
        return False
    t = str(title)
    low = t.lower()
    for kw in _EXPLAIN_SHEET_KEYWORDS:
        if kw in t or kw in low:
            return True
    # 辅助 sheet（非业务数据表）也作为候选
    return title not in data_sheet_names


def _discover_enum_from_explain_sheet(tables: Dict[str, TableMeta]) -> dict:
    """从各表的说明/枚举 sheet 中自动提取枚举值映射（D10 / 8.1-8.4）。

    8.1: 不再强制 title 含"说明"，改内容模式匹配 + 辅助 sheet 判断。
    8.2: 支持 label/value 两列竖排格式（任意行数）。
    8.3: 支持单列 key:value 竖排格式。
    8.4: 保留原横排格式（A1=列名, B1+=labels, B2+=values）向后兼容。
    """
    import openpyxl
    mappings: dict = {}

    for stem, tm in tables.items():
        try:
            wb = openpyxl.load_workbook(tm.path, data_only=True)
            data_sheet_names = set(tm.data_sheets.keys())
            for ws in wb.worksheets:
                # 8.1: 放宽 sheet 判断（说明类关键词 或 非业务 sheet）
                if not _is_explain_sheet(ws.title, data_sheet_names):
                    continue
                extracted = _extract_enums_from_sheet(ws)
                if not extracted:
                    continue
                # 匹配 data_sheets 中 int 列
                for col_name, entries in extracted:
                    for sn, sm in tm.data_sheets.items():
                        for col in sm.columns:
                            if col.clean_name == col_name and col.col_type == "int":
                                mappings.setdefault(stem, {}).setdefault(
                                    sn, {}).setdefault("columns", {})[
                                    col_name] = {
                                    "type": "int",
                                    "values": entries,
                                }
            wb.close()
        except Exception:
            continue

    return mappings


def generate_enum_mappings(tables: Dict[str, TableMeta]) -> dict:
    """从表结构自动生成枚举值映射配置。"""
    discovered = _discover_enum_from_explain_sheet(tables)
    return {
        "version": "1.0",
        "auto_generated": True,
        "tables": discovered or {},
    }


def generate_table_relations(tables: Dict[str, TableMeta]) -> dict:
    """D5: 从表结构自动发现外键关系，生成 table_relations.json 兼容格式。

    discover_relationships 的输出转换为 RelationGraph 的 relations 格式。
    供 regenerate_skills 落盘到 table_relations.json（merge 已有人工关系）。
    """
    rels = discover_relationships(tables)
    return {
        "relations": [
            {
                "from_path": f"{r['source_table']}/{r['source_table']}.xlsx",
                "from_sheet": r["source_sheet"],
                "from_column": r["source_col"],
                "to_path": f"{r['target_table']}/{r['target_table']}.xlsx",
                "to_sheet": r["target_sheet"],
                "to_column": r["target_col"],
                "relation_type": "foreign_key",
                "description": f"自动发现：{r['source_table']}.{r['source_col']} → {r['target_table']}.{r['target_col']}",
            }
            for r in rels
        ]
    }


_AUTO_YAMLS = [
    ("column_aliases.yaml", generate_column_aliases, "列名别名配置"),
    ("row_aliases.yaml", generate_row_aliases, "行定位别名规则"),
    ("table_context.yaml", generate_table_context, "表/sheet 上下文关键词"),
    ("value_constraints.yaml", generate_value_constraints, "值约束规则"),
    ("merge_strategies.yaml", generate_merge_strategies, "合并策略配置"),
    ("cascade_rules.yaml", generate_cascade_rules, "级联规则"),
    ("enum_mappings.yaml", generate_enum_mappings, "枚举值映射"),
]


def _resources_max_mtime(resources_dir: Path) -> float:
    """resources 下所有 xlsx 的最新 mtime（仅 stat，不开 xlsx）。用于增量判断。"""
    mx = 0.0
    for p in Path(resources_dir).rglob("*.xlsx"):
        if p.name.startswith("~$"):
            continue
        try:
            m = p.stat().st_mtime
            if m > mx:
                mx = m
        except OSError:
            continue
    return mx


def regenerate_skills(resources_dir: Path, skills_dir=None) -> None:
    """扫描 resources 下所有 xlsx，重生成 7 个自动 skills yaml 到 agent/skills/L1_derived/。

    T12: 四层目录重组 — 自动派生的 L1 yaml 写入 L1_derived/ 子目录，
    与人工维护的 L0 文件分离。skill_loader._load_yaml 优先从 L1_derived/ 加载。

    增量优化：用 .gen_stamp 记录上次生成时的 resources xlsx 最新 mtime；若当前 mtime
    未变且 7 个 yaml 均在，直接跳过全量 openpyxl 扫描 + O(n²) 关系发现（用户反馈
    "每次重生成 skills 耗时太长"的主要原因是无脑全量重算）。资源变更（改表/加表）后
    mtime 变化 → 触发重算。首次运行无戳 → 全量生成。

    skills_dir 可选：测试传 tmp 目录以隔离，避免把 tmp 内容写进生产 L1_derived 污染
    真实 skills（默认用本模块旁的 skills/）。
    """
    import yaml
    from datetime import datetime

    if skills_dir is None:
        skills_dir = Path(__file__).resolve().parents[1] / "skills"
    l1_dir = skills_dir / "L1_derived"
    l1_dir.mkdir(parents=True, exist_ok=True)

    # 增量闸门：资源 mtime 未变且产物齐全 → 跳过全量重生成
    # stamp 文件名按 resources_dir 路径哈希隔离，避免测试用 tmp resources 覆盖生产戳
    import hashlib
    _path_hash = hashlib.md5(str(Path(resources_dir).resolve()).encode("utf-8")).hexdigest()[:8]
    stamp_file = l1_dir / f".gen_stamp_{_path_hash}"
    cur_max_mtime = _resources_max_mtime(resources_dir)
    yaml_files = [l1_dir / name for name, _, _ in _AUTO_YAMLS]
    try:
        prev = float(stamp_file.read_text(encoding="utf-8").strip())
    except (FileNotFoundError, ValueError):
        prev = -1.0
    if prev == cur_max_mtime and all(f.exists() for f in yaml_files):
        print(f"skills 已是最新（resources mtime 未变，max={cur_max_mtime:.0f}），跳过重生成")
        return

    tables = scan_directory(resources_dir)
    n = len(tables)
    ts = datetime.now().isoformat(timespec="seconds")
    print(f"重生成 skills: {resources_dir} ({n} 张表) -> {l1_dir}")
    for name, fn, desc in _AUTO_YAMLS:
        data = fn(tables)
        header = (f"# {desc}（自动生成）\n# 生成时间: {ts}\n# 来源: {n} 张表\n# \n")
        body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        (l1_dir / name).write_text(header + body, encoding="utf-8")
        print(f"  wrote L1_derived/{name}")

    # D5: 自动发现外键关系，merge 到 table_relations.json（不覆盖人工补充的）
    try:
        from ..core.table_relations import RelationGraph
        import json
        existing = RelationGraph.load()
        existing_keys = {(r.from_path, r.from_column, r.to_path, r.to_column)
                         for r in existing.relations}
        new_rels = generate_table_relations(tables)
        added = 0
        for r in new_rels.get("relations", []):
            key = (r["from_path"], r["from_column"], r["to_path"], r["to_column"])
            if key not in existing_keys:
                existing.add_relation(
                    from_path=r["from_path"], from_sheet=r["from_sheet"],
                    from_column=r["from_column"], to_path=r["to_path"],
                    to_sheet=r["to_sheet"], to_column=r["to_column"],
                    relation_type=r.get("relation_type", "foreign_key"),
                    description=r.get("description", ""),
                )
                existing_keys.add(key)
                added += 1
        if added:
            existing.save()
            print(f"  wrote table_relations.json (+{added} 自动发现关系，merge 到既有)")
        else:
            print(f"  table_relations.json 无新增（自动发现 {len(new_rels.get('relations', []))} 条均已存在）")
    except Exception as exc:
        print(f"  [warn] table_relations.json 自动发现失败：{exc}")
    # 记录本次生成对应的 resources mtime 戳，下次启动未变更即可秒过
    try:
        stamp_file.write_text(str(cur_max_mtime), encoding="utf-8")
    except OSError as exc:
        print(f"  [warn] 写 .gen_stamp 失败：{exc}")
    print("done")


if __name__ == "__main__":
    import sys
    _default_res = Path(__file__).resolve().parents[3] / "resources"
    _res = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else _default_res
    regenerate_skills(_res)

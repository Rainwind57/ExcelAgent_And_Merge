"""Excel 表格通用 CLI 工具。

基于 StubCodeMakerCLI 封装，支持对项目中所有 .xlsx 表格的增删查改。

用法:
  python tools/xlsx_tool.py list                              # 列出所有表格
  python tools/xlsx_tool.py sheets school_ability             # 列出 sheet
  python tools/xlsx_tool.py header school_ability SchoolAbility  # 查看表头
  python tools/xlsx_tool.py read school_ability SchoolAbility    # 读取全部数据
  python tools/xlsx_tool.py read school_ability SchoolAbility --json  # JSON 输出
  python tools/xlsx_tool.py add school_ability SchoolAbility --神通id 8012 --名称 新神通 --神通描述 测试
  python tools/xlsx_tool.py delete school_ability SchoolAbility --row 78
  python tools/xlsx_tool.py delete school_ability SchoolAbility --name 神通测试
  python tools/xlsx_tool.py set --row 78 school_ability SchoolAbility --名称 改名测试
  python tools/xlsx_tool.py get school_ability SchoolAbility --row 78
  python tools/xlsx_tool.py get school_ability SchoolAbility --name 神通测试
  python tools/xlsx_tool.py search 神通测试                          # 全项目搜索
  python tools/xlsx_tool.py search 神通测试 --table school_ability   # 指定表
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from .cli_interface import StubCodeMakerCLI, SearchResult

WORKSPACE = Path(__file__).resolve().parent.parent.parent.parent
# T12: column_aliases.yaml 迁移到 L1_derived/，回退根目录兼容未迁移环境
_COLUMN_ALIASES_L1 = Path(__file__).resolve().parents[1] / "skills" / "L1_derived" / "column_aliases.yaml"
_COLUMN_ALIASES_ROOT = Path(__file__).resolve().parents[1] / "skills" / "column_aliases.yaml"
COLUMN_ALIASES_PATH = _COLUMN_ALIASES_L1 if _COLUMN_ALIASES_L1.exists() else _COLUMN_ALIASES_ROOT
# T30: required_fields 独立文件（原嵌 column_aliases.yaml 的 required_fields 节，README 宣称但缺）
_REQUIRED_FIELDS_L1 = Path(__file__).resolve().parents[1] / "skills" / "L1_derived" / "required_fields.yaml"
_REQUIRED_FIELDS_ROOT = Path(__file__).resolve().parents[1] / "skills" / "required_fields.yaml"
REQUIRED_FIELDS_PATH = (_REQUIRED_FIELDS_L1 if _REQUIRED_FIELDS_L1.exists()
                        else _REQUIRED_FIELDS_ROOT)


def _load_column_aliases() -> dict:
    """加载列别名配置，返回 {table_stem: {sheet: {alias: real_col}}}."""
    if not COLUMN_ALIASES_PATH.exists():
        return {}
    with open(COLUMN_ALIASES_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    result: dict = {}
    columns = raw.get("columns", {})
    for table_key, sheets in columns.items():
        result[table_key] = {}
        for sheet_key, aliases in sheets.items():
            result[table_key][sheet_key] = dict(aliases)
    return result


def _load_required_fields() -> dict:
    """加载必填字段配置。

    优先读独立 required_fields.yaml；不存在时回退 column_aliases.yaml 的 required_fields 节（兼容）。
    返回结构：{table_stem: {sheet: [field_aliases]}}，sheet 可为 "*" 表示通配所有 sheet。
    用途：cmd_add 时检查用户是否遗漏了建议必填的字段。
    """
    if REQUIRED_FIELDS_PATH.exists():
        with open(REQUIRED_FIELDS_PATH, encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        return raw.get("required_fields", {}) or {}
    # 回退：旧环境 required_fields 节仍嵌在 column_aliases.yaml
    if not COLUMN_ALIASES_PATH.exists():
        return {}
    with open(COLUMN_ALIASES_PATH, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return raw.get("required_fields", {}) or {}


def _resolve_table(name: str) -> Path:
    """将表别名解析为实际文件路径。

    支持:
      - 完整相对路径: resources/school/school_ability.xlsx
      - 文件名(不含扩展名): school_ability
      - 文件名(含扩展名): school_ability.xlsx
    """
    # 直接路径
    direct = WORKSPACE / name
    if direct.exists() and direct.suffix == ".xlsx":
        return direct

    # 加 .xlsx 再试
    with_ext = WORKSPACE / (name + ".xlsx")
    if with_ext.exists():
        return with_ext

    # 模糊搜索
    name_lower = name.lower().replace(".xlsx", "")
    cli = StubCodeMakerCLI(WORKSPACE)
    for p in cli.list_tables():
        if p.stem.lower() == name_lower:
            return p
        if name_lower in str(p).lower():
            return p

    raise FileNotFoundError(f"找不到表格: {name}")


def _resolve_column_alias(table_stem: str, sheet: str, alias: str) -> str:
    """将列别名解析为真实列名。

    三层优先级查找链（从具体到宽泛）：
    1. table.sheet —— 精确匹配指定表 + 指定 sheet 的别名
    2. table.*    —— 匹配指定表下所有 sheet 共享的别名
    3. *.*        —— 匹配全局通配别名（跨所有表、所有 sheet）

    未匹配到则原样返回 alias。
    """
    aliases = _load_column_aliases()
    # 1. 精确匹配 table.sheet
    if table_stem in aliases and sheet in aliases[table_stem]:
        if alias in aliases[table_stem][sheet]:
            return aliases[table_stem][sheet][alias]
    # 2. 通配 table.*
    if table_stem in aliases and "*" in aliases[table_stem]:
        if alias in aliases[table_stem]["*"]:
            return aliases[table_stem]["*"][alias]
    # 3. 全局通配 *.*
    if "*" in aliases and "*" in aliases["*"]:
        if alias in aliases["*"]["*"]:
            return aliases["*"]["*"][alias]
    return alias


def _match_header(col_name: str, header: list[str], table_stem: str, sheet: str) -> int | None:
    """在表头中匹配列名，返回 1-based 列索引。

    三级匹配策略（按优先级依次尝试）：
    1. 精确匹配：列名完全等于（去除首尾空白后）变体之一
    2. startswith 模糊匹配：表头项以某变体开头
    3. 换行符截取匹配：表头项按 \\n 分割取首行，再与变体精确匹配
       —— 用于处理含备注的表头，如 "建筑类型\\n（…）" 匹配 "建筑类型"

    未匹配到返回 None。
    """
    # 准备变体：将下划线替换为空格
    variants = {col_name.strip(), col_name.strip().replace("_", " ")}
    # 1. 精确匹配
    for ci, h in enumerate(header):
        if h is not None and str(h).strip() in variants:
            return ci + 1
    # 2. 模糊匹配 (startswith)
    for ci, h in enumerate(header):
        if h is not None:
            hs = str(h).strip()
            for v in variants:
                if hs.startswith(v):
                    return ci + 1
    # 3. 带换行符的变体（如 "建筑类型\n（...）" 匹配 "建筑类型"）
    for ci, h in enumerate(header):
        if h is not None:
            first_line = str(h).split("\n")[0].strip()
            for v in variants:
                if first_line == v:
                    return ci + 1
    return None


def _build_kv(args_kv: list[str], table_stem: str, sheet: str, header: list[str]) -> dict[int, str]:
    """解析命令行 --key value 对，返回 {col_index: value}。

    解析逻辑：
    1. 遍历 args_kv，遇到 -- 开头的参数视为列别名（去掉 -- 前缀）
    2. 通过 _resolve_column_alias 将别名转为真实列名
    3. 通过 _match_header 定位真实列名对应的列索引
    4. 紧接着的非 -- 开头的参数视为该列的值

    回退机制：若别名映射失败（real_col != col_alias 且未在表头中找到），
    会用原始别名再尝试一次 _match_header，以兼容未配置别名的直接列名输入。

    示例：--名称 新道具 --描述 测试  →  {3: '新道具', 5: '测试'}
    """
    result: dict[int, str] = {}
    i = 0
    while i < len(args_kv):
        key = args_kv[i]
        if key.startswith("--"):
            col_alias = key[2:]
            real_col = _resolve_column_alias(table_stem, sheet, col_alias)
            col_idx = _match_header(real_col, header, table_stem, sheet)
            # 别名映射失败时，回退用原始别名再试一次
            if col_idx is None and real_col != col_alias:
                col_idx = _match_header(col_alias, header, table_stem, sheet)
                if col_idx is not None:
                    real_col = col_alias
            if col_idx is None:
                print(f"警告: 列 '{real_col}' (别名 '{col_alias}') 不在表头中，跳过", file=sys.stderr)
                i += 1
                continue
            i += 1
            if i < len(args_kv) and not args_kv[i].startswith("--"):
                result[col_idx] = args_kv[i]
                i += 1
            else:
                result[col_idx] = ""
        else:
            i += 1
    return result


def cmd_list():
    cli = StubCodeMakerCLI(WORKSPACE)
    tables = cli.list_tables()
    if not tables:
        print("(无表格)")
        return
    for p in tables:
        rel = p.relative_to(WORKSPACE)
        sheets = cli.get_sheets(p)
        print(f"  {p.stem:30s}  [{', '.join(sheets)}]  ({rel})")


def cmd_sheets(args):
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    for s in cli.get_sheets(path):
        print(f"  {s}")


def cmd_header(args):
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    header = cli.read_header(path, args.sheet)
    for i, h in enumerate(header):
        print(f"  [{i + 1}] {h}")


def cmd_read(args):
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    header = cli.read_header(path, args.sheet)
    rows = cli.read_sheet(path, args.sheet)

    if args.json:
        result = []
        for r in rows:
            obj = {}
            for ci, h in enumerate(header):
                if ci < len(r):
                    obj[str(h)] = r[ci]
            result.append(obj)
        print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
        return

    # 表格输出
    col_widths = [max(len(str(h) or ""), 6) for h in header]
    for r in rows:
        for ci, v in enumerate(r):
            if ci < len(col_widths):
                col_widths[ci] = max(col_widths[ci], len(str(v or "")))

    def fmt_row(vals):
        return " | ".join(str(v or "").ljust(col_widths[i]) for i, v in enumerate(vals))

    print(fmt_row(header))
    print("-" * (sum(col_widths) + 3 * (len(col_widths) - 1)))
    for ri, r in enumerate(rows):
        data_start = cli.data_start_row
        print(f"[{ri + data_start}] {fmt_row(r)}")


# 进程级索引缓存：(mtime, data)，refresh_if_changed 重写后按 mtime 自动失效
_INDEX_CACHE: tuple[float, list] | None = None


def _load_index() -> list[dict]:
    """加载 _table_index.json 索引（进程级缓存 + mtime 失效）。"""
    global _INDEX_CACHE
    index_path = Path(__file__).resolve().parent / "_table_index.json"
    try:
        mtime = index_path.stat().st_mtime if index_path.exists() else 0.0
    except Exception:
        mtime = 0.0
    if _INDEX_CACHE is not None and _INDEX_CACHE[0] == mtime and mtime > 0:
        return _INDEX_CACHE[1]
    if mtime == 0.0:
        _INDEX_CACHE = (0.0, [])
        return []
    try:
        data = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        data = []
    _INDEX_CACHE = (mtime, data)
    return data


def _find_related_files(current_path: Path, current_stem: str) -> list[Path]:
    """找到与当前表格关联的其他 .xlsx 文件，用于级联操作。

    两策略互补：
    1. 同目录策略：枚举 current_path 所在目录下所有 .xlsx 文件（排除自身和临时文件 ~$）
       —— 适用于 pet/pet.xlsx ↔ pet/pet_evolve.xlsx 同目录紧密耦合场景
    2. 索引前缀策略：从 _table_index.json 中查找同 stem 前缀（_ 分割首段）的文件
       —— 适用于跨目录关联，如 school_ability ↔ school_buff 共享 "school" 前缀

    返回值已去重排序。
    """
    index = _load_index()
    current_dir = current_path.parent
    related: set[Path] = set()

    # 同目录文件
    for p in current_dir.glob("*.xlsx"):
        if p.resolve() != current_path.resolve() and not p.name.startswith("~$"):
            related.add(p)

    # 索引中同 stem 前缀（如 pet 匹配 pet_evolve）
    base = current_stem.split("_")[0] if "_" in current_stem else current_stem
    for t in index:
        t_stem = t.get("stem", "")
        base_t = t_stem.split("_")[0] if "_" in t_stem else t_stem
        if base_t == base and t_stem != current_stem:
            p = WORKSPACE / t.get("path", "")
            if p.exists() and not p.name.startswith("~$"):
                related.add(p)

    return sorted(related)


def _semantic_col_names(col_name: str) -> set[str]:
    """生成列名的语义变体集合。

    用于级联操作时跨 sheet/文件匹配含义相同但命名不同的列。
    内置 name_map 包含常见等价列名映射，如：
      '名称' → {'名称', '名字', '名', '灵兽名称', '宠物名称', ...}
      'id'   → {'id', '灵兽id', '宠物id', '道具id', '神通id', ...}

    未命中映射则至少返回原始列名及其下划线→空格替换变体。
    """
    variants = {col_name.strip(), col_name.strip().replace("_", " ")}
    # 通用名称映射
    name_map = {"名称": {"名称", "名字", "名", "灵兽名称", "宠物名称", "灵兽名字"},
                "灵兽id": {"灵兽id", "宠物id", "id"},
                "建筑名称": {"建筑名称", "名称", "建筑名"},
                "id": {"id", "灵兽id", "宠物id", "道具id", "神通id", "技能id", "仙友id"},
                }
    clean = col_name.strip()
    for key, vals in name_map.items():
        if clean == key:
            variants.update(vals)
    return variants


def _cascade_add(cli, path: Path, target_sheet: str, header: list[str],
                values: dict[int, str], table_stem: str):
    """级联添加：将共享键值同步写入关联 sheet/文件。

    级联范围分两层：
    1. 同文件其他 sheet —— 遍历当前 xlsx 中除目标 sheet 外的所有 sheet
    2. 关联文件      —— 通过 _find_related_files 找到关联 .xlsx，遍历其所有 sheet

    每一目标调用 _cascade_add_for_file，通过语义列名匹配决定写入哪些列。
    """
    all_sheets = cli.get_sheets(path)
    # 同文件其他 sheet
    for other_sheet in all_sheets:
        if other_sheet == target_sheet:
            continue
        _cascade_add_for_file(cli, path, other_sheet, header, values, table_stem)
    # 关联文件
    for rel_path in _find_related_files(path, table_stem):
        for rel_sheet in cli.get_sheets(rel_path):
            _cascade_add_for_file(cli, rel_path, rel_sheet, header, values, table_stem)


def _cascade_add_for_file(cli, path: Path, target_sheet: str, header: list[str],
                          values: dict[int, str], table_stem: str):
    """对单个文件的单个 sheet 执行级联添加。

    算法：
    1. 读取目标 sheet 的表头
    2. 遍历原始 values 的每个 (col_idx, val)
    3. 通过 _semantic_col_names 生成该列的语义变体集合
    4. 在目标表头中用 _match_header 匹配每个变体，找到对应列索引
    5. 收集匹配到的 (target_col_idx, val) 对
    6. 调用 append_row 一次写入整行

    若目标 sheet 中不存在任何可匹配的列，则跳过（不新增空行）。
    """
    h = cli.read_header(path, target_sheet)
    cascade_values: dict[int, str] = {}
    for col_idx, val in values.items():
        col_name = header[col_idx - 1] if col_idx <= len(header) else ""
        if not col_name:
            continue
        variants = _semantic_col_names(str(col_name))
        for variant in variants:
            matched_ci = _match_header(variant, h, table_stem, target_sheet)
            if matched_ci is not None and matched_ci not in cascade_values:
                cascade_values[matched_ci] = val
                break
    if cascade_values:
        cr = cli.append_row(path, target_sheet, cascade_values)
        if cr.ok:
            rel = path.relative_to(WORKSPACE)
            cols = ", ".join(f"col{c}" for c in cascade_values)
            print(f"  ↳ 级联添加: {rel}:{target_sheet} 行{cr.data['row']} (共享列: {cols})")
        else:
            print(f"  ⚠ 级联添加 {target_sheet} 失败: {cr.error}", file=sys.stderr)


def cmd_add(args):
    """添加新行命令。

    流程：解析表路径 → 构建 KV 对 → 必填字段检查 → 写入目标行 → 级联添加。
    """
    # ---- 1. 解析表路径 ----
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    header = cli.read_header(path, args.sheet)
    table_stem = path.stem

    # ---- 2. 构建 KV 对（别名 → 列索引） ----
    values = _build_kv(args.kv, table_stem, args.sheet, header)
    if not values:
        print("错误: 未提供有效的列值，格式: --列名 值 [--列名2 值2 ...]", file=sys.stderr)
        sys.exit(1)

    # ---- 3. 必填字段检查 ----
    required = _load_required_fields()
    table_req = required.get(table_stem, {})
    req_fields = table_req.get(args.sheet) or table_req.get("*", [])
    if req_fields:
        provided_names: set[str] = set()
        header_map = {ci: str(header[ci - 1]) for ci in values if ci <= len(header)}
        for ci, h_name in header_map.items():
            provided_names.add(h_name)
        # 将 required 中引用的别名解析为真实列名
        missing = []
        for req in req_fields:
            real = _resolve_column_alias(table_stem, args.sheet, req)
            if real not in provided_names and req not in provided_names:
                # 检查用户是否用其他别名提供了
                found = False
                for ci, v in values.items():
                    if ci <= len(header) and str(header[ci - 1]) == real:
                        found = True
                        break
                if not found:
                    missing.append(req)
        if missing:
            print(f"⚠ 警告: 缺少以下建议必填字段: {', '.join(missing)}", file=sys.stderr)
            print("  继续添加，但数据可能不完整。", file=sys.stderr)

    # ---- 4. 写入目标行 ----
    r = cli.append_row(path, args.sheet, values)
    if r.ok:
        print(f"✓ 已添加 {args.sheet} 行{r.data['row']}")
        print(f"  值: {r.data['values']}")
        # ---- 5. 级联添加 ----
        _cascade_add(cli, path, args.sheet, header, values, table_stem)
    else:
        print(f"✗ 添加失败: {r.error}", file=sys.stderr)
        sys.exit(1)


def cmd_search(args):
    keyword = args.keyword
    if not keyword:
        print("错误: 请指定搜索关键词", file=sys.stderr)
        sys.exit(1)

    cli = StubCodeMakerCLI(WORKSPACE)
    tables = cli.list_tables()
    if args.table:
        try:
            path = _resolve_table(args.table)
            tables = [path]
        except FileNotFoundError:
            print(f"错误: 找不到表格 {args.table}", file=sys.stderr)
            sys.exit(1)

    total = 0
    for path in tables:
        rel = path.relative_to(WORKSPACE)
        try:
            results = cli.search_rows(path, keyword, sheet=args.sheet or "", col=args.col or "")
        except Exception as e:
            print(f"  跳过 {path.stem}: {e}", file=sys.stderr)
            continue
        if not results:
            continue
        print(f"\n【{rel}】({path.stem})")
        print("-" * 80)
        for r in results:
            header = cli.read_header(path, r.sheet)
            start = cli.data_start_row
            print(f"  Sheet={r.sheet}  Row={r.row}  Col={r.col}({r.col_name})")
            print(f"    匹配: {r.cell_value}")
            parts = []
            for ci, (h, v) in enumerate(zip(header, r.row_data), start=1):
                if v is not None and str(v).strip():
                    parts.append(f"{h}={v}")
            if parts:
                print(f"    行数据: {', '.join(parts)}")
            total += 1

    if total == 0:
        print(f"\n未找到包含 '{keyword}' 的数据")
    else:
        print(f"\n共找到 {total} 条匹配")


def _resolve_name_to_row(cli, path, sheet, name: str) -> int | None:
    """按名称定位行号。优先用 locate_row 在'名称'列找，找不到再搜任意列。"""
    header = cli.read_header(path, sheet)
    # 找到名称列
    name_col = None
    for ci, h in enumerate(header, start=1):
        if h and ('名称' == str(h).strip() or '名字' == str(h).strip() or '名' == str(h).strip()):
            name_col = ci
            break
    if name_col:
        row = cli.locate_row(path, sheet, name_col, name, mode="contains")
        if row is not None:
            return row
    # 回退：搜索任意列
    results = cli.search_rows(path, name, sheet=sheet)
    if results:
        return results[0].row
    return None


def _read_row_data(cli, path: Path, sheet: str, row: int) -> dict[int, Any]:
    """读取指定行的所有列值，返回 {col_idx: value}。"""
    ws = cli._load(path)[sheet]
    result = {}
    header = cli.read_header(path, sheet)
    max_col = len(header)
    for ci in range(1, max_col + 1):
        val = ws.cell(row, ci).value
        if val is not None:
            result[ci] = val
    return result


def _collect_cascade_deletes(cli, path: Path, target_sheet: str, header: list[str],
                             row_data: dict[int, Any], table_stem: str
                             ) -> list[tuple[Path, str, int, str]]:
    """收集级联删除目标（dry-run），不执行任何删除。

    遍历关联文件/sheet，按语义列名匹配找出值相等的行。
    返回 [(rel_path, sheet, row, summary), ...]，供调用方预览或确认。
    """
    pending: list[tuple[Path, str, int, str]] = []  # (path, sheet, row, summary)
    for rel_path in _find_related_files(path, table_stem):
        for rel_sheet in cli.get_sheets(rel_path):
            other_header = cli.read_header(rel_path, rel_sheet)
            rows_to_delete: set[int] = set()
            summaries: dict[int, str] = {}
            for col_idx, val in row_data.items():
                if col_idx > len(header):
                    continue
                col_name = str(header[col_idx - 1])
                variants = _semantic_col_names(col_name)
                matched_ci = None
                for variant in variants:
                    matched_ci = _match_header(variant, other_header, table_stem, rel_sheet)
                    if matched_ci is not None:
                        break
                if matched_ci is None:
                    continue
                ws = cli._load(rel_path)[rel_sheet]
                last_row = cli._last_data_row(ws, cli.data_start_row)
                for r in range(cli.data_start_row, last_row + 1):
                    cell_val = ws.cell(r, matched_ci).value
                    if cell_val is not None and str(cell_val) == str(val):
                        rows_to_delete.add(r)
                        summaries[r] = f"{rel_sheet} 行{r}: {col_name}={val}"
            for r in sorted(rows_to_delete):
                pending.append((rel_path, rel_sheet, r, summaries.get(r, f"{rel_sheet} 行{r}")))
    return pending


def _cascade_delete(cli, path: Path, target_sheet: str, header: list[str],
                    row_data: dict[int, Any], table_stem: str, args=None,
                    cascade: bool = True):
    """级联删除：在关联文件中删除共享键值匹配的行。

    四阶段流程：
    1. 收集阶段 —— 遍历关联文件/sheet，按语义列名匹配找出值相等的行
    2. 展示阶段 —— 列出所有将被删除的行，供用户预览
    3. 确认阶段 —— 交互式询问（除非 --yes 或 --no-cascade）
    4. 执行阶段 —— 逐行调用 delete_row 完成删除

    Args:
        cascade: 显式开关，False 时跳过级联（默认 True 保持兼容）。
                 与 args.no_cascade 任一为真即跳过。
    """
    if args and args.no_cascade:
        return
    if not cascade:
        return
    # ---- 1. 收集阶段 ----
    pending = _collect_cascade_deletes(cli, path, target_sheet, header, row_data, table_stem)

    if not pending:
        return

    # ---- 2. 展示阶段 ----
    print(f"\n  以下 {len(pending)} 条关联数据将被级联删除:")
    for _, _, r, summary in pending:
        rel = path.relative_to(WORKSPACE)
        print(f"    {summary}")

    # ---- 3. 确认阶段 ----
    skip = False
    if args and args.yes:
        pass  # 自动确认
    else:
        try:
            answer = input("  确认级联删除? [y/N]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer != "y":
            print("  已跳过级联删除")
            skip = True

    # ---- 4. 执行阶段 ----
    if not skip:
        for rel_path, rel_sheet, row, _ in pending:
            dr = cli.delete_row(rel_path, rel_sheet, row)
            if dr.ok:
                rel = rel_path.relative_to(WORKSPACE)
                print(f"  ↳ 级联删除: {rel}:{rel_sheet} 行{row}")
            else:
                print(f"  ⚠ 级联删除 {rel_sheet} 行{row} 失败: {dr.error}", file=sys.stderr)


def _cascade_update(cli, path: Path, target_sheet: str, header: list[str],
                    target_row: int, col_idx: int, old_val: Any, new_val: Any,
                    table_stem: str):
    """级联更新：在关联文件中同步更新共享列的值。

    前置检查：old_val 为 None 或新旧值相同时跳过（无意义更新）。
    遍历 _find_related_files 找到的每个关联文件，对其每个 sheet
    调用 _cascade_update_for_file 执行匹配+批量更新。
    """
    if old_val is None or str(old_val) == str(new_val):
        return
    for rel_path in _find_related_files(path, table_stem):
        for rel_sheet in cli.get_sheets(rel_path):
            _cascade_update_for_file(cli, rel_path, rel_sheet, header, col_idx, old_val, new_val, table_stem)


def _cascade_update_for_file(cli, path: Path, target_sheet: str, header: list[str],
                             col_idx: int, old_val: Any, new_val: Any, table_stem: str):
    """对单个文件的单个 sheet 执行级联更新。

    算法：
    1. 从原始 header 取 col_idx 对应的列名 → 生成语义变体集合
    2. 在目标 sheet 表头中用 _match_header 匹配到对应列索引 matched_ci
    3. 扫描目标 sheet 所有数据行，找出 old_val 匹配的单元格
    4. 逐格 write_cell 替换为新值，统计更新行数
    """
    col_name = str(header[col_idx - 1]) if col_idx <= len(header) else ""
    if not col_name:
        return
    other_header = cli.read_header(path, target_sheet)
    variants = _semantic_col_names(col_name)
    matched_ci = None
    for variant in variants:
        matched_ci = _match_header(variant, other_header, table_stem, target_sheet)
        if matched_ci is not None:
            break
    if matched_ci is None:
        return
    ws = cli._load(path)[target_sheet]
    last_row = cli._last_data_row(ws, cli.data_start_row)
    updated = 0
    for r in range(cli.data_start_row, last_row + 1):
        cell_val = ws.cell(r, matched_ci).value
        if cell_val is not None and str(cell_val) == str(old_val):
            wr = cli.write_cell(path, target_sheet, r, matched_ci, new_val)
            if wr.ok:
                updated += 1
    if updated:
        rel = path.relative_to(WORKSPACE)
        print(f"  ↳ 级联更新: {rel}:{target_sheet} {updated}行 col{matched_ci} = {new_val}")


def cmd_delete(args):
    """删除行命令。

    分支：
    - --name 指定名称 → 先 search_rows 精确匹配，找不到则逐 sheet 用 _resolve_name_to_row 回退查找
    - --row 指定行号  → 直接按行号删除
    删除后统一调用 _cascade_delete 处理关联文件中的级联删除。
    """
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    table_stem = path.stem

    # ---- 分支 A: 按名称删除 ----
    if args.name:
        rows = cli.search_rows(path, args.name, sheet=args.sheet)
        if not rows:
            for sn in cli.get_sheets(path):
                target_row = _resolve_name_to_row(cli, path, sn, args.name)
                if target_row is not None:
                    header = cli.read_header(path, sn)
                    row_data = _read_row_data(cli, path, sn, target_row)
                    r = cli.delete_row(path, sn, target_row)
                    if r.ok:
                        print(f"✓ 已删除 {sn} 行{target_row} (名称={args.name})")
                        _cascade_delete(cli, path, sn, header, row_data, table_stem, args)
                    else:
                        print(f"✗ 删除失败: {r.error}", file=sys.stderr)
                        sys.exit(1)
                    return
            print(f"✗ 未找到包含 '{args.name}' 的行", file=sys.stderr)
            sys.exit(1)

        targets = rows
        if len(targets) > 1:
            print(f"找到 {len(targets)} 条匹配:")
            for t in targets:
                print(f"  Sheet={t.sheet} Row={t.row} -> {t.cell_value}")
            print("警告: 将只删除第一条匹配行。使用 --row 精确指定行号。", file=sys.stderr)

        t = targets[0]
        header = cli.read_header(path, t.sheet)
        row_data = _read_row_data(cli, path, t.sheet, t.row)
        r = cli.delete_row(path, t.sheet, t.row)
        if r.ok:
            print(f"✓ 已删除 {t.sheet} 行{t.row} (匹配={t.cell_value})")
            _cascade_delete(cli, path, t.sheet, header, row_data, table_stem, args)
        else:
            print(f"✗ 删除失败: {r.error}", file=sys.stderr)
            sys.exit(1)
        return

    if args.row is None:
        print("错误: 请指定 --row 或 --name", file=sys.stderr)
        sys.exit(1)

    # ---- 分支 B: 按行号删除 ----
    header = cli.read_header(path, args.sheet)
    row_data = _read_row_data(cli, path, args.sheet, args.row)
    r = cli.delete_row(path, args.sheet, args.row)
    if r.ok:
        print(f"✓ 已删除 {args.sheet} 行{args.row}")
        _cascade_delete(cli, path, args.sheet, header, row_data, table_stem, args)
    else:
        print(f"✗ 删除失败: {r.error}", file=sys.stderr)
        sys.exit(1)


def cmd_set(args):
    """写入单元格命令。

    流程：解析表路径 → 构建 KV 对 → 读取旧值 → 逐列写入新值 → 级联更新。
    每个 --key value 对独立处理：先 read_cell 记录旧值，再 write_cell 写入新值，
    然后调用 _cascade_update 同步关联文件中的相同列。
    """
    # ---- 1. 解析表路径 ----
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    header = cli.read_header(path, args.sheet)
    table_stem = path.stem

    # ---- 2. 构建 KV 对 ----
    values = _build_kv(args.kv, table_stem, args.sheet, header)
    if not values:
        print("错误: 未提供有效的列值，格式: --列名 值", file=sys.stderr)
        sys.exit(1)

    # ---- 3. 逐列写入 + 级联更新 ----
    for col_idx, val in values.items():
        # 3a. 读取旧值（用于级联更新时定位需要同步的行）
        old_val = cli.read_cell(path, args.sheet, args.row, col_idx)
        old = old_val.data if old_val.ok else None
        # 3b. 写入新值
        r = cli.write_cell(path, args.sheet, args.row, col_idx, val)
        if r.ok:
            print(f"✓ 已写入 [{args.row}, {col_idx}] = {val}")
            # 3c. 级联更新关联文件
            _cascade_update(cli, path, args.sheet, header, args.row,
                          col_idx, old, val, table_stem)
        else:
            print(f"✗ 写入失败 [{args.row}, {col_idx}]: {r.error}", file=sys.stderr)
            sys.exit(1)


def cmd_get(args):
    """读取单元格/行命令。

    流程：
    1. --name 指定 → 先通过 _resolve_name_to_row 按名称定位行号
    2. --row 指定  → 直接使用行号
    3. --col 指定  → 读取单个单元格 (row, col)
    4. 无 --col     → 读取整行所有非空列并格式化输出
    """
    # ---- 1. 解析表路径 ----
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)

    # ---- 2. 按名称定位行号（若指定了 --name） ----
    if args.name and args.row is None:
        row = _resolve_name_to_row(cli, path, args.sheet, args.name)
        if row is None:
            print(f"✗ 未找到包含 '{args.name}' 的行", file=sys.stderr)
            sys.exit(1)
        args.row = row

    if args.row is None:
        print("错误: 请指定 --row 或 --name", file=sys.stderr)
        sys.exit(1)

    header = cli.read_header(path, args.sheet)

    # ---- 3. 输出：单列或整行 ----
    if args.col is not None:
        # 单列读取
        r = cli.read_cell(path, args.sheet, args.row, args.col)
        if r.ok:
            col_name = header[args.col - 1] if args.col <= len(header) else f"col{args.col}"
            print(f"  [{args.row}, {args.col}] {col_name} = {r.data}")
        else:
            print(f"✗ 读取失败: {r.error}", file=sys.stderr)
    else:
        # 整行读取：遍历所有列，展示非空值
        row_data = {}
        ws = cli._load(path)[args.sheet]
        for ci in range(1, ws.max_column + 1):
            val = ws.cell(args.row, ci).value
            col_name = header[ci - 1] if ci <= len(header) else f"col{ci}"
            row_data[col_name] = val
        print(f"  行 {args.row}:")
        for k, v in row_data.items():
            if v is not None:
                print(f"    {k}: {v}")


# ---- 列级操作命令 ----
def cmd_column_list(args):
    """列出 sheet 所有列。"""
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    cols = cli.list_columns(path, args.sheet)
    if not cols:
        print("未读取到列信息")
        return
    print(f"【{path.stem}:{args.sheet}】共 {len(cols)} 列")
    for ci, name in cols:
        print(f"  [{ci}] {name}")


def cmd_column_add(args):
    """新增列。默认追加末尾，--after 指定插入位置。"""
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    after = None
    if args.after:
        if args.after.isdigit():
            after = int(args.after)
        else:
            header = cli.read_header(path, args.sheet)
            after = _match_header(args.after, header, path.stem, args.sheet)
            if after is None:
                print(f"✗ 未找到插入位置列: {args.after}", file=sys.stderr)
                sys.exit(1)
    r = cli.insert_column(path, args.sheet, args.name, after=after, type_str=args.type)
    if r.ok:
        pos = f"在列{after}后" if after else "末尾"
        extra = f" 类型={args.type}" if args.type else ""
        print(f"✓ 已新增列 {pos} col{r.data['col']}({args.name}){extra}")
    else:
        print(f"✗ 新增列失败: {r.error}", file=sys.stderr)
        sys.exit(1)


def cmd_column_delete(args):
    """删除列。--name 列名 或 --col 列号。"""
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    col = args.col if args.col is not None else args.name
    if col is None:
        print("错误: 请指定 --name 或 --col", file=sys.stderr)
        sys.exit(1)
    r = cli.delete_column(path, args.sheet, col)
    if r.ok:
        print(f"✓ 已删除列 col{r.data['col']}")
    else:
        print(f"✗ 删除列失败: {r.error}", file=sys.stderr)
        sys.exit(1)


def cmd_column_rename(args):
    """重命名列。"""
    path = _resolve_table(args.table)
    cli = StubCodeMakerCLI(WORKSPACE)
    r = cli.rename_column(path, args.sheet, args.name, args.to)
    if r.ok:
        print(f"✓ 已重命名列 col{r.data['col']}: {r.data['old']} -> {r.data['new']}")
    else:
        print(f"✗ 重命名列失败: {r.error}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(description="Excel 表格通用 CLI 工具")
    sub = parser.add_subparsers(dest="command", help="操作命令")

    # list
    sub.add_parser("list", help="列出所有表格")

    # sheets
    p = sub.add_parser("sheets", help="列出 sheet")
    p.add_argument("table", help="表格名/路径")

    # header
    p = sub.add_parser("header", help="查看表头")
    p.add_argument("table", help="表格名/路径")
    p.add_argument("sheet", help="Sheet 名称")

    # read
    p = sub.add_parser("read", help="读取数据")
    p.add_argument("table", help="表格名/路径")
    p.add_argument("sheet", help="Sheet 名称")
    p.add_argument("--json", action="store_true", help="JSON 格式输出")

    # search
    p = sub.add_parser("search", help="按关键词搜索")
    p.add_argument("keyword", help="搜索关键词")
    p.add_argument("--table", default=None, help="限定表格名/路径")
    p.add_argument("--sheet", default="", help="限定 sheet 名")
    p.add_argument("--col", default="", help="限定列名或列号")

    # add
    p = sub.add_parser("add", help="追加新行")
    p.add_argument("table", help="表格名/路径")
    p.add_argument("sheet", help="Sheet 名称")
    p.add_argument("kv", nargs=argparse.REMAINDER, help="--列名 值 [--列名 值 ...]")

    # delete
    p = sub.add_parser("delete", help="删除行")
    p.add_argument("table", help="表格名/路径")
    p.add_argument("sheet", help="Sheet 名称")
    p.add_argument("--row", type=int, default=None, help="行号")
    p.add_argument("--name", default=None, help="按名称定位删除")
    p.add_argument("--yes", "-y", action="store_true", help="跳过级联删除确认")
    p.add_argument("--no-cascade", action="store_true", help="跳过级联删除")

    # set
    p = sub.add_parser("set", help="写入单元格")
    p.add_argument("--row", type=int, required=True, help="行号")
    p.add_argument("table", help="表格名/路径")
    p.add_argument("sheet", help="Sheet 名称")
    p.add_argument("kv", nargs=argparse.REMAINDER, help="--列名 值")

    # get
    p = sub.add_parser("get", help="读取单元格/行")
    p.add_argument("table", help="表格名/路径")
    p.add_argument("sheet", help="Sheet 名称")
    p.add_argument("--row", type=int, default=None, help="行号")
    p.add_argument("--name", default=None, help="按名称定位查询")
    p.add_argument("--col", type=int, default=None, help="列号(1-based)，不指定则读取整行")

    # column（列级操作）
    p = sub.add_parser("column", help="列级操作: list/add/delete/rename")
    col_sub = p.add_subparsers(dest="col_action", help="列操作类型")

    pc = col_sub.add_parser("list", help="列出所有列")
    pc.add_argument("table", help="表格名/路径")
    pc.add_argument("sheet", help="Sheet 名称")

    pc = col_sub.add_parser("add", help="新增列")
    pc.add_argument("table", help="表格名/路径")
    pc.add_argument("sheet", help="Sheet 名称")
    pc.add_argument("name", help="新列名")
    pc.add_argument("--after", default=None, help="插入位置(列名或列号)，默认追加末尾")
    pc.add_argument("--type", default=None, help="类型标注(写入类型行)")

    pc = col_sub.add_parser("delete", help="删除列")
    pc.add_argument("table", help="表格名/路径")
    pc.add_argument("sheet", help="Sheet 名称")
    pc.add_argument("--name", default=None, help="列名")
    pc.add_argument("--col", default=None, help="列号(1-based)")

    pc = col_sub.add_parser("rename", help="重命名列")
    pc.add_argument("table", help="表格名/路径")
    pc.add_argument("sheet", help="Sheet 名称")
    pc.add_argument("--name", required=True, help="旧列名")
    pc.add_argument("--to", required=True, help="新列名")

    args = parser.parse_args()

    if args.command == "list" or args.command is None:
        cmd_list()
    elif args.command == "sheets":
        cmd_sheets(args)
    elif args.command == "header":
        cmd_header(args)
    elif args.command == "read":
        cmd_read(args)
    elif args.command == "search":
        cmd_search(args)
    elif args.command == "add":
        cmd_add(args)
    elif args.command == "delete":
        cmd_delete(args)
    elif args.command == "set":
        cmd_set(args)
    elif args.command == "get":
        cmd_get(args)
    elif args.command == "column":
        if args.col_action == "list":
            cmd_column_list(args)
        elif args.col_action == "add":
            cmd_column_add(args)
        elif args.col_action == "delete":
            cmd_column_delete(args)
        elif args.col_action == "rename":
            cmd_column_rename(args)
        else:
            print("用法: column {list|add|delete|rename} ...")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

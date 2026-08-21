"""扩展 demo_svn fixture，新增 4 个小表分支/子目录，并保证 dev1-4 任两两都有冲突。

布局（位置严格区分）：
  平行分支（branches/ 下，与 dev1/dev2 同级；subdev_1 不再作分支，改 trunk 子目录）：
    /branches/dev3     svn copy /trunk@R，删 3 大表，改 ability/reward/tips
    /branches/dev4     svn copy /trunk@R，删 3 大表，改 const/fabao/guild
  trunk 下子目录（私有表集合，目录合并 source）：
    /trunk/subdev_1/   随 trunk 导入（5 张私有表：ability/const/item_drop/monster/reward）
    /trunk/subdev_2/   5 张小私有表 {const,fabao,guild,mail,tips}（B2 改私有标记）
    /trunk/subdev_3/   5 张小私有表 {ability,reward,map,interaction,world_buff}
  全配对冲突锚点：dev1/dev2/dev3/dev4 的 tips.xlsx 首个 sheet B2 各写不同值，
    使任两分支合并都在该单元格冲突。

排除大表 monster(5.6MB)/skill_level(2.8MB)/item_drop(2.4MB)，仅留小表测合并时间。
依赖：build_svn_real.py 已建好 repo + trunk + dev1/dev2（subdev_1 随 trunk 导入）。

合并引导可见：
  /api/merge/branch/dirs 列出 svn/demo_svn/wc/trunk + branches/{dev1..4}（不含 subdev_1）
  /api/merge/subdir/dirs 递归扫描列出 svn/demo_svn/wc/trunk/subdev_{1,2,3}

幂等：分支/子目录已存在则跳过，仅补改表。
用法：
    python merge/scripts/build_svn_small_branches.py
    python merge/scripts/build_svn_small_branches.py --clean-branches  # 删旧 dev3/dev4 + trunk/subdev_2|3 + 遗留 subdev_1 再重建
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
MERGE_DIR = SCRIPT_DIR.parent
SVN_DEMO_DIR = MERGE_DIR / "svn" / "demo_svn"
REPO_DIR = SVN_DEMO_DIR / "repo"
WC_DIR = SVN_DEMO_DIR / "wc"
TRUNK_WC = WC_DIR / "trunk"
DEFAULT_SRC = MERGE_DIR / "_seed_data"

# 排除的 3 张大表（均 >2MB，compare 阶段 O(n^2) 卡顿）
BIG_TABLES = {"monster.xlsx", "skill_level.xlsx", "item_drop.xlsx"}

# 平行分支（branches/ 下）
NEW_BRANCHES = ["dev3", "dev4"]
DEV3_MODIFY = ["ability.xlsx", "reward.xlsx", "tips.xlsx"]
DEV4_MODIFY = ["const.xlsx", "fabao.xlsx", "guild.xlsx"]

# trunk 下子目录（目录合并 source），各保留 5 张小私有表（与 subdev_1 集合错开）
NEW_SUBDIRS = ["subdev_2", "subdev_3"]
SUBDEV2_KEEP = ["const.xlsx", "fabao.xlsx", "guild.xlsx", "mail.xlsx", "tips.xlsx"]
SUBDEV3_KEEP = ["ability.xlsx", "reward.xlsx", "map.xlsx", "interaction.xlsx", "world_buff.xlsx"]

# dev1-4 全面冲突场景：分散多表 + 同表不同 sheet + 新增 sheet(结构差异) + 新增行(含同 PK 冲突)。
# 每个 dev 都参与每场景（写本分支值），故任两 dev 合并在每场景都冲突/差异。用户要求
# "不应集中在一个表格，要分散多表、同表不同 sheet、新增 sheet 和行"。
# 1) content 冲突：5 个 表/sheet 组合（4 张表，fabao 跨 2 sheet 展示同表不同 sheet）
RICH_CONTENT = [
    ("ability.xlsx", "Ability", 2, (2, 3)),     # 2 数据行 × (B,C) = 4 格
    ("reward.xlsx", "Reward", 2, (2,)),          # 2 格
    ("const.xlsx", "Const", 2, (2,)),            # 2 格
    ("fabao.xlsx", "Fabao", 2, (2,)),            # 2 格（同表 sheet1）
    ("fabao.xlsx", "FabaoLevel", 2, (2,)),      # 2 格（同表 sheet2 → 同表不同 sheet 冲突）
]
# 2) 新增 sheet（结构差异：每分支加 DevNote_{branch}，合并不在 common → extra/missing sheet）
RICH_NEW_SHEET_TABLE = "const.xlsx"
# 3) 新增行（插入）：每分支独有 PK（单向 inserted）+ 共享 PK 9999（不同内容 → 同 PK 插入冲突）
RICH_INSERT_TABLE = "ability.xlsx"
RICH_INSERT_SHEET = "Ability"
RICH_INSERT_SHARED_PK = 9999  # 所有分支都插此 PK，写不同值 → 合并时插入行 PK 冲突
ALL_BRANCHES = ["dev1", "dev2", "dev3", "dev4"]

# 目录合并（subdev_N → trunk）冲突制造：subdev_N/table 与 trunk/table 同名表同单元格
# 双方都改、值不同即冲突。stage4 只改 subdev 一侧（B2 私有标记）→ 单向变更非冲突。
# 用户反馈"subdev_2 和 trunk 没有冲突、数量太少无参考价值"，要求增至 10-20 个。
# 3 单元格 × 5 表 × 2 子目录 = 30 个双方冲突（每子目录 15，落在 10-20）。
SUBDIR_CONFLICT_CELLS = [(2, 2), (2, 3), (2, 4)]  # 旧固定格（已废，改 _find_data_rows 动态取行）
SUBDIR_ROW_COUNT = 3        # 每表取 3 个唯一 PK 数据行
SUBDIR_COLS = (2,)          # 每行改 1 格(B) → 3 表×3 行=9~15 冲突/子目录（落在 10-20）


def _run(cmd, check=True, timeout=600):
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode != 0:
        err = (r.stderr or "").strip()
        out = (r.stdout or "").strip()
        raise RuntimeError(f"命令失败 [{' '.join(cmd)}]\n  stdout: {out[:800]}\n  stderr: {err[:800]}")
    return r


def _svn(*args, check=True, timeout=600):
    return _run(["svn", *args], check=check, timeout=timeout)


def _repo_url() -> str:
    p = str(REPO_DIR).replace("\\", "/")
    return f"file:///{p}"


def _head_rev() -> int:
    r = _svn("info", "--show-item", "revision", _repo_url())
    return int(r.stdout.strip())


def _branch_exists(branch: str) -> bool:
    r = _svn("info", f"{_repo_url()}/branches/{branch}", check=False)
    return r.returncode == 0


def _svn_path_exists(url_path: str) -> bool:
    r = _svn("info", url_path, check=False)
    return r.returncode == 0


def _set_cell(xlsx: Path, sheet: str, row: int, col: int, value) -> bool:
    from openpyxl import load_workbook
    wb = load_workbook(xlsx)
    if sheet not in wb.sheetnames:
        wb.close()
        return False
    ws = wb[sheet]
    if ws.cell(row, col).value == value:
        wb.close()
        return False
    ws.cell(row, col).value = value
    wb.save(xlsx)
    wb.close()
    return True


def _first_sheet(xlsx: Path) -> str:
    from openpyxl import load_workbook
    wb = load_workbook(xlsx, read_only=True)
    name = wb.sheetnames[0]
    wb.close()
    return name


# 非数据 sheet 关键词（与 server/engine/id_scope._NON_DATA_SHEET_KEYWORDS 对齐）：
# 合并 compare 跳过这些 sheet，故冲突标记必须写在数据 sheet 上才可见。
# tips.xlsx 等"首个 sheet 是 CONFIG"的表，_first_sheet 会取到 CONFIG（被跳过）→ 标记不可见。
_NON_DATA_SHEET_KEYWORDS = ("CONFIG", "PATCH_CONFIG", "说明", "SETTING", "INDEX")


def _first_data_sheet(xlsx: Path) -> str:
    """首个数据 sheet（跳过 CONFIG/PATCH_CONFIG/说明 等被合并 compare 忽略的 sheet）。
    全是非数据 sheet 时回退首个（保旧行为）。"""
    from openpyxl import load_workbook
    wb = load_workbook(xlsx, read_only=True)
    names = wb.sheetnames
    wb.close()
    for n in names:
        if not any(kw in n for kw in _NON_DATA_SHEET_KEYWORDS):
            return n
    return names[0]


def _svn_commit_as(wc_path: Path, msg: str, username: str):
    """svn commit 用指定作者。file:// repo 用 --username 设 svn:author，使两侧不同作者
    → compare 的 commit_authors 判定为真冲突（不同作者）而非同作者自动合并（D3 规则
    会把同作者同格改动自动合并、不算冲突）。--non-interactive --no-auth-cache 防 prompt。
    """
    return _svn("commit", str(wc_path), "--username", username, "--password", "",
                "--non-interactive", "--no-auth-cache", "-m", msg)


def _find_data_rows(xlsx: Path, sheet: str, count: int) -> list[int]:
    """返回前 count 个"A 列非空且唯一"的行号（数据行）。compare 按主键(A 列)行匹配，
    空/重复 A 列的行会折叠成 1 行 → 15 格只出 3 冲突（tips.xlsx 的坑）。故冲突单元格
    必须写在 A 列唯一的数据行上。从 row 3 起跳过表头(1)+类型(2)。
    """
    from openpyxl import load_workbook
    wb = load_workbook(xlsx, read_only=True)
    ws = wb[sheet] if sheet in wb.sheetnames else wb[wb.sheetnames[0]]
    rows: list[int] = []
    seen: set[str] = set()
    for r in range(3, (ws.max_row or 0) + 1):
        v = ws.cell(r, 1).value
        if v is None or v == "":
            continue
        key = str(v)
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
        if len(rows) >= count:
            break
    wb.close()
    return rows


def _edit_xlsx(xlsx: Path, fn) -> bool:
    """可写打开 xlsx，应用 fn(wb)（多编辑批量），保存。返回是否有写盘（fn 返回 True 表改了）。
    比 _set_cell 逐格 open/save 高效，且支持 create_sheet/insert_rows 等结构编辑。
    """
    from openpyxl import load_workbook
    wb = load_workbook(xlsx)
    changed = bool(fn(wb))
    if changed:
        wb.save(xlsx)
    wb.close()
    return changed


def _ensure_sheet(wb, sheet: str) -> bool:
    """确保 sheet 存在（不存在则建空 sheet）。返回是否新建（True=本次新建）。"""
    if sheet in wb.sheetnames:
        return False
    wb.create_sheet(sheet)
    return True


def _append_row(ws, values: list) -> bool:
    """在 ws 末尾追加一行（values[0]→A, [1]→B…）。返回 True。"""
    ws.append(values)
    return True


def _data_rows_ws(ws, count: int) -> list[int]:
    """同 _find_data_rows，但直接在已打开的 ws 上找（供 _edit_xlsx 内批量编辑用）。"""
    rows: list[int] = []
    seen: set[str] = set()
    for r in range(3, (ws.max_row or 0) + 1):
        v = ws.cell(r, 1).value
        if v is None or v == "":
            continue
        key = str(v)
        if key in seen:
            continue
        seen.add(key)
        rows.append(r)
        if len(rows) >= count:
            break
    return rows


def _pk_row(ws, pk) -> int:
    """返回 A 列等于 pk 的行号，无则 0。"""
    for r in range(3, (ws.max_row or 0) + 1):
        if ws.cell(r, 1).value == pk:
            return r
    return 0


def stage0_check_base():
    print("=== [0/5] 检查基础 repo 是否就绪 ===")
    if not REPO_DIR.exists() or not TRUNK_WC.is_dir():
        print(f" {TRUNK_WC} 不存在。", file=sys.stderr)
        print("  请先运行：python merge/scripts/setup_svn_demo.py", file=sys.stderr)
        sys.exit(2)
    rev = _head_rev()
    print(f"  repo 就绪，HEAD r{rev}")
    return rev


def stage1_branch_off(trunk_rev: int, url: str):
    """从 /trunk@r 切出 dev3/dev4 平行分支（已存在则跳过）。"""
    print(f"=== [1/5] 从 /trunk@r{trunk_rev} 切出平行分支 dev3/dev4 ===")
    for name in NEW_BRANCHES:
        if _branch_exists(name):
            print(f"  branches/{name}: 已存在，跳过 copy")
            continue
        _svn("copy", f"{url}/trunk@{trunk_rev}", f"{url}/branches/{name}",
             "-m", f"{name}: 从 trunk@r{trunk_rev} 切出（小表分支，排除 {', '.join(sorted(BIG_TABLES))}）")
        print(f"  branches/{name}: 已切出")


def stage2_checkout(url: str):
    print("=== [2/5] checkout dev3/dev4 工作副本 ===")
    for name in NEW_BRANCHES:
        wc = WC_DIR / "branches" / name
        if (wc / ".svn").is_dir():
            print(f"  wc/branches/{name}: 已存在，revert+update")
            _svn("revert", "-R", str(wc), check=False)
            _svn("update", str(wc))
            continue
        wc.mkdir(parents=True, exist_ok=True)
        _svn("checkout", f"{url}/branches/{name}", str(wc))
        print(f"  wc/branches/{name}")


def stage3_dev_branch(branch: str, modify_tables: list[str]):
    """dev3/dev4：删 3 大表，改若干小表（与 trunk 形成差异）。"""
    print(f"=== [3/5] 整理 branches/{branch}：删大表 + 改小表 ===")
    wc = WC_DIR / "branches" / branch
    for big in BIG_TABLES:
        p = wc / big
        if p.exists():
            _svn("rm", str(p))
            print(f"  rm {big}")
    for name in modify_tables:
        fp = wc / name
        if not fp.exists():
            print(f"  跳过 {name}（不存在）")
            continue
        sheet = _first_sheet(fp)
        marker = f"{branch}_mark"
        if _set_cell(fp, sheet, 2, 2, marker):
            print(f"  {name}!{sheet}!B2 = {marker}")
        else:
            print(f"  {name}!{sheet}!B2 已是 {marker}，跳过")
    _svn("commit", str(wc), "-m",
         f"{branch}: 删除 3 大表({', '.join(sorted(BIG_TABLES))})，改 {len(modify_tables)} 张小表({', '.join(modify_tables)})")
    print(f"  {branch} 提交完成")


def stage4_trunk_subdirs(url: str):
    """在 trunk 工作副本下创建 subdev_2/subdev_3 子目录（目录合并 source）。

    子目录内放 5 张小私有表：从 trunk 顶层拷入，改 B2 作私有标记，svn add 子目录
    （递归加入未版本化子项）后提交到 trunk。与 subdev_1（trunk/subdev_1/）同级，
    前端 /api/merge/subdir/dirs 递归扫描可见。
    """
    print("=== [4/5] 在 trunk 下创建 subdev_2/subdev_3 子目录 ===")
    for subname, keep in [("subdev_2", SUBDEV2_KEEP), ("subdev_3", SUBDEV3_KEEP)]:
        sdir = TRUNK_WC / subname
        existed = sdir.is_dir() and (sdir / ".svn").is_dir()
        sdir.mkdir(parents=True, exist_ok=True)
        marker = f"{subname}_priv"
        for name in keep:
            src_fp = TRUNK_WC / name
            if not src_fp.exists():
                print(f"  警告：trunk/{name} 不存在，跳过", file=sys.stderr)
                continue
            dst = sdir / name
            shutil.copy2(src_fp, dst)
            sheet = _first_sheet(dst)
            _set_cell(dst, sheet, 2, 2, marker)
            print(f"  {subname}/{name}!{sheet}!B2 = {marker}")
        # svn add 子目录本身（递归加入未版本化子项；已版本化则 check=False 跳过）
        if not existed:
            _svn("add", str(sdir), check=False)
        msg = (f"trunk/{subname}: 新增私有子目录（{len(keep)} 张小表："
               f"{', '.join(keep)}），目录合并 source" if not existed
               else f"trunk/{subname}: 刷新私有表标记")
        # 提交整个 trunk wc（子目录变更随 trunk 一起进版本库）
        _svn("commit", str(TRUNK_WC), "-m", msg)
        print(f"  trunk/{subname} 提交完成")


def stage_subdir_trunk_conflicts():
    """为 subdev_2/subdev_3 vs trunk 制造真实冲突（每子目录 ~15 个，落在用户要求的 10-20）。

    目录合并 source=subdev_N（trunk 子目录）→ target=trunk，base=subdev 创建时 trunk 快照。
    冲突 = subdev_N/table 与 trunk/table 同名表同单元格都改且值不同。stage4 只改 subdev 一侧
    （B2 私有标记）→ 单向变更非冲突。本阶段对每张表在 subdev 与 trunk 同名表上同改 3 个
    单元格（B2/C2/D2）写不同值，构成双方都改的真冲突。subdev 子目录与 trunk 同名表都在
    trunk 工作副本下，一次 commit trunk wc 覆盖两侧。
    """
    print("=== [5/7] subdev_2/3 vs trunk 冲突制造（3 单元格 × 5 表 × 2 子目录）===")
    pairs = [("subdev_2", SUBDEV2_KEEP), ("subdev_3", SUBDEV3_KEEP)]
    n_conflict = 0
    # 分两阶段提交、两侧不同 svn 作者：commit_authors 同作者自动合并（D3）会把同作者同格
    # 改动算自动合并、不算冲突。故 subdev 侧用作者 subname 提交，trunk 侧用作者 trunk 提交
    # → 真冲突。先改+提交 subdev 子树（仅 subdev 侧），再改+提交 trunk 顶层（仅 trunk 侧）。
    # 行号由 _find_data_rows 动态取（A 列唯一数据行）——subdev 副本与 trunk 同名表 A 列一致，
    # 故两侧取到同一批行号，单元格对齐才冲突（空/重复 A 列的行 compare 会折叠成 1 行）。
    # 每表 SUBDIR_ROW_COUNT 行 × SUBDIR_COLS 列，5 表 = ~15 冲突/子目录（落在 10-20）。
    table_rows: dict[str, list[int]] = {}  # tname -> 数据行号（两侧共用）
    for subname, keep in pairs:
        sub_changed = 0
        for tname in keep:
            sub_fp = TRUNK_WC / subname / tname
            trunk_fp = TRUNK_WC / tname
            if not sub_fp.exists() or not trunk_fp.exists():
                print(f"  警告：{subname}/{tname} 或 trunk/{tname} 不存在，跳过", file=sys.stderr)
                continue
            sheet = _first_data_sheet(sub_fp)
            rows = table_rows.setdefault(tname, _find_data_rows(sub_fp, sheet, SUBDIR_ROW_COUNT))
            if not rows:
                print(f"  警告：{tname}!{sheet} 无 A 列唯一数据行，跳过", file=sys.stderr)
                continue
            for r in rows:
                for c in SUBDIR_COLS:
                    if _set_cell(sub_fp, sheet, r, c, f"{subname}_conf_r{r}c{c}"):
                        sub_changed += 1
                    n_conflict += 1
            print(f"  {subname}/{tname}!{sheet}：subdev 侧 {len(rows)} 行 × {len(SUBDIR_COLS)} 格")
        # 提交 subdev 子树（作者=subname）
        if sub_changed:
            _svn_commit_as(TRUNK_WC / subname,
                 f"{subname}: 目录合并 source 侧改 {sub_changed} 格（作者 {subname}，与 trunk 侧构成真冲突）",
                 subname)
            print(f"  trunk/{subname} 提交完成（作者 {subname}，{sub_changed} 格）")
    # trunk 顶层侧：对每张表同批数据行同格写不同值，最后一次性提交（作者=trunk）
    trunk_changed = 0
    for subname, keep in pairs:
        for tname in keep:
            trunk_fp = TRUNK_WC / tname
            sub_fp = TRUNK_WC / subname / tname
            if not sub_fp.exists() or not trunk_fp.exists():
                continue
            sheet = _first_data_sheet(sub_fp)
            rows = table_rows.get(tname, [])
            if not rows:
                continue
            for r in rows:
                for c in SUBDIR_COLS:
                    if _set_cell(trunk_fp, sheet, r, c, f"trunk_{subname}_r{r}c{c}"):
                        trunk_changed += 1
            print(f"  trunk/{tname}!{sheet}：trunk 侧 {len(rows)} 行 × {len(SUBDIR_COLS)} 格")
    if trunk_changed:
        _svn_commit_as(TRUNK_WC,
             f"trunk: 目录合并 target 侧改 {trunk_changed} 格（作者 trunk，与 subdev 侧构成真冲突）",
             "trunk")
        print(f"  trunk 顶层提交完成（作者 trunk，{trunk_changed} 格）")
    print(f"  共 {n_conflict} 个冲突单元格（subdev_2、subdev_3 各 ~{n_conflict // 2}），两侧分作者提交")


def stage_rich_dev_conflicts():
    """dev1-4 全面冲突：分散多表 + 同表不同 sheet + 新增 sheet + 新增行(含同 PK 冲突)。

    用户要求"不应集中在一个表格，要分散多表、同表不同 sheet、新增 sheet 和行"。
    每个 dev 都参与每场景（写本分支值），任两 dev 合并在每场景都冲突/差异：
      1. content：5 个 表/sheet 组合（ability/reward/const/fabao×2 sheet），每表 2 数据行
      2. 新增 sheet：每分支在 const.xlsx 加 DevNote_{branch}（合并不在 common → 结构差异）
      3. 新增行：ability!Ability 插本分支独有 PK（单向 inserted）+ 共享 PK 9999（不同值 → 同 PK 插入冲突）
    分作者提交（--username {branch}）避同作者自动合并。幂等：值/sheet/行已存在则跳过。
    """
    print("=== [6/7] dev1-4 全面冲突(多表/多sheet/新sheet/新行) ===")
    for bi, branch in enumerate(ALL_BRANCHES):
        wc = WC_DIR / "branches" / branch
        edits = 0

        # 1+3) ability.xlsx：content(2 行×2 格) + 新增行(2 独有 PK + 共享 PK 9999)
        def _abl(wb, b=branch, i=bi):
            ch = 0
            ws = wb["Ability"] if "Ability" in wb.sheetnames else wb.active
            for r in _data_rows_ws(ws, 2):
                for c in (2, 3):
                    v = f"{b}_abl_r{r}c{c}"
                    if ws.cell(r, c).value != v:
                        ws.cell(r, c).value = v; ch += 1
            for pk in (9000 + i * 10 + 1, 9000 + i * 10 + 2, RICH_INSERT_SHARED_PK):
                v = f"{b}_ins_{pk}"
                rr = _pk_row(ws, pk)
                if rr == 0:
                    ws.append([pk, v, v, v]); ch += 1
                elif ws.cell(rr, 2).value != v:
                    ws.cell(rr, 2).value = v; ch += 1
            return ch > 0
        if (wc / "ability.xlsx").exists() and _edit_xlsx(wc / "ability.xlsx", _abl):
            edits += 1

        # reward.xlsx：content(2 行×B)
        def _rwd(wb, b=branch):
            ch = 0
            ws = wb["Reward"] if "Reward" in wb.sheetnames else wb.active
            for r in _data_rows_ws(ws, 2):
                v = f"{b}_rwd_r{r}"
                if ws.cell(r, 2).value != v:
                    ws.cell(r, 2).value = v; ch += 1
            return ch > 0
        if (wc / "reward.xlsx").exists() and _edit_xlsx(wc / "reward.xlsx", _rwd):
            edits += 1

        # const.xlsx：content(2 行×B) + 新增 DevNote_{branch} sheet（结构差异）
        def _cst(wb, b=branch):
            ch = 0
            ws = wb["Const"] if "Const" in wb.sheetnames else wb.active
            for r in _data_rows_ws(ws, 2):
                v = f"{b}_cst_r{r}"
                if ws.cell(r, 2).value != v:
                    ws.cell(r, 2).value = v; ch += 1
            sn = f"DevNote_{b}"
            if sn not in wb.sheetnames:
                nws = wb.create_sheet(sn)
                nws.append(["key", "note"]); nws.append([1, f"{b}_note"])
                ch += 1
            return ch > 0
        if (wc / "const.xlsx").exists() and _edit_xlsx(wc / "const.xlsx", _cst):
            edits += 1

        # fabao.xlsx：content 跨 2 sheet(Fabao + FabaoLevel) → 同表不同 sheet 冲突
        def _fab(wb, b=branch):
            ch = 0
            for sn in ("Fabao", "FabaoLevel"):
                if sn not in wb.sheetnames:
                    continue
                ws = wb[sn]
                for r in _data_rows_ws(ws, 2):
                    v = f"{b}_fab_{sn}_r{r}"
                    if ws.cell(r, 2).value != v:
                        ws.cell(r, 2).value = v; ch += 1
            return ch > 0
        if (wc / "fabao.xlsx").exists() and _edit_xlsx(wc / "fabao.xlsx", _fab):
            edits += 1

        if edits:
            _svn_commit_as(wc,
                 f"{branch}: 全面冲突(5 表/sheet content + DevNote_{branch} 新 sheet + ability 新行含同 PK 冲突)，分作者提交",
                 branch)
            print(f"  branches/{branch}: content(ability/reward/const/fabao×2) + DevNote_{branch} sheet + ability 新行")
        else:
            print(f"  branches/{branch}: 已是本分支值，跳过")


def stage5_verify(url: str, trunk_rev: int):
    print(f"=== [7/7] 验证布局 ===")
    import re
    ok_all = True
    # dev3/dev4：copyfrom=/trunk 且 rev <= trunk_rev（分支切点可能早于当前 HEAD）+ 大表已删
    for name in NEW_BRANCHES:
        wc = WC_DIR / "branches" / name
        r = _svn("log", "-v", "--stop-on-copy", "--xml", str(wc), check=False)
        out = r.stdout or ""
        cf_path = re.search(r'copyfrom-path="([^"]+)"', out)
        cf_rev = re.search(r'copyfrom-rev="([^"]+)"', out)
        cf_ok = (cf_path and cf_path.group(1).endswith("/trunk") and
                 cf_rev and int(cf_rev.group(1)) <= trunk_rev)
        big_missing = all(not (wc / b).exists() for b in BIG_TABLES)
        status = "OK" if (cf_ok and big_missing) else "WARN"
        if status != "OK":
            ok_all = False
        print(f"  branches/{name}: copyfrom={'/trunk@r'+cf_rev.group(1) if cf_rev else '?'} "
              f"大表已删={big_missing} [{status}]")
    # trunk/subdev_2|3：子目录存在 + 含 5 张小表
    for subname, keep in [("subdev_2", SUBDEV2_KEEP), ("subdev_3", SUBDEV3_KEEP)]:
        sdir = TRUNK_WC / subname
        has_all = all((sdir / n).exists() for n in keep)
        status = "OK" if has_all else "WARN"
        if status != "OK":
            ok_all = False
        print(f"  trunk/{subname}: 含 {len(keep)} 张小表={has_all} [{status}]")
    return ok_all


def _clean(url: str):
    """删除已存在的 dev3/dev4 分支 + trunk/subdev_2|3 子目录 + 遗留的 branches/subdev_2|3 脏分支。"""
    print("=== [clean] 删除旧 dev3/dev4 + trunk/subdev_2|3 + 遗留 branches/subdev_2|3 ===")
    for name in NEW_BRANCHES:
        if _branch_exists(name):
            _svn("rm", f"{url}/branches/{name}", "-m", f"clean: 删除旧 {name}")
            print(f"  删除 /branches/{name}")
        wc = WC_DIR / "branches" / name
        if (wc / ".svn").is_dir():
            shutil.rmtree(wc, onerror=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
            print(f"  删除 wc/branches/{name}")
    # 遗留：旧脚本曾把 subdev_2|3 建成 branches/ 下分支，此处一并清掉
    for subname in NEW_SUBDIRS:
        if _branch_exists(subname):
            _svn("rm", f"{url}/branches/{subname}", "-m", f"clean: 删除遗留 branches/{subname}（应作 trunk 子目录）")
            print(f"  删除遗留 /branches/{subname}")
        wc = WC_DIR / "branches" / subname
        if (wc / ".svn").is_dir():
            shutil.rmtree(wc, onerror=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
            print(f"  删除遗留 wc/branches/{subname}")
    # 遗留：旧脚本曾把 subdev_1 建成 branches/ 下分支（现改作 trunk 子目录），一并清掉
    if _branch_exists("subdev_1"):
        _svn("rm", f"{url}/branches/subdev_1", "-m",
             "clean: 删除遗留 branches/subdev_1（subdev_1 改作 trunk 子目录，不再作分支）")
        print(f"  删除遗留 /branches/subdev_1")
    wc_sub1 = WC_DIR / "branches" / "subdev_1"
    if (wc_sub1 / ".svn").is_dir() or wc_sub1.exists():
        shutil.rmtree(wc_sub1, onerror=lambda f, p, e: (os.chmod(p, stat.S_IWRITE), f(p)))
        print(f"  删除遗留 wc/branches/subdev_1")
    # trunk 下子目录：svn rm 后随 trunk commit 删除
    for subname in NEW_SUBDIRS:
        sdir = TRUNK_WC / subname
        if sdir.is_dir() and (sdir / ".svn").is_dir():
            _svn("rm", "-R", str(sdir), check=False)
            print(f"  svn rm trunk/{subname}")
    status_out = _svn("status", str(TRUNK_WC), check=False).stdout
    if status_out.strip():
        _svn("commit", str(TRUNK_WC), "-m", "clean: 删除 trunk/subdev_2|3 子目录")
        print("  提交 trunk 子目录删除")


def main():
    ap = argparse.ArgumentParser(description="扩展 demo_svn：新增 dev3/dev4 平行分支 + trunk/subdev_2|3 子目录")
    ap.add_argument("--src", default=str(DEFAULT_SRC), help=f"源数据目录（默认 {DEFAULT_SRC}）")
    ap.add_argument("--clean-branches", action="store_true",
                    help="先删除 dev3/dev4 分支 + trunk/subdev_2|3 子目录再重建")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print(f"错误：源目录不存在: {src}", file=sys.stderr)
        sys.exit(2)

    trunk_rev = stage0_check_base()
    url = _repo_url()

    if args.clean_branches:
        _clean(url)

    stage1_branch_off(trunk_rev, url)
    stage2_checkout(url)
    stage3_dev_branch("dev3", DEV3_MODIFY)
    stage3_dev_branch("dev4", DEV4_MODIFY)
    stage4_trunk_subdirs(url)
    stage_subdir_trunk_conflicts()
    stage_rich_dev_conflicts()
    ok = stage5_verify(url, trunk_rev)

    print("\n" + "=" * 60)
    print("小表分支/子目录扩展完成。")
    print("  平行分支（/api/merge/branch/dirs）：")
    print("    svn/demo_svn/wc/branches/dev3  ← 跨分支合并（小表，已删 monster/skill_level/item_drop）")
    print("    svn/demo_svn/wc/branches/dev4  ← 跨分支合并（小表）")
    print("  trunk 下子目录（/api/merge/subdir/dirs 递归扫描）：")
    print("    svn/demo_svn/wc/trunk/subdev_2  ← 目录合并 source（const/fabao/guild/mail/tips）")
    print("    svn/demo_svn/wc/trunk/subdev_3  ← 目录合并 source（ability/reward/map/interaction/world_buff）")
    print("  排除大表：", ", ".join(sorted(BIG_TABLES)))
    if not ok:
        print("\n注意：部分校验 [WARN]，请人工核对。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

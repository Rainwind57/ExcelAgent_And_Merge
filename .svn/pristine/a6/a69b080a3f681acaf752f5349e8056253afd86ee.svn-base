"""两阶段 merge 实验材料生成脚本（通用注入器版）。

用法：
    cd c:\\Users\\wuzhixian\\Desktop\\project
    python -X utf8 merge\\scripts\\build_merge_materials.py
    python -X utf8 merge\\scripts\\build_merge_materials.py --from-svn-fixture   # 见下方"SVN fixture 覆盖模式"

作用：
    从 merge/samples/ 基准样本 + resources/ 真实游戏表中选取多个「表分组」，
    自动识别每个工作簿里的「整型主键数据 sheet」，跨多个 sheet 批量注入
    冲突/修改/新增/删除差异，生成两阶段 merge 所需的丰富测试材料。

产出结构：
    merge/trunk/{group}.xlsx              阶段2 目标基准（源表原样拷贝）
    merge/src/devbranch1/{group}_1..3.xlsx  生产者1 三次提交（含提交间冲突）
    merge/src/devbranch2/{group}_1..2.xlsx  生产者2 两次提交（干净单向变更）

通用注入器（对每个数据 sheet，引擎约定 row1=表头、row2+=数据、第一列=主键）：
    - conflict_pks：devbranch1 的 _2 与 _3 对同一批现有行同列改成不同值 → 提交间冲突
    - change_pks ：devbranch2 的 _2 单向修改另一批现有行 → changed（可自动采纳）
    - inserts    ：各分支各提交追加未占用主键的新行（复制真实行 + 改主键，公式随行号自适应）
    - deletes    ：devbranch1 的 _3 删除末尾若干现有行 → deleted（仅非公式 sheet）
    数值列改为「原值+偏移」（保持类型，公式输入列可触发重算）；文本列写标记串。

幂等：重复运行先清空 trunk / devbranch1 / devbranch2 内 xlsx 及 devbranch 缓冲区。

SVN fixture 覆盖模式（openspec change merge-svn-dual-mode 任务 2.4，`--from-svn-fixture`）：
    不传该参数时，行为与上文完全一致（默认模式 = 纯文件复制/注入，无 SVN 依赖，
    现有测试 server/tests/verify_three_stage.py 等不受影响）。

    传该参数且检测到 `merge/svn/fixture/repo` 已由
    `merge/svn/fixture/init_svn_fixture.py` 初始化时，在上述默认流程跑完之后，
    额外对 ability（分支场景）/ match_stat（子目录场景）两个分组做一次覆盖：
    用 `svn export` 从 fixture repo 的真实提交历史反查 copyfrom 版本号，取出
    对应内容覆盖 mergebase 快照与该生产者的 {group}_N.xlsx 提交样本，
    替代手工复制得到的版本。其余分组不受影响。若本机没有 svn 命令或 fixture
    未初始化，打印提示后跳过覆盖（不报错、不影响默认产物）。
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Optional, Tuple

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell
from openpyxl.comments import Comment

MERGE_ROOT = Path(__file__).resolve().parent.parent
RES = MERGE_ROOT.parent / "resources"
TRUNK_DIR = MERGE_ROOT / "trunk"
SRC_DIR = MERGE_ROOT / "src"
DB1_DIR = SRC_DIR / "devbranch1"
DB2_DIR = SRC_DIR / "devbranch2"
DEVBUF_DIR = SRC_DIR / "devbranch"
MERGEBASE_DIR = MERGE_ROOT / "mergebase"   # fork 快照（merge-base）存放处
SAMPLES_DIR = MERGE_ROOT / "samples"       # 基准样本（整理后从根目录迁入）
LEGACY_DIR = MERGE_ROOT / "legacy"         # 编号/漏行样本（整理后从根目录迁入）

# SVN fixture 覆盖模式相关路径（方式二：真实 SVN，见 merge/svn/fixture/init_svn_fixture.py）
# 与方式一 demo 快照（merge/demo/）物理隔离，见 server/config.py
SVN_FIXTURE_REPO = MERGE_ROOT / "svn" / "fixture" / "repo"
SVN_FIXTURE_WC = MERGE_ROOT / "svn" / "fixture" / "wc"

# 表分组 → 源工作簿。既含 merge/samples/ 基准样本，也含 resources 真实多-sheet 表。
GROUP_SOURCES = {
    "item":       SAMPLES_DIR / "item.xlsx",
    "ability":    SAMPLES_DIR / "ability.xlsx",
    "match_stat": SAMPLES_DIR / "match_stat.xlsx",
    "reward":     RES / "reward.xlsx",
    "hero_level": RES / "hero" / "hero_level.xlsx",
    "pet":        RES / "pet" / "pet.xlsx",
    "quest":      RES / "quest" / "quest.xlsx",
    "building":   RES / "city" / "building.xlsx",
    "fabao":      RES / "fabao.xlsx",
    "combat":     RES / "combat" / "combat.xlsx",
    "guild":      RES / "guild.xlsx",
}

MIN_PK_ROWS = 5      # sheet 至少这么多整型主键行才作为数据 sheet
MAX_SHEETS = 4       # 每个 group 最多注入的数据 sheet 数（控制规模）
TAG_DELTA = {"v2": 111, "v3": 222, "vb2": 333}


# ── IO / 目录 ─────────────────────────────────────────────────

def _clear(d: Path):
    d.mkdir(parents=True, exist_ok=True)
    for pat in ("*.xlsx", "*.bak_*"):
        for f in d.glob(pat):
            f.unlink(missing_ok=True)


def _ensure_dirs():
    for d in (TRUNK_DIR, DB1_DIR, DB2_DIR):
        _clear(d)
    MERGEBASE_DIR.mkdir(parents=True, exist_ok=True)
    for f in MERGEBASE_DIR.glob("*.xlsx"):
        f.unlink(missing_ok=True)
    DEVBUF_DIR.mkdir(parents=True, exist_ok=True)
    for f in DEVBUF_DIR.glob("*.xlsx"):
        f.unlink(missing_ok=True)
    mf = DEVBUF_DIR / "_stage1_manifest.json"
    if mf.is_file():
        mf.unlink()


def _copy(src: Path, dst: Path):
    shutil.copy2(src, dst)


# ── sheet 分析 ────────────────────────────────────────────────

def _int_pk_rows(ws):
    """返回 [(row, pk_int), ...]，仅第一列可解析为整数的数据行（row>=2）。"""
    out = []
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=1).value
        if v is None:
            continue
        try:
            out.append((r, int(str(v).strip())))
        except (ValueError, TypeError):
            continue
    return out


def _has_formula(ws) -> bool:
    for r in range(2, min(ws.max_row, 40) + 1):
        for c in range(1, ws.max_column + 1):
            v = ws.cell(row=r, column=c).value
            if isinstance(v, str) and v.startswith("="):
                return True
    return False


def _plan_group(src: Path):
    """从源表规划每个数据 sheet 的注入点。返回 {sheet: plan} 与 formula 标记。"""
    wb = load_workbook(src, data_only=False)
    plan = {}
    for sn in wb.sheetnames:
        ws = wb[sn]
        pks = _int_pk_rows(ws)
        if len(pks) < MIN_PK_ROWS:
            continue
        is_formula = _has_formula(ws)
        pk_vals = [pk for _, pk in pks]
        max_pk = max(pk_vals)
        mod_col = 2 if ws.max_column >= 2 else 1
        # 取互不重叠的主键子集
        conflict_pks = pk_vals[:3]
        change_pks = pk_vals[3:5] if len(pk_vals) >= 5 else pk_vals[:1]
        delete_pks = [] if is_formula else pk_vals[-2:]
        # 避免删除与改动集合重叠
        delete_pks = [p for p in delete_pks if p not in conflict_pks and p not in change_pks]
        plan[sn] = {
            "is_formula": is_formula,
            "mod_col": mod_col,
            "max_pk": max_pk,
            "conflict_pks": conflict_pks,
            "change_pks": change_pks,
            "delete_pks": delete_pks,
            "src_row": pks[0][0],   # 复制这一真实行作为新增行模板
        }
        if len(plan) >= MAX_SHEETS:
            break
    wb.close()
    return plan


# ── 单元格操作 ────────────────────────────────────────────────

def _safe_set(ws, r, c, value, comment=None):
    cell = ws.cell(row=r, column=c)
    if isinstance(cell, MergedCell):
        return False
    cell.value = value
    if comment:
        cell.comment = Comment(comment, "build-script")
    return True


def _find_row(ws, pk):
    for r in range(2, ws.max_row + 1):
        v = ws.cell(row=r, column=1).value
        if v is None:
            continue
        try:
            if int(str(v).strip()) == pk:
                return r
        except (ValueError, TypeError):
            continue
    return None


def _new_value(ws, r, col, tag):
    cur = ws.cell(row=r, column=col).value
    if isinstance(cur, bool):
        cur = None
    if isinstance(cur, (int, float)):
        return round(cur) + TAG_DELTA[tag]
    return f"{tag}#{ws.cell(row=r, column=1).value}"


def _modify(ws, pk, col, tag, comment):
    r = _find_row(ws, pk)
    if r is None:
        return False
    return _safe_set(ws, r, col, _new_value(ws, r, col, tag), comment)


def _last_data_row(ws):
    last = 1
    for r in range(2, ws.max_row + 1):
        if ws.cell(row=r, column=1).value is not None:
            last = r
    return last


def _insert(ws, new_pk, src_row, mod_col, tag, comment):
    """复制 src_row 一整行到末尾作为新行，改主键 + 标记列；公式随行号自适应。"""
    new_r = _last_data_row(ws) + 1
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=src_row, column=c).value
        if isinstance(v, str) and v.startswith("="):
            v = re.sub(rf"(?<!\d){src_row}(?!\d)", str(new_r), v)  # 行内引用改到新行
        if not _safe_set(ws, new_r, c, v):
            continue
    _safe_set(ws, new_r, 1, new_pk, comment)
    _safe_set(ws, new_r, mod_col, f"{tag}#{new_pk}")
    return new_r


def _delete(ws, pks):
    rows = sorted((r for r in (_find_row(ws, p) for p in pks) if r), reverse=True)
    for r in rows:
        ws.delete_rows(r)


# ── 构建 ──────────────────────────────────────────────────────

def _build_trunk(plans):
    for group in plans:
        _copy(GROUP_SOURCES[group], TRUNK_DIR / f"{group}.xlsx")
        print(f"[trunk] {group}.xlsx  (数据 sheet: {', '.join(plans[group])})")


def _build_mergebase(plans):
    """fork 快照（merge-base）：每个生产者拷 ca 时刻的 trunk 基准。

    模拟环境里 trunk 一次性生成不变，故 fork = trunk 原始基准。每个 branch 一份
    （mergebase/{branch}_{group}.xlsx），作阶段2 三方合并的公共祖先。
    """
    for group in plans:
        trunk = TRUNK_DIR / f"{group}.xlsx"
        for branch in ("devbranch1", "devbranch2"):
            _copy(trunk, MERGEBASE_DIR / f"{branch}_{group}.xlsx")
    print(f"[mergebase] {len(plans)} group × 2 branch fork 快照（阶段2 三方公共祖先）")


def _build_devbranch1(plans):
    for group, plan in plans.items():
        trunk = TRUNK_DIR / f"{group}.xlsx"
        d1 = DB1_DIR / f"{group}_1.xlsx"
        d2 = DB1_DIR / f"{group}_2.xlsx"
        d3 = DB1_DIR / f"{group}_3.xlsx"

        # _1：基准（trunk 原样）
        _copy(trunk, d1)

        # _2：改 conflict_pks→v2 + 新增
        _copy(d1, d2)
        wb = load_workbook(d2)
        for sn, p in plan.items():
            ws = wb[sn]
            for pk in p["conflict_pks"]:
                _modify(ws, pk, p["mod_col"], "v2", "devbranch1 提交2（与提交3 冲突）")
            _insert(ws, p["max_pk"] + 1, p["src_row"], p["mod_col"], "db1c2add", "devbranch1 提交2 新增")
            _insert(ws, p["max_pk"] + 2, p["src_row"], p["mod_col"], "db1c2add", "devbranch1 提交2 新增")
            if p["delete_pks"]:
                _delete(ws, p["delete_pks"])   # 两个衍生提交一致删除 → 判定 deleted
        wb.save(d2)

        # _3：改 conflict_pks→v3（与 _2 不同）+ 新增 + 删除
        _copy(d1, d3)
        wb = load_workbook(d3)
        for sn, p in plan.items():
            ws = wb[sn]
            for pk in p["conflict_pks"]:
                _modify(ws, pk, p["mod_col"], "v3", "devbranch1 提交3（与提交2 冲突）")
            _insert(ws, p["max_pk"] + 11, p["src_row"], p["mod_col"], "db1c3add", "devbranch1 提交3 新增")
            if p["delete_pks"]:
                _delete(ws, p["delete_pks"])
        wb.save(d3)

        c = sum(len(p["conflict_pks"]) for p in plan.values())
        print(f"[devbranch1] {group}_1/2/3.xlsx  (提交间冲突 {c} 处 / {len(plan)} sheet)")


def _build_devbranch2(plans):
    for group, plan in plans.items():
        trunk = TRUNK_DIR / f"{group}.xlsx"
        d1 = DB2_DIR / f"{group}_1.xlsx"
        d2 = DB2_DIR / f"{group}_2.xlsx"

        # _1：trunk + 新增
        _copy(trunk, d1)
        wb = load_workbook(d1)
        for sn, p in plan.items():
            ws = wb[sn]
            _insert(ws, p["max_pk"] + 21, p["src_row"], p["mod_col"], "db2c1add", "devbranch2 提交1 新增")
        wb.save(d1)

        # _2：改 change_pks（单向变更，无冲突）
        _copy(d1, d2)
        wb = load_workbook(d2)
        for sn, p in plan.items():
            ws = wb[sn]
            for pk in p["change_pks"]:
                _modify(ws, pk, p["mod_col"], "vb2", "devbranch2 提交2 单向变更")
        wb.save(d2)

        ch = sum(len(p["change_pks"]) for p in plan.values())
        print(f"[devbranch2] {group}_1/2.xlsx  (单向变更 {ch} 处 / {len(plan)} sheet)")


def _build_missing():
    for name in ("ability_missing.xlsx", "item_missing.xlsx"):
        src = LEGACY_DIR / name
        if src.exists():
            _copy(src, DB1_DIR / name)
            print(f"[devbranch1] {name}  (漏行样本 M3)")


# ── SVN fixture 覆盖模式（任务 2.4，--from-svn-fixture）───────────────
#
# 默认（不传该参数）走上面纯文件复制/注入的产出，不受本节代码影响。
# 传该参数且 merge/svn/fixture/repo 已初始化（见 merge/svn/fixture/init_svn_fixture.py）
# 时，用 svn log/svn cat 直接反查 fixture repo 的真实提交历史，取出对应版本内容，
# 覆盖 ability（对应场景A：branches/A 多次提交）与 match_stat（对应场景B：
# trunk/subdir_x_copied 多次提交）两个分组的 mergebase 快照 + devbranch{1,2} 提交样本，
# 替代手工复制得到的版本。其余分组维持默认产物不变。
#
# 与 server/routers/svn_history.py 一致：只用 subprocess 调 svn 官方 CLI，不引入
# python svn 绑定库。

def _svn_fixture_ready() -> bool:
    if shutil.which("svn") is None:
        print("  [svn-fixture] 未找到 svn 命令，跳过覆盖（默认产物保持不变）")
        return False
    if not (SVN_FIXTURE_REPO.is_dir() and any(SVN_FIXTURE_REPO.iterdir())):
        print(f"  [svn-fixture] {SVN_FIXTURE_REPO} 未初始化，跳过覆盖（默认产物保持不变）。"
              f"先运行 python -X utf8 merge\\svn\\fixture\\init_svn_fixture.py")
        return False
    return True


def _fixture_repo_url() -> str:
    return SVN_FIXTURE_REPO.resolve().as_uri()


def _svn_run_text(cmd: List[str]) -> str:
    """跑 svn 文本类子命令（log 等），返回 stdout；失败抛 RuntimeError（由调用方捕获降级）。"""
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                        errors="replace", timeout=60)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip())
    return r.stdout


def _svn_cat_to_file(url: str, rev: int, dest: Path) -> None:
    """`svn cat -r rev url` 二进制内容写到 dest（xlsx 是二进制，不能走 text 模式）。"""
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "wb") as wf:
        r = subprocess.run(["svn", "cat", "-r", str(rev), url],
                            stdout=wf, stderr=subprocess.PIPE, timeout=60)
    if r.returncode != 0:
        dest.unlink(missing_ok=True)
        raise RuntimeError(r.stderr.decode("utf-8", errors="replace").strip())


def _commit_revisions(url: str) -> List[int]:
    """该 URL 的全部提交版本号，按 rev 升序。"""
    out = _svn_run_text(["svn", "log", "--xml", url])
    root = ET.fromstring(out)
    revs = [int(e.get("revision")) for e in root.findall("logentry")]
    revs.sort()
    return revs


def _copyfrom_source(url: str) -> Optional[Tuple[str, int]]:
    """`svn log -v --xml --stop-on-copy url`：取该路径创建时刻记录的 (copyfrom-path, copyfrom-rev)。

    纯新建（无 copyfrom，如 subdir_x_new 场景）返回 None。
    """
    out = _svn_run_text(["svn", "log", "-v", "--xml", "--stop-on-copy", url])
    root = ET.fromstring(out)
    entries = root.findall("logentry")
    if not entries:
        return None
    earliest = entries[-1]   # svn log 默认新→旧排列，--stop-on-copy 后最后一条即创建提交
    for p in earliest.findall("paths/path"):
        cf_path, cf_rev = p.get("copyfrom-path"), p.get("copyfrom-rev")
        if cf_path and cf_rev:
            return cf_path, int(cf_rev)
    return None


def _overlay_ability_from_fixture(plans) -> bool:
    """场景A（跨分支）：branches/A 的真实提交历史覆盖 ability 的 mergebase + devbranch1_1..3。"""
    if "ability" not in plans:
        return False
    repo = _fixture_repo_url()
    branch_file_url = f"{repo}/branches/A/ability.xlsx"
    try:
        src = _copyfrom_source(f"{repo}/branches/A")
        if src is None:
            print("  [svn-fixture][ability] branches/A 无 copyfrom 记录，跳过覆盖")
            return False
        cf_path, cf_rev = src
        revs = _commit_revisions(branch_file_url)
        if len(revs) < 3:
            print(f"  [svn-fixture][ability] branches/A/ability.xlsx 提交数不足3次（{len(revs)}），跳过覆盖")
            return False
        trunk_file_url = f"{repo}{cf_path}/ability.xlsx"
        _svn_cat_to_file(trunk_file_url, cf_rev, MERGEBASE_DIR / "devbranch1_ability.xlsx")
        for i, rev in enumerate(revs[:3], start=1):
            _svn_cat_to_file(branch_file_url, rev, DB1_DIR / f"ability_{i}.xlsx")
        print(f"  [svn-fixture][ability] mergebase<-trunk@r{cf_rev}；"
              f"devbranch1_1..3 <- branches/A@r{revs[:3]}")
        return True
    except RuntimeError as e:
        print(f"  [svn-fixture][ability] svn 命令失败，跳过覆盖: {e}")
        return False


def _overlay_match_stat_from_fixture(plans) -> bool:
    """场景B（子目录，拷贝创建变体）：trunk/subdir_x_copied 的真实提交历史
    覆盖 match_stat 的 mergebase + devbranch2_1..2。"""
    if "match_stat" not in plans:
        return False
    repo = _fixture_repo_url()
    subdir_file_url = f"{repo}/trunk/subdir_x_copied/match_stat.xlsx"
    try:
        src = _copyfrom_source(subdir_file_url)
        if src is None:
            print("  [svn-fixture][match_stat] subdir_x_copied/match_stat.xlsx 无 copyfrom 记录，跳过覆盖")
            return False
        cf_path, cf_rev = src   # cf_path = /trunk/match_stat.xlsx
        revs = _commit_revisions(subdir_file_url)
        if len(revs) < 3:  # 创建提交 + 至少2次修改
            print(f"  [svn-fixture][match_stat] subdir_x_copied/match_stat.xlsx 提交数不足（{len(revs)}），跳过覆盖")
            return False
        trunk_file_url = f"{repo}{cf_path}"
        _svn_cat_to_file(trunk_file_url, cf_rev, MERGEBASE_DIR / "devbranch2_match_stat.xlsx")
        modify_revs = revs[-2:]   # 跳过创建提交本身，取后续两次真实修改提交
        for i, rev in enumerate(modify_revs, start=1):
            _svn_cat_to_file(subdir_file_url, rev, DB2_DIR / f"match_stat_{i}.xlsx")
        print(f"  [svn-fixture][match_stat] mergebase<-trunk@r{cf_rev}；"
              f"devbranch2_1..2 <- subdir_x_copied@r{modify_revs}")
        return True
    except RuntimeError as e:
        print(f"  [svn-fixture][match_stat] svn 命令失败，跳过覆盖: {e}")
        return False


def _overlay_from_svn_fixture(plans) -> None:
    print("=== SVN fixture 覆盖模式（--from-svn-fixture）===")
    if not _svn_fixture_ready():
        return
    did_ability = _overlay_ability_from_fixture(plans)
    did_match_stat = _overlay_match_stat_from_fixture(plans)
    if not (did_ability or did_match_stat):
        print("  [svn-fixture] 无分组被覆盖，默认产物维持不变")


def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--from-svn-fixture", action="store_true",
        help="在默认纯文件复制产出基础上，用 merge/svn/fixture 的真实 SVN 提交历史"
             "覆盖 ability/match_stat 两个分组的样本（fixture 未初始化则自动跳过，不报错）。",
    )
    args = parser.parse_args()

    missing_src = [g for g, s in GROUP_SOURCES.items() if not s.exists()]
    if missing_src:
        raise SystemExit(f"缺少源表: {missing_src}")

    print("=== 规划注入点 ===")
    plans = {}
    for group, src in GROUP_SOURCES.items():
        plans[group] = _plan_group(src)
        if not plans[group]:
            print(f"  [warn] {group} 无可用整型主键数据 sheet，跳过")
    plans = {g: p for g, p in plans.items() if p}

    print("=== 清空目标目录 ===")
    _ensure_dirs()
    print("=== 构建 trunk ===")
    _build_trunk(plans)
    print("=== 构建 mergebase（fork 快照）===")
    _build_mergebase(plans)
    print("=== 构建 devbranch1（三次提交）===")
    _build_devbranch1(plans)
    _build_missing()
    print("=== 构建 devbranch2（两次提交）===")
    _build_devbranch2(plans)

    if args.from_svn_fixture:
        _overlay_from_svn_fixture(plans)

    total_conf = sum(len(p["conflict_pks"]) for pl in plans.values() for p in pl.values())
    print(f"\n=== 完成：{len(plans)} 个表分组，累计提交间冲突约 {total_conf} 处 ===")
    print("分组：" + ", ".join(plans))


if __name__ == "__main__":
    main()

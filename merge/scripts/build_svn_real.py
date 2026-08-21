"""真实 SVN 模式样例数据搭建脚本（方式二：merge/svn/demo_svn/）。

把桌面 Merge测试集 的真实大表数据导入到 merge/svn/demo_svn/ 下的 svnadmin 仓库，
并构造 trunk / dev1 / dev2 三个工作副本，使合并引导（merge_branch 路由）能在
真实 SVN copyfrom 历史上做三方合并。目录合并（merge_subdir 路由）的私有子目录
直接随 trunk 导入（trunk/subdev_1 来自 _seed_data/trunk/subdev_1），不再单列分支。

  仓库布局（merge/svn/demo_svn/repo，file:// 直连，无需 svnserve）：
    /trunk                 桌面 Merge测试集/trunk 全量（26 表 + 12 子目录，含 monster 10w 行）
    /branches/dev1         svn copy /trunk@R_trunk，覆写 8 张改表（monster/skill_level/item_drop...）
    /branches/dev2         svn copy /trunk@R_trunk，覆写 8 张改表（与 dev1 真实差异）
    （subdev_1 不再作分支，trunk/subdev_1 子目录随 trunk 导入，供目录合并 source）

  工作副本（merge/svn/demo_svn/wc/，merge_branch.list_dirs 第 3 步扫描此处）：
    wc/trunk / wc/branches/dev1 / wc/branches/dev2

  合并基线反查（svn_history._resolve_branch_point）：
    dev1/dev2  → svn log --stop-on-copy → copyfrom=/trunk, rev=R_trunk → base=trunk@R_trunk
    trunk/subdev_1 → 直接取 trunk 快照作目录合并 base（merge_subdir 反查子目录 copyfrom）

中文路径注意：源数据在桌面 `Merge测试集`（含中文），svn/svnlook CLI 对中文路径编码
错乱会失败。故本脚本用 Python shutil 从中文源路径读取文件，写入全 ASCII 的
merge/svn/demo_svn/ 工作副本；所有 svn CLI 操作只触及 ASCII 路径与 file:// URL。

用法：
    python merge/scripts/build_svn_real.py            # 默认从项目内置 merge/_seed_data/ 读取（deploy 自包含）
    python merge/scripts/build_svn_real.py --src DIR  # 指定其它源目录（如桌面 Merge测试集）
    python merge/scripts/build_svn_real.py --clean    # 先删除已存在的 repo/wc 再重建
"""
import argparse
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

# Windows 控制台默认 GBK，含 ²/→ 等字符的中文提示会 UnicodeEncodeError。
# 统一切到 utf-8 输出，与调用方 [Console]::OutputEncoding=UTF8 对齐。
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

# 默认数据源：项目内置种子数据（merge/_seed_data/），其他用户 clone 后无需外部数据即可 deploy。
# 可用 --src 指向自定义源（如桌面 Merge测试集）覆盖。
DEFAULT_SRC = MERGE_DIR / "_seed_data"

# trunk/subdev_1 子目录随 trunk 导入即可（见 stage2 _copy_tree_contents），无需单列分支。


def _run(cmd, check=True, capture=True, timeout=600):
    """执行命令，返回 CompletedProcess。统一 utf-8 解码、超时保护。"""
    if capture:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    else:
        r = subprocess.run(cmd, timeout=timeout)
    if check and r.returncode != 0:
        err = (r.stderr or "").strip() if capture else ""
        out = (r.stdout or "").strip() if capture else ""
        raise RuntimeError(f"命令失败 [{ ' '.join(cmd) }]\n  stdout: {out[:800]}\n  stderr: {err[:800]}")
    return r


def _svn(*args, check=True, timeout=600):
    return _run(["svn", *args], check=check, timeout=timeout)


def _svnadmin(*args, check=True, timeout=600):
    return _run(["svnadmin", *args], check=check, timeout=timeout)


def _repo_url() -> str:
    """file:// URL（repo 路径全 ASCII，避免中文编码问题）。"""
    # 路径用正斜杠，svn file:// 接受
    p = str(REPO_DIR).replace("\\", "/")
    return f"file:///{p}"


def _force_remove(path: Path):
    """删除目录树。SVN repo/工作副本含只读文件与 .svn/wc.db（可能被 TSVNCache 锁），
    Python shutil.rmtree 对此类树不可靠，优先用系统 rm/rd，回退到 rmtree+onerror。"""
    if not path.exists():
        return
    if os.name == "nt":
        rc = subprocess.run(["cmd", "/c", "rd", "/s", "/q", str(path)],
                            capture_output=True).returncode
    else:
        rc = subprocess.run(["rm", "-rf", str(path)],
                            capture_output=True).returncode
    if rc != 0 or path.exists():
        def _onerror(func, p, exc):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except Exception:
                pass
        shutil.rmtree(path, onerror=_onerror)


def _clean():
    if SVN_DEMO_DIR.exists():
        print(f"[clean] 删除 {SVN_DEMO_DIR}")
        _force_remove(SVN_DEMO_DIR)
    SVN_DEMO_DIR.mkdir(parents=True, exist_ok=True)


def _copy_tree_contents(src: Path, dst: Path):
    """把 src 下的全部内容（文件+子目录）拷进 dst（dst 已存在）。

    用 Python 处理中文源路径；跳过 ~$ 临时锁文件与 .svn。
    """
    for item in src.iterdir():
        if item.name == ".svn":
            continue
        target = dst / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True)
        else:
            if item.name.startswith("~$"):
                continue
            shutil.copy2(item, target)


def _svn_add_all(path: Path):
    """递归把工作副本下未版本化文件加入版本控制（xlsx 含 NUL 自动识别为 binary）。"""
    _svn("add", "--force", "--depth", "infinity", str(path))


def stage1_create_repo():
    print(f"=== [1/8] svnadmin create: {REPO_DIR} ===")
    if REPO_DIR.exists():
        _force_remove(REPO_DIR)
    REPO_DIR.mkdir(parents=True, exist_ok=True)
    _svnadmin("create", str(REPO_DIR))
    url = _repo_url()
    # 建主干/分支顶层（一次提交）
    _svn("mkdir", f"{url}/trunk", f"{url}/branches",
         "-m", "init: trunk + branches 布局")
    rev = _head_rev()
    print(f"  repo 就绪，r{rev}")
    return url


def stage2_import_trunk(url: str, src: Path) -> int:
    print(f"=== [2/8] 导入 trunk 真实数据（{src}/trunk → /trunk）===")
    trunk_wc = WC_DIR / "trunk"
    trunk_wc.parent.mkdir(parents=True, exist_ok=True)
    _svn("checkout", f"{url}/trunk", str(trunk_wc))
    _copy_tree_contents(src / "trunk", trunk_wc)
    _svn_add_all(trunk_wc)
    _svn("commit", str(trunk_wc), "-m", "trunk: 导入 Merge测试集 全量大表（monster/skill_level/item_drop 等 10w 行级）")
    rev = _head_rev()
    print(f"  trunk 导入完成，r{rev}")
    return rev


def stage3_branch_off(url: str, trunk_rev: int):
    print(f"=== [3/8] 从 /trunk@r{trunk_rev} 切出 dev1 / dev2 ===")
    _svn("copy", f"{url}/trunk@{trunk_rev}", f"{url}/branches/dev1",
         "-m", f"dev1: 从 trunk@r{trunk_rev} 切出（svn copy，真实 copyfrom）")
    _svn("copy", f"{url}/trunk@{trunk_rev}", f"{url}/branches/dev2",
         "-m", f"dev2: 从 trunk@r{trunk_rev} 切出（svn copy，真实 copyfrom）")
    print(f"  两分支已切出")


def stage4_checkout_branches(url: str):
    print(f"=== [4/8] checkout 工作副本 ===")
    for name in ("dev1", "dev2"):
        wc = WC_DIR / "branches" / name
        wc.mkdir(parents=True, exist_ok=True)
        _svn("checkout", f"{url}/branches/{name}", str(wc))
        print(f"  wc/branches/{name}")


def stage5_dev_branch(url: str, branch: str, src: Path, src_dir: str):
    print(f"=== [5/8] 覆写 {branch} 改表（{src}/{src_dir}）===")
    wc = WC_DIR / "branches" / branch
    sdir = src / src_dir
    n = 0
    for fp in sorted(sdir.glob("*.xlsx")):
        if fp.name.startswith("~$"):
            continue
        shutil.copy2(fp, wc / fp.name)  # 该文件继承自 trunk，已版本化 → 内容变更即 Modified
        n += 1
    _svn("commit", str(wc), "-m", f"{branch}: 应用 {n} 张分支改表（{', '.join(p.stem for p in sorted(sdir.glob('*.xlsx')))[:120]}）")
    print(f"  {branch} 提交 {n} 张改表")


def stage6_cleanup_legacy_subdev1(url: str):
    """清理旧脚本遗留的 branches/subdev_1 分支。

    subdev_1 不再作为分支：目录合并 source 用 trunk/subdev_1 子目录（随 trunk 导入）。
    若旧 repo 残留 branches/subdev_1 分支与工作副本，删除之，避免它出现在
    /api/merge/branch/dirs 选择列表里（用户要求 branches 只含 dev*）。
    幂等：不存在则跳过。
    """
    print(f"=== [6/8] 清理遗留 branches/subdev_1（已改作 trunk 子目录）===")
    r = _svn("info", f"{url}/branches/subdev_1", check=False)
    if r.returncode == 0:
        _svn("rm", f"{url}/branches/subdev_1", "-m",
             "clean: 删除遗留 branches/subdev_1（subdev_1 改作 trunk 子目录，不再作分支）")
        print("  删除 /branches/subdev_1")
    else:
        print("  /branches/subdev_1 不存在，跳过")
    wc = WC_DIR / "branches" / "subdev_1"
    if (wc / ".svn").is_dir() or wc.exists():
        _force_remove(wc)
        print(f"  删除 wc/branches/subdev_1")


def _head_rev() -> int:
    r = _svn("info", "--show-item", "revision", _repo_url())
    return int(r.stdout.strip())


def stage7_verify(url: str, trunk_rev: int):
    print(f"=== [7/8] 验证 copyfrom 反查 ===")
    ok_all = True
    for name, expect_cf in [("dev1", "/trunk"), ("dev2", "/trunk")]:
        wc = WC_DIR / "branches" / name
        r = _svn("log", "-v", "--stop-on-copy", "--xml", str(wc), check=False)
        out = r.stdout or ""
        # 粗解析 copyfrom-path / copyfrom-rev
        cf_path = None
        cf_rev = None
        import re
        m = re.search(r'copyfrom-path="([^"]+)"', out)
        if m:
            cf_path = m.group(1)
        m2 = re.search(r'copyfrom-rev="([^"]+)"', out)
        if m2:
            cf_rev = m2.group(1)
        status = "OK" if (cf_path and cf_path.endswith(expect_cf) and cf_rev == str(trunk_rev)) else "WARN"
        if status != "OK":
            ok_all = False
        print(f"  {name}: copyfrom_path={cf_path!r} copyfrom_rev={cf_rev!r} (期望 /trunk@r{trunk_rev}) [{status}]")
    return ok_all


def stage8_summary(url: str, trunk_rev: int):
    print(f"=== [8/8] 完成 ===")
    print(f"  repo  : {url}  (HEAD r{_head_rev()})")
    print(f"  trunk : r{trunk_rev} 全量真实大表")
    print("  分支  (合并引导 /api/merge/branch/dirs 可见):")
    print("    svn/demo_svn/wc/trunk")
    print("    svn/demo_svn/wc/branches/dev1   ← 跨分支合并 source/target")
    print("    svn/demo_svn/wc/branches/dev2   ← 跨分支合并 source/target")
    print("  目录合并 source（/api/merge/subdir/dirs 递归扫描 trunk 子目录）:")
    print("    svn/demo_svn/wc/trunk/subdev_1  ← 随 trunk 导入，不再单列分支")
    print("\n  提示：monster.xlsx(10w行)/skill_level/item_drop 体积大，compare 阶段")
    print("  若 O(n^2) 卡顿属已知问题 M7（见 合并引导问题排查报告.md），与数据搭建无关。")


def main():
    ap = argparse.ArgumentParser(description="搭建真实 SVN 模式样例（merge/svn/demo_svn/）")
    ap.add_argument("--src", default=str(DEFAULT_SRC),
                    help=f"Merge测试集 源目录（默认 {DEFAULT_SRC}）")
    ap.add_argument("--clean", action="store_true", help="先删除已存在的 repo/wc 再重建")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.is_dir():
        print(f"错误：源目录不存在: {src}", file=sys.stderr)
        sys.exit(2)
    for sub in ("trunk", "dev1", "dev2"):
        if not (src / sub).is_dir():
            print(f"错误：源目录缺少 {sub}/: {src}/{sub}", file=sys.stderr)
            sys.exit(2)
    if not (src / "trunk" / "subdev_1").is_dir():
        print(f"错误：源目录缺少 trunk/subdev_1/: {src}/trunk/subdev_1", file=sys.stderr)
        sys.exit(2)

    if args.clean or not REPO_DIR.exists():
        _clean()
    SVN_DEMO_DIR.mkdir(parents=True, exist_ok=True)
    WC_DIR.mkdir(parents=True, exist_ok=True)

    url = stage1_create_repo()
    trunk_rev = stage2_import_trunk(url, src)
    stage3_branch_off(url, trunk_rev)
    stage4_checkout_branches(url)
    stage5_dev_branch(url, "dev1", src, "dev1")
    stage5_dev_branch(url, "dev2", src, "dev2")
    stage6_cleanup_legacy_subdev1(url)
    ok = stage7_verify(url, trunk_rev)
    stage8_summary(url, trunk_rev)
    if not ok:
        print("\n注意：部分 copyfrom 校验未完全匹配，请人工核对上方 [WARN]。", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

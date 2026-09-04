"""服务器配置。"""
import os
from pathlib import Path

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Excel 资源目录
RESOURCES_DIR = PROJECT_ROOT / "resources"

# 前端静态文件目录
STATIC_DIR = Path(__file__).resolve().parent / "static"

# Merge 工作目录：输入文件与导出文件均在此目录下
MERGE_DIR = PROJECT_ROOT / "merge"

# 散放样本归类子目录（整理后顶层不再堆放散文件）：
#   samples/ = 基准样本（无 _数字 后缀，build 脚本输入 + folder 模式基准）
#   legacy/  = 编号/漏行样本（{group}_N.xlsx 等，folder 模式多版本比对 + 导出落点）
MERGE_SAMPLES_DIR = MERGE_DIR / "samples"
MERGE_LEGACY_DIR = MERGE_DIR / "legacy"

# ── 两种 SVN 合并数据源（物理隔离，互不交错，属两种不同方式）──
# 方式一：demo 快照（文件夹模拟 SVN 版本历史，_meta.json 记 copyfrom，无需 svn CLI）。
#   merge/demo/ 下 trunk_r1/A_r2 等快照目录，由 merge/scripts/build_svn_demo.py 生成。
DEMO_SNAP_DIR = MERGE_DIR / "demo"
# 方式二：真实 SVN 仓库与工作副本（需 svn CLI，svnadmin create + svn checkout 初始化）。
#   merge/svn/fixture/  = 测试 fixture 仓库（tests/verify_merge_*、build_merge_materials --from-svn-fixture）
#   merge/svn/demo_svn/ = 分支演示工作副本（merge_branch 扫描 trunk/branches，版本号为真实 SVN revision）
SVN_AREA_DIR = MERGE_DIR / "svn"
SVN_FIXTURE_DIR = SVN_AREA_DIR / "fixture"
SVN_DEMO_WC_DIR = SVN_AREA_DIR / "demo_svn"

# 快照存储目录
SNAPSHOT_DIR = PROJECT_ROOT / ".snapshots"

# 下载/导出文件目录：所有生成文件（导出 zip、合并临时 xlsx）统一落在此处，
# 避免散落到系统临时目录 C:\Users\<u>\AppData\Local\Temp\
DOWNLOADS_DIR = PROJECT_ROOT / "downloads"
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# R13: merge 比对结果历史记录目录 + 保留时长（1 天 = 86400 秒）
MERGE_HISTORY_DIR = MERGE_DIR / "history"
os.makedirs(MERGE_HISTORY_DIR, exist_ok=True)
MERGE_HISTORY_TTL_SECONDS = 24 * 3600

# R5: skill 四层目录（L0_methodology / L1_derived / L2_runtime / L3_anti_patterns / _pending）
SKILLS_DIR = PROJECT_ROOT / "server" / "agent" / "excel" / "skills"

# 服务器配置
HOST = "127.0.0.1"
PORT = 8000

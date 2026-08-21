#!/usr/bin/env python3
"""安装配表操作模式到 .codemaker/table-mode/。

把 tools/table-mode/ 下的指令文档 + 知识库，连同 server/agent/excel/skills/
的 skill 原文，复制到 .codemaker/table-mode/（.codemaker 不提交，各自生成）。

运行：python tools/table-mode/install.py
"""
import shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent           # tools/table-mode/
PROJECT_ROOT = HERE.parent.parent                 # 项目根
TARGET = PROJECT_ROOT / ".codemaker" / "table-mode"
SKILLS_SRC = PROJECT_ROOT / "server" / "agent" / "excel" / "skills"

SOURCE_DOCS = ["配表操作模式.md", "知识库.md"]


def ignore_fn(directory, names):
    """复制 skills/ 时跳过 _pending/.snapshots 目录 + jsonl 候选文件。"""
    skipped = []
    for n in names:
        if n in ("_pending", ".snapshots"):
            skipped.append(n)
            continue
        p = Path(directory) / n
        if p.is_file() and p.suffix == ".jsonl":
            skipped.append(n)
    return skipped


def main():
    if not SKILLS_SRC.exists():
        print(f"[错误] skills 源目录不存在: {SKILLS_SRC}")
        raise SystemExit(1)

    TARGET.mkdir(parents=True, exist_ok=True)
    skills_target = TARGET / "skills"

    # 1. 复制指令文档 + 知识库
    for name in SOURCE_DOCS:
        src = HERE / name
        if not src.exists():
            print(f"[错误] 缺少源文档: {src}")
            raise SystemExit(1)
        shutil.copy2(src, TARGET / name)
        print(f"[复制] {name}")

    # 2. R25: 先预生成 L1_derived skills（列别名/约束/枚举等从真实表结构派生）。
    #    必须在 copytree 之前执行 —— 否则拷进 .codemaker 的是生成前的旧版，
    #    配表模式读到的 L1_derived 永远是上一轮的历史版本。
    try:
        import sys
        sys.path.insert(0, str(PROJECT_ROOT / "server"))
        from agent.excel.schema_infer import regenerate_skills
        regenerate_skills(PROJECT_ROOT / "resources")
        print("[R25] L1_derived skills 已预生成")
    except Exception as e:
        print(f"[R25] 预生成 L1_derived 失败（致命，终止安装）：{e}")
        raise SystemExit(1)

    # 2.1 校验 L1_derived 已生成到 skills 源目录
    l1_dir = SKILLS_SRC / "L1_derived"
    l1_files = sorted(p.name for p in l1_dir.glob("*.yaml")) if l1_dir.exists() else []
    if not l1_files:
        print(f"[错误] L1_derived 未生成到 {l1_dir}，skill 复制不完整")
        raise SystemExit(1)
    print(f"[校验] L1_derived 共 {len(l1_files)} 个 yaml: {', '.join(l1_files)}")

    # 3. 复制 skills/ 原文（跳过 _pending/jsonl）到 .codemaker（此时 L1_derived 已是最新）
    if skills_target.exists():
        shutil.rmtree(skills_target)
    shutil.copytree(SKILLS_SRC, skills_target, ignore=ignore_fn)
    skill_files = sorted(str(p.relative_to(skills_target)) for p in skills_target.rglob("*") if p.is_file())
    print(f"[复制] skills/（{len(skill_files)} 文件）")
    for rel in skill_files:
        print(f"    - {rel}")

    print()
    print("=" * 52)
    print("✅ 配表操作模式安装完成")
    print("=" * 52)
    print(f"安装位置: {TARGET}")
    print()
    print("使用方式:")
    print("  在 CodeMaker 对话框输入: 进入配表模式")
    print("  CodeMaker 询问确认 → 确认后进入模式")
    print("  退出输入: 退出配表模式")
    print()
    print("模式内能力:")
    print("  ✅ 查询/搜索表格    ✅ 改单元格值（单格/批量）")
    print("  ✅ 新增行（二段提交）✅ 删除行/插入行")
    print("  ✅ 列增删（需确认）  ✅ 跨表 ID 校验")
    print("  ✅ 定位失败相近项建议")
    print()
    print("前提: 后端服务运行中（http://127.0.0.1:8000）")


if __name__ == "__main__":
    main()

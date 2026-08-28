"""R5: codemaker 插件 skill 挂载路由。

三端点供 codemaker 插件侧拉取 skill 包：
  - GET /api/skills/manifest  列出各 yaml/md/json 用途/层级/更新时间/大小
  - GET /api/skills/export    打包整个 skills/ 为 zip 下载
  - GET /api/skills/diff      返回 since 之后修改的文件清单（增量 delta）
"""
import io
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import StreamingResponse

from config import SKILLS_DIR

router = APIRouter(prefix="/api/skills", tags=["skills"])

# 文件用途描述（相对路径 → (层级, 用途)）。未列出的文件按路径推断层级、用途"未分类"。
_SKILL_FILE_INFO = {
    # L0 方法论（纯人工，极少改）
    "parser_config.yaml": ("L0", "解析器前导动词 + 非业务 sheet 标记"),
    "sheet_aliases.yaml": ("L0", "自然语言别名 → 真实 sheet 名"),
    "column_short_form.yaml": ("L0", "列名短形式别名（名→名称、级→等级）"),
    "index_builder_hints.yaml": ("L0", "表头/文件名 → 业务域关键词推断"),
    "docs/formula_agent.md": ("docs", "公式处理 Agent Skill 文档"),
    "docs/cross_table_chain_principles.md": ("docs", "跨表链原则"),
    "docs/case_quest_npc_optimization.md": ("docs", "NPC 优化案例复盘"),
    "scripts/derive_required_fields.py": ("scripts", "从 _table_index.json 派生必填列"),
    # L1_derived 表结构派生（表变更自动重算）
    "L1_derived/cascade_rules.yaml": ("L1", "级联删除/修改规则"),
    "L1_derived/column_aliases.yaml": ("L1", "列名别名映射（含中英对照）"),
    "L1_derived/enum_mappings.yaml": ("L1", "枚举值映射"),
    "L1_derived/merge_strategies.yaml": ("L1", "合并策略（默认 manual）"),
    "L1_derived/row_aliases.yaml": ("L1", "行定位规则（locator_column + match）"),
    "L1_derived/table_context.yaml": ("L1", "表/sheet 上下文关键词"),
    "L1_derived/value_constraints.yaml": ("L1", "值约束（类型/唯一/外键）"),
    # L2_runtime 使用经验（运行结果驱动）
    "L2_runtime/table_relations.runtime.json": ("L2", "跨表热路径权重"),
    "L2_runtime/column_aliases.runtime.yaml": ("L2", "运行时列别名（promote 生成）"),
    # L3_anti_patterns 反模式
    "L3_anti_patterns/anti_patterns.yaml": ("L3", "反模式/雷区黑名单"),
}

# 跳过导出的文件/目录（候选池 jsonl + 隔离区 + 快照 + README，插件侧无需）
_SKIP_REL_PREFIXES = (
    "_pending/column_alias_candidates.jsonl",
    "_pending/anti_pattern_signals.jsonl",
    "_pending/quarantine",
    ".snapshots",
)


def _layer_of(rel: str) -> str:
    """从相对路径推断层级。根目录文件 = L0，子目录首段映射层级。"""
    parts = rel.replace("\\", "/").split("/")
    if len(parts) == 1:
        return "L0"
    head = parts[0]
    return {
        "L1_derived": "L1",
        "L2_runtime": "L2",
        "L3_anti_patterns": "L3",
        "_pending": "pending",
    }.get(head, head)


def _iter_skill_files() -> list[Path]:
    """枚举 skills/ 下所有文件（跳过 _pending jsonl/quarantine/.snapshots），按相对路径排序。"""
    if not SKILLS_DIR.exists():
        return []
    out = []
    for p in SKILLS_DIR.rglob("*"):
        if not p.is_file():
            continue
        rel = p.relative_to(SKILLS_DIR).as_posix()
        if any(rel.startswith(pre) for pre in _SKIP_REL_PREFIXES):
            continue
        out.append(p)
    out.sort(key=lambda x: x.relative_to(SKILLS_DIR).as_posix())
    return out


def _file_entry(p: Path) -> dict:
    rel = p.relative_to(SKILLS_DIR).as_posix()
    layer, purpose = _SKILL_FILE_INFO.get(rel, (_layer_of(rel), "未分类"))
    st = p.stat()
    return {
        "path": rel,
        "layer": layer,
        "purpose": purpose,
        "size": st.st_size,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
    }


@router.get("/manifest")
async def skills_manifest():
    """列出 skills/ 下所有文件的层级/用途/更新时间/大小。供插件侧判断是否需拉取增量。"""
    files = [_file_entry(p) for p in _iter_skill_files()]
    return {
        "skills_dir": str(SKILLS_DIR),
        "total_files": len(files),
        "files": files,
    }


@router.get("/export")
async def skills_export():
    """打包整个 skills/ 为 zip 流式下载（跳过候选池 jsonl/隔离区/快照）。"""
    if not SKILLS_DIR.exists():
        raise HTTPException(status_code=404, detail=f"skills 目录不存在: {SKILLS_DIR}")

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in _iter_skill_files():
            arcname = p.relative_to(SKILLS_DIR).as_posix()
            zf.write(p, arcname)
    buf.seek(0)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="skills_bundle_{stamp}.zip"'},
    )


@router.get("/diff")
async def skills_diff(since: str = Query(..., description="起始日期 YYYY-MM-DD 或 ISO 8601 时间戳")):
    """返回 since 之后修改的文件清单（增量 delta）。插件侧按此决定是否重新拉取。"""
    # 解析 since：先试 ISO 8601，失败补成当天 00:00:00 UTC
    try:
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)
    except ValueError:
        try:
            since_dt = datetime.strptime(since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="since 格式应为 YYYY-MM-DD 或 ISO 8601")

    changed = []
    for p in _iter_skill_files():
        entry = _file_entry(p)
        mtime = datetime.fromisoformat(entry["mtime"])
        if mtime > since_dt:
            changed.append(entry)
    return {
        "since": since_dt.isoformat(),
        "changed_files": changed,
        "total": len(changed),
    }

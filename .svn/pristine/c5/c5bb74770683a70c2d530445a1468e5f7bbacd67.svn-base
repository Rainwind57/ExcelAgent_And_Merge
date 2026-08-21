"""合并提交历史：读取 trunk 审计日志，归一化为 commit 记录列表。

分支合并/目录合并的 apply 都会写 `_merge_audit.json`（最新在前），
本接口把两类条目统一为 {time, mode, source, target, group, output, commit_name} 供前端展示。
"""
from fastapi import APIRouter

from routers.merge_stages import _read_audit

router = APIRouter(prefix="/api/merge", tags=["merge-commits"])

_MODE_LABEL = {
    "branch_absorb": "分支合并 absorb",
    "branch_absorb_overwrite": "分支合并 absorb·覆盖",
    "branch_merge_back": "分支合回 trunk",
    "branch_merge_back_overwrite": "分支合回 trunk·覆盖",
    "subdir_overwrite": "目录合并·覆盖",
    "subdir_new_version": "目录合并·新版本",
}


@router.get("/commits")
def list_commits():
    """返回合并提交历史（最新在前）：时间、模式、涉及目录、表、产出、commit 名称。

    一次合并（多表）一条记录：新格式条目带 tables 明细数组，group/output 为汇总字符串；
    兼容旧的单表条目（无 tables，直接取 group/output）。
    """
    commits = []
    for e in _read_audit():
        mode = e.get("mode", "")
        tables = e.get("tables") or []
        if tables:
            group = ", ".join(t.get("group", "") for t in tables)
            output = ", ".join(t.get("output", "") for t in tables)
        else:
            group = e.get("group", "")
            output = e.get("output", "")
        commits.append({
            "time": e.get("time", ""),
            "mode": mode,
            "mode_label": _MODE_LABEL.get(mode, mode),
            "source": e.get("source_dir") or e.get("source_branch", ""),
            "target": e.get("target_dir") or e.get("target_branch", ""),
            "version_dir": e.get("version_dir", ""),
            "group": group,
            "output": output,
            "commit_name": e.get("commit_name", ""),
        })
    return {"commits": commits}

"""校验路由：ID 编号段校验 + 跨表查重 + _capped 格式校验。"""
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Query
from pydantic import BaseModel

from config import RESOURCES_DIR
from engine.id_scope import get_id_scope_validator
from engine.patch_validator import validate_capped_workbook

router = APIRouter(prefix="/api/validate", tags=["validate"])


@router.get("/id-scope")
async def validate_id_scope(mode: str = "single"):
    """全目录 ID 校验：扫所有表 ID 列做跨表查重 + id_mgr 段校验。

    mode=single（默认）：单根 RESOURCES_DIR 扫描，返 CrossTableReport。
    mode=multibranch：扫 RESOURCES_DIR + 子目录各分支根，返 CrossBranchReport
      （location 加 branch 维度，冲突判定从"文件"改"分支"，方法 F）。

    返回：
      single: cross_table_conflicts/segment_violations/scanned_tables/id_mgr_loaded
      multibranch: cross_branch_conflicts/reserved_segments/scanned_tables/branches_scanned
    """
    v = get_id_scope_validator()
    if mode == "multibranch":
        roots = [RESOURCES_DIR]
        for sub in RESOURCES_DIR.iterdir():
            if sub.is_dir() and not sub.name.startswith(".") and sub.name != "__pycache__":
                roots.append(sub)
        report = v.validate_multi_branch(roots)
        return report.to_dict()
    report = v.validate_all(RESOURCES_DIR)
    return report.to_dict()


@router.get("/id-claim")
async def validate_id_claim(id: int = Query(..., description="待查询的 ID 值"),
                             mode: str = "single"):
    """id-claim 查询（方法 F4）：value 是否冲突 + 建议下一空闲号。

    agent 写 ID 列前调此，命中冲突 → pre_commit_hold(kind=id_conflict) + 建议换号，
    不静默改编号。mode=multibranch 扫子目录各分支根。
    """
    v = get_id_scope_validator()
    branches = None
    if mode == "multibranch":
        branches = [RESOURCES_DIR]
        for sub in RESOURCES_DIR.iterdir():
            if sub.is_dir() and not sub.name.startswith(".") and sub.name != "__pycache__":
                branches.append(sub)
    result = v.claim_id(id, RESOURCES_DIR, branches=branches)
    return result


@router.get("/structural/sync-preview")
async def structural_sync_preview(table: str = Query(..., description="表 stem（如 pet）"),
                                  sheet: str = Query(..., description="sheet 名")):
    """方法 E3：trunk 加列后预检 _capped.xlsx / ca/dev/* 同名表缺此列。

    扫 RESOURCES_DIR + 子目录各分支根建列血缘图，返回 table/sheet 在 trunk 有
    但其他分支缺的列清单。前端据此渲染拦截卡（kind=structure_sync_missing 留前端接）。
    """
    from engine.column_lineage import compute_column_lineage
    roots = [RESOURCES_DIR]
    for sub in RESOURCES_DIR.iterdir():
        if sub.is_dir() and not sub.name.startswith(".") and sub.name != "__pycache__":
            roots.append(sub)
    graph = compute_column_lineage(roots)
    return graph.sync_preview(table, sheet)


class CappedValidateRequest(BaseModel):
    """_capped.xlsx 格式校验请求（方法 C）。"""
    path: str                    # _capped.xlsx 路径（相对 RESOURCES_DIR 或绝对）
    trunk_sheets: List[str] = [] # trunk 主表 sheet 名集合（规则③校验，空则跳过）


@router.post("/capped")
async def validate_capped(req: CappedValidateRequest):
    """_capped.xlsx 格式 5 坑校验（ca-overview §3.3）。

    返回 violations 清单（全 hold 级），ok=True 表示合规。
    """
    p = Path(req.path)
    if not p.is_absolute():
        p = RESOURCES_DIR / p
    if not p.exists():
        return {"ok": False, "error": f"文件不存在：{req.path}", "violations": []}
    violations = validate_capped_workbook(p, req.trunk_sheets)
    return {"ok": len(violations) == 0, "violations": [v.to_dict() for v in violations]}

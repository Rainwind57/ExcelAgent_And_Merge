"""表格浏览路由：浏览、搜索 Excel 配表数据。"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from models.agent_models import (
    TableInfo, SheetDataPage, SearchResponse,
    AddFormBuildRequest, AddFormResponse,
    FormValidateRequest, AddFormValidateResponse,
    FormCommitRequest, AddFormCommitResponse,
    CellUpdateRequest, CellUpdateResponse,
    FormColumn,
    SuggestResponse,
    BatchCellUpdateRequest, BatchCellUpdateResponse,
    RowDeleteRequest, RowInsertRequest, ColumnOpRequest, RowOpResponse,
)
from services.agent_service import get_agent_service

router = APIRouter(prefix="/api/tables", tags=["tables"])


@router.get("", response_model=list[TableInfo])
async def list_tables():
    """列出所有表格及 Sheet 摘要。"""
    service = get_agent_service()
    return service.get_tables()


@router.get("/search", response_model=SearchResponse)
async def search_tables(
    q: str = Query(..., min_length=1, description="搜索关键词"),
    table: str = Query(default="", description="限定表格名称"),
):
    """全文搜索表格内容。"""
    service = get_agent_service()
    return service.search(keyword=q, table=table)


# R26: history 路由必须在 /{stem} 之前注册，否则 /history 被 /{stem} 当 stem 参数吃掉（同 T2）
@router.get("/history", response_model=list)
async def list_table_history(
    table: str = Query(..., description="表格 stem"),
    sheet: str = Query(default="", description="sheet 名（空=该表所有 sheet）"),
    since: int = Query(default=24, ge=1, le=168, description="近 N 小时"),
):
    """R26: 查询配表操作历史（近 N 小时）。"""
    from engine.table_history import list_history
    return list_history(table, sheet=sheet, since_hours=since)


@router.post("/history/rollback/{record_id}", response_model=RowOpResponse)
async def rollback_history(record_id: str):
    """R26: 按记录 id 回滚单次变更。"""
    service = get_agent_service()
    return service.rollback_history(record_id)


@router.get("/{stem}", response_model=TableInfo)
async def get_table(stem: str):
    """获取单个表格的详细信息。"""
    service = get_agent_service()
    result = service.get_table_detail(stem)
    if result is None:
        raise HTTPException(404, f"表格 '{stem}' 不存在")
    return result


@router.get("/{stem}/sheets/{sheet}", response_model=SheetDataPage)
async def get_sheet_data(
    stem: str,
    sheet: str,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=100, ge=1, le=1000),
    include_columns: bool = Query(default=False, description="R21: 同时返回列约束 columns，减少配表模式往返"),
):
    """读取指定 sheet 的分页数据。

    R20: 响应含 columns_meta（列名+列号），供同名列消歧。
    R21: include_columns=true 时同时返回 columns（列约束）。
    """
    service = get_agent_service()
    result = service.get_sheet_data(stem, sheet, page=page, page_size=page_size,
                                    include_columns=include_columns)
    if result is None:
        raise HTTPException(404, f"表格 '{stem}' 或 Sheet '{sheet}' 不存在")
    return result


@router.get("/{stem}/sheets/{sheet}/columns", response_model=list[FormColumn])
async def get_sheet_columns(stem: str, sheet: str):
    """R8: 读取指定 sheet 的列约束（类型/必填/唯一/外键），供前端 hover 列头 tooltip。"""
    service = get_agent_service()
    result = service.get_sheet_columns(stem, sheet)
    if result is None:
        raise HTTPException(404, f"表格 '{stem}' 或 Sheet '{sheet}' 不存在")
    return result


@router.get("/{stem}/sheets/{sheet}/suggest", response_model=SuggestResponse)
async def suggest_rows(
    stem: str,
    sheet: str,
    value: str = Query(..., min_length=1, description="待匹配的定位值（如法宝名称）"),
    col: str = Query(default="", description="定位列名或列号；为空自动取名称列"),
    top: int = Query(default=3, ge=1, le=10, description="返回相近项数量"),
):
    """R19: 名称定位失败时返回 top-N 相近行（模糊匹配）。

    供配表模式定位失败回退：调此端点拿相近候选行（含行号/值/相似度/字段摘要），
    展示给用户选择后再走 cell/update 等操作。
    """
    service = get_agent_service()
    return service.suggest_rows(stem, sheet, value, col=col, top=top)


# ── 表单式新增 ──

@router.post("/add-form/build", response_model=AddFormResponse)
async def build_add_form(req: AddFormBuildRequest):
    """自然语言匹配表 → 返回表头 + 约束 + 空行（供前端渲染可填写表单）。"""
    service = get_agent_service()
    return service.build_add_form(req.text)


@router.post("/add-form/validate", response_model=AddFormValidateResponse)
async def validate_add_form_row(req: FormValidateRequest):
    """逐列校验表单填写：必填/类型/唯一/范围，返回 errors 供前端标红。"""
    service = get_agent_service()
    return service.validate_add_row(req)


@router.post("/add-form/commit", response_model=AddFormCommitResponse)
async def commit_add_form_row(req: FormCommitRequest):
    """校验通过后插入新行并按主键排序。confirm=True 跳过校验。

    R23: dry_run=true 只校验+返回预览（inserted_values）不写盘；
    用户确认后 dry_run=false 真写。配表模式强制二段提交。
    """
    service = get_agent_service()
    return service.commit_add_row(req)


@router.post("/cell/update", response_model=CellUpdateResponse)
async def update_cell(req: CellUpdateRequest):
    """R8: 单元格原地更新（跳过 NL 解析直接写值）。

    供前端 TablesView 双击单元格编辑后提交：校验类型/唯一性 → 转换值 → 写值。
    """
    service = get_agent_service()
    return service.update_cell(req)


@router.post("/cells/batch-update", response_model=BatchCellUpdateResponse)
async def batch_update_cells(req: BatchCellUpdateRequest):
    """R22: 同行多列事务性批量改值。

    atomic=true（默认）先逐列校验（类型/唯一），任一失败则全部不写；
    atomic=false 逐列校验+写，失败的列跳过。供配表模式多字段一次提交。
    """
    service = get_agent_service()
    return service.update_cells_batch(req)


@router.post("/row/delete", response_model=RowOpResponse)
async def delete_row(req: RowDeleteRequest):
    """R24: 删行 + 公式引用位移提示。"""
    service = get_agent_service()
    return service.delete_row(req)


@router.post("/row/insert", response_model=RowOpResponse)
async def insert_row(req: RowInsertRequest):
    """R24: 插行 + 样式继承 + 公式位移提示。"""
    service = get_agent_service()
    return service.insert_row(req)


@router.post("/column/add", response_model=RowOpResponse)
async def add_column(req: ColumnOpRequest):
    """R24: 新增列（高风险，需 confirm=true）。在指定列左侧插入新列。"""
    service = get_agent_service()
    return service.add_column(req)


@router.post("/column/delete", response_model=RowOpResponse)
async def delete_column(req: ColumnOpRequest):
    """R24: 删列（高风险，需 confirm=true）。"""
    service = get_agent_service()
    return service.delete_column(req)

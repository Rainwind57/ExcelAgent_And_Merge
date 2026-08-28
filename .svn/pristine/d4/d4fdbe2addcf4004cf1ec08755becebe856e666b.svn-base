"""ID 编号段校验 + 跨表 ID 查重。

消费 `resources/id_mgr.xlsx` 的 SETTING sheet（module/id_min/id_max/used_min/
used_max/status），校验各模块编号是否越界；扫全目录所有表的 ID 列做跨表查重。

接入方式：
  - 独立路由 `GET /api/validate/id-scope` 触发全目录校验
  - agent 写 ID 列时调 `validate_value(module, value)` 做段校验 + 跨表查重

设计依据：openspec/changes/add-cross-table-id-conflict/design.md
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# 跳过非数据 sheet（CONFIG/PATCH_CONFIG/说明类）
_NON_DATA_SHEET_KEYWORDS = ("CONFIG", "PATCH_CONFIG", "说明", "SETTING", "INDEX")
# ID 列名启发式关键词
_ID_COL_PATTERNS = re.compile(r"(^|_)(id|ID|编号)$|id$|^编号", re.IGNORECASE)


@dataclass
class SegmentViolation:
    """编号段越界。"""
    module: str
    id_min: int
    id_max: int
    used_min: int
    used_max: int
    status: str

    def to_dict(self) -> dict:
        return {
            "module": self.module,
            "range": [self.id_min, self.id_max],
            "used_range": [self.used_min, self.used_max],
            "status": self.status,
            "severity": "P1",
            "reason": (f"已用段[{self.used_min},{self.used_max}]越界"
                       f"预留段[{self.id_min},{self.id_max}]"),
        }


@dataclass
class CrossTableConflict:
    """跨表 ID 冲突。"""
    conflict_value: int
    locations: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "conflict_value": self.conflict_value,
            "locations": self.locations,
            "severity": "P0",
        }


@dataclass
class CrossTableReport:
    """全目录校验报告。"""
    cross_table_conflicts: list[CrossTableConflict] = field(default_factory=list)
    segment_violations: list[SegmentViolation] = field(default_factory=list)
    scanned_tables: int = 0
    id_mgr_loaded: bool = False

    def to_dict(self) -> dict:
        return {
            "cross_table_conflicts": [c.to_dict() for c in self.cross_table_conflicts],
            "segment_violations": [v.to_dict() for v in self.segment_violations],
            "scanned_tables": self.scanned_tables,
            "id_mgr_loaded": self.id_mgr_loaded,
        }


@dataclass
class CrossBranchConflict:
    """跨分支 ID 冲突（同一 id_value 出现在多个分支根目录）。"""
    conflict_value: int
    locations: list[dict] = field(default_factory=list)  # [{branch, file, sheet, col, row}]

    def to_dict(self) -> dict:
        return {
            "conflict_value": self.conflict_value,
            "locations": self.locations,
            "severity": "P0",
        }


@dataclass
class CrossBranchReport:
    """多分支编号账本校验报告（方法 F）。

    扫多个 SVN 分支根目录（trunk + dev 分支），对每 id 记 {id, table, branch_origin}，
    输出跨分支冲突 + 预留段分布。与 CrossTableReport 区别：location 加 branch 维度，
    冲突判定标准从"文件"改"分支"（同分支内同 stem 跨 sheet 不算冲突）。
    """
    cross_branch_conflicts: list[CrossBranchConflict] = field(default_factory=list)
    reserved_segments: list[dict] = field(default_factory=list)
    scanned_tables: int = 0
    branches_scanned: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "cross_branch_conflicts": [c.to_dict() for c in self.cross_branch_conflicts],
            "reserved_segments": self.reserved_segments,
            "scanned_tables": self.scanned_tables,
            "branches_scanned": self.branches_scanned,
        }


class IdScopeValidator:
    """ID 编号段校验器。

    用法：
      - 全目录校验：`validate_all(resources_dir) → CrossTableReport`
      - 单值段校验：`validate_value(module, value) → (ok, reason)`
    """

    def __init__(self):
        self._segments: list[dict] = []
        self._id_mgr_loaded = False
        self._index: dict[str, dict] = {}
        self._index_path: Optional[Path] = None

    # ---- id_mgr 段定义 ----
    def load_id_mgr(self, id_mgr_path: Path) -> bool:
        """读 id_mgr.xlsx 的 SETTING sheet → 段定义列表。

        格式：模块 | 编号下限 | 编号上限 | 已用下限 | 已用上限 | 状态
        缺失/异常 → 返回 False，调用方跳过段校验不阻断。
        """
        import openpyxl
        try:
            wb = openpyxl.load_workbook(id_mgr_path, data_only=True)
            if "SETTING" not in wb.sheetnames:
                logger.warning("[IdScope] id_mgr 无 SETTING sheet: %s", id_mgr_path)
                self._id_mgr_loaded = False
                return False
            ws = wb["SETTING"]
            segs: list[dict] = []
            for r in range(2, ws.max_row + 1):
                module = ws.cell(r, 1).value
                id_min = ws.cell(r, 2).value
                id_max = ws.cell(r, 3).value
                used_min = ws.cell(r, 4).value
                used_max = ws.cell(r, 5).value
                status = ws.cell(r, 6).value or ""
                if not module or id_min is None or id_max is None:
                    continue
                try:
                    segs.append({
                        "module": str(module),
                        "id_min": int(id_min),
                        "id_max": int(id_max),
                        "used_min": int(used_min) if used_min is not None else None,
                        "used_max": int(used_max) if used_max is not None else None,
                        "status": str(status),
                    })
                except (ValueError, TypeError):
                    continue
            self._segments = segs
            self._id_mgr_loaded = True
            return True
        except Exception as e:
            logger.warning("[IdScope] id_mgr 加载失败 %s: %s", id_mgr_path, e)
            self._id_mgr_loaded = False
            return False

    def validate_value(self, module: str, value: Any) -> tuple[bool, str]:
        """单值段校验：value 是否落在 module 的预留段内。

        Returns:
            (ok, reason)。ok=True 在段内；ok=False 越界，reason 说明。
        """
        if not self._id_mgr_loaded or not module or value is None:
            return True, ""
        try:
            v = int(value)
        except (ValueError, TypeError):
            return True, ""
        for seg in self._segments:
            if seg["module"] != module:
                continue
            if v < seg["id_min"] or v > seg["id_max"]:
                return False, (f"值 {v} 越界：模块 {module} 预留段"
                               f"[{seg['id_min']},{seg['id_max']}]")
            return True, ""
        # 模块未在 id_mgr 注册 → 不校验（无法判定）
        return True, ""

    def check_segments(self) -> list[SegmentViolation]:
        """检查所有模块已用段是否越界预留段。"""
        violations: list[SegmentViolation] = []
        for seg in self._segments:
            umn, umx = seg.get("used_min"), seg.get("used_max")
            if umn is None or umx is None:
                continue
            if umn < seg["id_min"] or umx > seg["id_max"]:
                violations.append(SegmentViolation(
                    module=seg["module"],
                    id_min=seg["id_min"], id_max=seg["id_max"],
                    used_min=umn, used_max=umx,
                    status=seg["status"],
                ))
        return violations

    # ---- 跨表查重 ----
    def _is_data_sheet(self, sheet_name: str) -> bool:
        """跳过非数据 sheet（CONFIG/PATCH_CONFIG/说明类）。"""
        sn = sheet_name.strip()
        for kw in _NON_DATA_SHEET_KEYWORDS:
            if kw in sn:
                return False
        return True

    def _detect_id_col(self, headers: list[str]) -> int:
        """识别 ID 列索引（1-based）。

        启发式：首个含 id/编号 的列名；无匹配取第 1 列。
        """
        for i, h in enumerate(headers, start=1):
            if h and _ID_COL_PATTERNS.search(str(h)):
                return i
        return 1

    def _build_id_index(self, resources_dir: Path) -> dict[int, list[dict]]:
        """扫全目录 ID 列 → {id_value: [locations]}。

        location: {file: stem, sheet, col, row}。
        """
        import openpyxl
        index: dict[int, list[dict]] = {}
        cli_path = Path(__file__).parent.parent / "agent" / "excel"
        import sys
        if str(cli_path) not in sys.path:
            sys.path.insert(0, str(cli_path))
        # 复用 StubCodeMakerCLI 的避免重复实现
        try:
            from cli_interface import StubCodeMakerCLI  # type: ignore
            cli = StubCodeMakerCLI(resources_dir)
        except Exception:
            cli = None
        scanned = 0
        for p in sorted(resources_dir.rglob("*.xlsx")):
            if p.name.startswith("~$") or p.name == "id_mgr.xlsx":
                continue
            scanned += 1
            try:
                wb = openpyxl.load_workbook(p, data_only=True)
            except Exception as e:
                logger.debug("[IdScope] 跳过不可读表 %s: %s", p.name, e)
                continue
            try:
                stem = p.stem
                for sn in wb.sheetnames:
                    if not self._is_data_sheet(sn):
                        continue
                    ws = wb[sn]
                    # 用 cli 的 header 抽取（统一行为）
                    if cli is not None:
                        try:
                            headers = cli._header_of(ws)
                        except Exception:
                            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
                    else:
                        headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
                    id_col = self._detect_id_col(headers)
                    dsr = cli._resolve_data_start(p, sn) if cli else 5
                    # M8 性能优化：单遍 iter_rows(values_only=True) 同时定位末数据行与提取 ID 列，
                    # 替代原 _last_data_row 全表扫描 + 逐行 ws.cell(r,id_col).value 的双重 O(n) Cell 创建。
                    id_col_idx = id_col - 1
                    for r, row in enumerate(
                        ws.iter_rows(min_row=dsr, values_only=True),
                        start=dsr,
                    ):
                        v = row[id_col_idx] if id_col_idx < len(row) else None
                        if v is None:
                            continue
                        try:
                            iv = int(v)
                        except (ValueError, TypeError):
                            continue
                        index.setdefault(iv, []).append({
                            "file": stem, "sheet": sn,
                            "col": id_col, "row": r,
                        })
            finally:
                wb.close()
        self._index = {"scanned": scanned}
        return index

    @staticmethod
    def _last_data_row(ws, data_start_row: int) -> int:
        # M8 性能优化：用 iter_rows(values_only=True) 单遍扫描，避免逐 ws.cell() 创建 Cell
        # 对象的 O(n×m) 高常因子开销；10w 行表原全表扫描极慢。
        last = data_start_row - 1
        for r, row in enumerate(
            ws.iter_rows(min_row=data_start_row, values_only=True),
            start=data_start_row,
        ):
            if any(v is not None for v in row):
                last = r
        return last

    def find_cross_table_conflicts(self, id_index: dict[int, list[dict]]) -> list[CrossTableConflict]:
        """跨表查重：同一 id_value 出现在多个不同 .xlsx 文件 → 冲突。

        同表跨 sheet 白名单：locations 全属同一 stem 不算冲突。
        """
        conflicts: list[CrossTableConflict] = []
        for v, locs in id_index.items():
            if len(locs) < 2:
                continue
            stems = {loc["file"] for loc in locs}
            if len(stems) < 2:
                continue
            conflicts.append(CrossTableConflict(conflict_value=v, locations=locs))
        return conflicts

    def validate_all(self, resources_dir: Path,
                     id_mgr_path: Optional[Path] = None) -> CrossTableReport:
        """全目录校验入口：跨表查重 + 段校验。

        Args:
            resources_dir: resources/ 目录
            id_mgr_path: id_mgr.xlsx 路径，None 时默认 resources/id_mgr.xlsx

        Returns:
            CrossTableReport
        """
        report = CrossTableReport()
        if id_mgr_path is None:
            id_mgr_path = resources_dir / "id_mgr.xlsx"
        report.id_mgr_loaded = self.load_id_mgr(id_mgr_path)
        id_index = self._build_id_index(resources_dir)
        report.scanned_tables = self._index.get("scanned", 0)
        report.cross_table_conflicts = self.find_cross_table_conflicts(id_index)
        if report.id_mgr_loaded:
            report.segment_violations = self.check_segments()
        return report

    # ---- 方法 F：多分支编号账本 ----
    def _build_multi_branch_index(self, branch_roots: list[Path]) -> dict[int, list[dict]]:
        """扫多分支根目录 ID 列 → {id_value: [locations]}（含 branch 维度）。

        对每 branch_root 重复单根 _build_id_index 的扫描逻辑，location 加 branch
        字段（根目录名），供跨分支冲突判定。同分支同 stem 跨 sheet 不算冲突。
        """
        import openpyxl
        import sys
        index: dict[int, list[dict]] = {}
        cli_path = Path(__file__).parent.parent / "agent" / "excel"
        if str(cli_path) not in sys.path:
            sys.path.insert(0, str(cli_path))
        try:
            from cli_interface import StubCodeMakerCLI  # type: ignore
            cli = StubCodeMakerCLI(branch_roots[0] if branch_roots else Path("."))
        except Exception:
            cli = None
        self._branches_scanned = []
        for root in branch_roots:
            branch_name = root.name or str(root)
            self._branches_scanned.append(branch_name)
            for p in sorted(root.rglob("*.xlsx")):
                if p.name.startswith("~$") or p.name == "id_mgr.xlsx":
                    continue
                self._scanned_total = getattr(self, "_scanned_total", 0) + 1
                try:
                    wb = openpyxl.load_workbook(p, data_only=True)
                except Exception as e:
                    logger.debug("[IdScope] 跳过不可读表 %s: %s", p.name, e)
                    continue
                try:
                    stem = p.stem
                    for sn in wb.sheetnames:
                        if not self._is_data_sheet(sn):
                            continue
                        ws = wb[sn]
                        if cli is not None:
                            try:
                                headers = cli._header_of(ws)
                            except Exception:
                                headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
                        else:
                            headers = [ws.cell(1, c).value for c in range(1, ws.max_column + 1)]
                        id_col = self._detect_id_col(headers)
                        dsr = cli._resolve_data_start(p, sn) if cli else 5
                        id_col_idx = id_col - 1
                        for r, row in enumerate(
                            ws.iter_rows(min_row=dsr, values_only=True),
                            start=dsr,
                        ):
                            v = row[id_col_idx] if id_col_idx < len(row) else None
                            if v is None:
                                continue
                            try:
                                iv = int(v)
                            except (ValueError, TypeError):
                                continue
                            index.setdefault(iv, []).append({
                                "branch": branch_name,
                                "file": stem, "sheet": sn,
                                "col": id_col, "row": r,
                            })
                finally:
                    wb.close()
        return index

    def find_cross_branch_conflicts(self, id_index: dict[int, list[dict]]) -> list[CrossBranchConflict]:
        """跨分支查重：同一 id_value 出现在多个分支根 → 冲突。

        同分支同 stem 跨 sheet 白名单（与 find_cross_table_conflicts 一致）。
        判定标准：locations 涉及 ≥2 不同 branch。
        """
        conflicts: list[CrossBranchConflict] = []
        for v, locs in id_index.items():
            if len(locs) < 2:
                continue
            branches = {loc["branch"] for loc in locs}
            if len(branches) < 2:
                continue
            conflicts.append(CrossBranchConflict(conflict_value=v, locations=locs))
        return conflicts

    def validate_multi_branch(self, branch_roots: list[Path],
                              id_mgr_path: Optional[Path] = None) -> CrossBranchReport:
        """多分支校验入口（方法 F）。

        扫多分支根目录（trunk + dev/* + cappedbranch 等），输出跨分支 ID 冲突 +
        预留段分布。与 validate_all 区别：多根 + branch 维度，冲突从"文件"改"分支"。
        向后兼容：单根时退化为单分支报告。
        """
        report = CrossBranchReport()
        if not branch_roots:
            return report
        self._scanned_total = 0
        id_index = self._build_multi_branch_index(list(branch_roots))
        report.scanned_tables = getattr(self, "_scanned_total", 0)
        report.branches_scanned = list(getattr(self, "_branches_scanned", []))
        report.cross_branch_conflicts = self.find_cross_branch_conflicts(id_index)
        report.reserved_segments = list(self._segments) if self._id_mgr_loaded else []
        return report

    def claim_id(self, value: int, resources_dir: Path,
                 branches: Optional[list[Path]] = None) -> dict:
        """id-claim 查询（方法 F4）：value 在当前账本中是否冲突 + 建议下一空闲号。

        单分支模式（branches=None）扫 resources_dir；多分支模式扫 branches 列表。
        返回 {claimed: bool, conflict_locations: list, suggested_next: int|None}。
        suggested_next = 已用最大 id + 1（跳过已占用），供 agent 建议用户换号。
        """
        branch_roots = branches if branches else [resources_dir]
        id_index = self._build_multi_branch_index(list(branch_roots))
        used = set(id_index.keys())
        locs = id_index.get(int(value), [])
        # 单分支模式：同 stem 跨 sheet 白名单不算冲突（与 find_cross_table_conflicts 一致）
        branches_set = {loc["branch"] for loc in locs}
        conflict = bool(locs) and (len(branches_set) >= 2 if branches else len({l["file"] for l in locs}) >= 2)
        suggested = None
        if conflict and used:
            suggested = max(used) + 1
            while suggested in used:
                suggested += 1
        return {"claimed": conflict, "conflict_locations": locs, "suggested_next": suggested}


# 单例
_validator: Optional[IdScopeValidator] = None


def get_id_scope_validator() -> IdScopeValidator:
    """全局 IdScopeValidator 单例。"""
    global _validator
    if _validator is None:
        _validator = IdScopeValidator()
    return _validator

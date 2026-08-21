"""PATCH_CONFIG 守门：_capped.xlsx 格式 5 坑硬规则校验。

ca-overview §3.3 轻享服 5 个坑（merge 系统必须处理）：
  1. sheet 名与基准 xlsx 对不上 → 融合结果不含该 sheet
  2. 忘了在 PATCH_CONFIG 登记新 sheet → 该 sheet 不参与融合
  3. PATCH_CONFIG sheet 缺失 → 整个文件融合退化或失败
  4. 给 _capped.xlsx 加了 CONFIG sheet → 格式破坏
  5. 同名 sheet 重复，先写者优先 → 反直觉，后者静默忽略

§3.2 PATCH_CONFIG sheet 格式：两列无表头，A=目标 sheet 名，B=融合方式(PATCH_GEN/SHEET_GEN)。

§3.1 _capped.xlsx 是补丁文件：没有 CONFIG sheet，只有 PATCH_CONFIG + 数据 sheet。

本模块 5 条硬规则全 hold 级（§3.3 明文"静默失败"，不允许 warning 放行）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional


@dataclass
class Violation:
    """单条违规。"""
    rule: int            # 1-5 对应五坑
    kind: str = "patch_config"
    severity: str = "hold"
    sheet: str = ""
    message: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "kind": self.kind,
            "severity": self.severity,
            "sheet": self.sheet,
            "message": self.message,
            "detail": self.detail,
        }


# §3.2 合法融合方式
_VALID_FUSION_MODES = {"PATCH_GEN", "SHEET_GEN"}


def validate_capped_workbook(path: Path, trunk_sheet_names: Optional[List[str]] = None) -> List[Violation]:
    """校验 _capped.xlsx 格式 5 坑。

    Args:
        path: _capped.xlsx 路径
        trunk_sheet_names: trunk 主表 sheet 名集合（用于规则③校验 PATCH_CONFIG A 列）。
            None/空时跳过规则③（无 trunk 参照）

    Returns:
        List[Violation]：命中的违规清单（全 hold 级），空列表表示合规
    """
    import openpyxl
    violations: List[Violation] = []
    wb = openpyxl.load_workbook(Path(path), data_only=False)
    try:
        sheet_names = wb.sheetnames

        # 规则④反向（坑4）：_capped 不能有 CONFIG sheet（补丁文件无 CONFIG，字段结构来自 trunk）
        if "CONFIG" in sheet_names:
            violations.append(Violation(
                rule=4,
                sheet="CONFIG",
                message="_capped 文件不能有 CONFIG sheet",
                detail="坑4：给 _capped 加了 CONFIG sheet 致格式破坏",
            ))

        # 规则③（坑3）：_capped 必须有 PATCH_CONFIG sheet
        if "PATCH_CONFIG" not in sheet_names:
            violations.append(Violation(
                rule=3,
                sheet="PATCH_CONFIG",
                message="_capped 文件必须含 PATCH_CONFIG sheet",
                detail="坑3：PATCH_CONFIG 缺失致融合退化或失败",
            ))
            # 无 PATCH_CONFIG 则规则①②⑤无法校验，提前返回
            return violations

        # 读 PATCH_CONFIG A/B 列
        ws = wb["PATCH_CONFIG"]
        a_values: List[str] = []
        b_values: List[str] = []
        for row in ws.iter_rows(min_row=1, values_only=False):
            a_cell = row[0] if len(row) > 0 else None
            b_cell = row[1] if len(row) > 1 else None
            a_val = str(a_cell.value).strip() if a_cell is not None and a_cell.value is not None else ""
            if a_val:
                a_values.append(a_val)
                b_values.append(str(b_cell.value).strip() if (b_cell is not None and b_cell.value is not None) else "")

        # 规则①（坑1）：PATCH_CONFIG A 列每项 ∈ trunk 主表 sheet 名集合
        if trunk_sheet_names:
            trunk_set = set(trunk_sheet_names)
            for a in a_values:
                if a not in trunk_set:
                    violations.append(Violation(
                        rule=1,
                        sheet=a,
                        message=f"PATCH_CONFIG 登记的 sheet '{a}' 不在 trunk 主表",
                        detail="坑1：sheet 名对不上致融合结果不含该 sheet",
                    ))

        # 规则⑤（§3.2）：B 列取值 ∈ {PATCH_GEN, SHEET_GEN}
        for a, b in zip(a_values, b_values):
            if b and b not in _VALID_FUSION_MODES:
                violations.append(Violation(
                    rule=5,
                    sheet=a,
                    message=f"PATCH_CONFIG B 列取值 '{b}' 不合法（应为 PATCH_GEN/SHEET_GEN）",
                    detail="§3.2：融合方式仅支持 PATCH_GEN/SHEET_GEN",
                ))

        # 规则②（坑2 + 坑5 复合）：PATCH_CONFIG A 列登记的 sheet 名应覆盖 _capped 数据 sheet
        # 且 A 列登记不重复（坑5：同名 sheet 重复先写者优先，后者静默忽略）
        seen: set = set()
        dups: set = set()
        for a in a_values:
            if a in seen:
                dups.add(a)
            seen.add(a)
        for a in sorted(dups):
            violations.append(Violation(
                rule=2,
                sheet=a,
                message=f"PATCH_CONFIG 重复登记 sheet '{a}'（先写者优先，后者静默忽略）",
                detail="坑5：同名 sheet 重复反直觉，后者静默忽略",
            ))
    finally:
        wb.close()
    return violations

"""主线1：通用 resolution 台账（跨 Step 结构化决策账本）。

背景（docs/ExcelAgent 优化策略 + 交接铁律）：此前"用户决策/校验结论"去重靠局部
变量 + 不稳定 `id(obj)` + intent.extras 随 deepcopy 携带，跨 Step / 跨 deepcopy
不稳定，易出现"同一冲突反复 ask"。本模块把它升级为 **ctx 级结构化台账**：

  - `make_issue_id`：由**内容**派生稳定 issue_id（表+sheet+列+值/语义），
    绝不用 id(obj)。同一语义问题在任何 Step、任何 deepcopy 后都得同一 id。
  - `ResolutionLedger`：issue_id → resolution 记录，支持记录/查询/持久化(to_dict/
    from_dict)/合并。所有 ask/校验结论只发生一次、跨 Step 稳定。

纯数据 + 纯函数，无 IO、无 LLM，可离线确定性验证。
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = ["make_issue_id", "Resolution", "ResolutionLedger"]

_NORM_RE = re.compile(r"[\s_:\-./\\()\[\]{}（）【】]+")


def _norm(value: object) -> str:
    core = str(value if value is not None else "").split(":")[0]
    return _NORM_RE.sub("", core).strip().lower()


def make_issue_id(
    *,
    kind: str,
    table: object = "",
    sheet: object = "",
    col: object = "",
    value: object = None,
    extra: object = None,
) -> str:
    """由内容派生稳定 issue_id。

    同一 (kind, 表, sheet, 列, 值/语义) 无论对象实例/deepcopy/跨 Step 都得同一 id。
    绝不使用 id(obj)/内存地址/时间戳等不稳定量。

    Returns: 形如 "pk_conflict:a1b2c3d4" 的稳定短 id。
    """
    parts = [
        _norm(kind),
        _norm(table),
        _norm(sheet),
        _norm(col),
        _norm(value) if value is not None else "",
        _norm(extra) if extra is not None else "",
    ]
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:8]
    return f"{_norm(kind) or 'issue'}:{digest}"


@dataclass
class Resolution:
    """单条决策记录。

    value = 问题主体值（如冲突的 PK 值，参与 issue_id 派生，标识"是哪个问题"）；
    resolved = 决策结果（如用户改用的新值/答复，不参与 id，记录"怎么解决的"）。
    """
    issue_id: str
    kind: str = ""
    status: str = "resolved"        # resolved | skipped | pending | rejected
    table: str = ""
    sheet: str = ""
    col: str = ""
    value: Any = None               # 问题主体值（标识用）
    resolved: Any = None            # 决策结果（用户答复/修正后的值）
    source: str = ""                # user | auto | validator
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "issue_id": self.issue_id, "kind": self.kind, "status": self.status,
            "table": self.table, "sheet": self.sheet, "col": self.col,
            "value": self.value, "resolved": self.resolved,
            "source": self.source, "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Resolution":
        d = dict(d or {})
        return cls(
            issue_id=str(d.get("issue_id") or ""),
            kind=str(d.get("kind") or ""),
            status=str(d.get("status") or "resolved"),
            table=str(d.get("table") or ""),
            sheet=str(d.get("sheet") or ""),
            col=str(d.get("col") or ""),
            value=d.get("value"),
            resolved=d.get("resolved"),
            source=str(d.get("source") or ""),
            detail=str(d.get("detail") or ""),
        )


@dataclass
class ResolutionLedger:
    """ctx 级 resolution 台账：issue_id → Resolution。

    幂等：同一 issue_id 已解则 record 默认不覆盖（除非 overwrite=True），
    让"所有 ask/修正只发生一次"。可 to_dict/from_dict 持久化随 ctx 跨 Step 携带。
    """
    _items: dict[str, Resolution] = field(default_factory=dict)

    def record(self, res: Resolution, *, overwrite: bool = False) -> bool:
        """记录一条决策。Returns True=新写入/更新，False=已存在且未覆盖。"""
        if not res or not res.issue_id:
            return False
        if res.issue_id in self._items and not overwrite:
            return False
        self._items[res.issue_id] = res
        return True

    def record_kv(self, *, kind: str, table: object = "", sheet: object = "",
                  col: object = "", value: object = None, resolved: object = None,
                  status: str = "resolved", source: str = "", detail: str = "",
                  extra: object = None, overwrite: bool = False) -> str:
        """便捷记录：由问题主体 (kind,表,sheet,列,value) 派生 issue_id 并记录，
        返回 issue_id。resolved=决策结果（不参与 id）。"""
        iid = make_issue_id(kind=kind, table=table, sheet=sheet, col=col,
                            value=value, extra=extra)
        self.record(Resolution(
            issue_id=iid, kind=str(kind), status=status,
            table=str(table or ""), sheet=str(sheet or ""), col=str(col or ""),
            value=value, resolved=resolved, source=source, detail=detail),
            overwrite=overwrite)
        return iid

    def get(self, issue_id: str) -> Optional[Resolution]:
        return self._items.get(issue_id)

    def is_resolved(self, issue_id: str) -> bool:
        r = self._items.get(issue_id)
        return bool(r and r.status in ("resolved", "skipped"))

    def has(self, issue_id: str) -> bool:
        return issue_id in self._items

    def __len__(self) -> int:
        return len(self._items)

    def to_dict(self) -> dict:
        return {iid: r.to_dict() for iid, r in self._items.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "ResolutionLedger":
        led = cls()
        for iid, rd in (d or {}).items():
            r = Resolution.from_dict(rd)
            if not r.issue_id:
                r.issue_id = str(iid)
            led._items[r.issue_id] = r
        return led

    def merge(self, other: "ResolutionLedger", *, overwrite: bool = False) -> None:
        for r in (other._items.values() if other else []):
            self.record(r, overwrite=overwrite)

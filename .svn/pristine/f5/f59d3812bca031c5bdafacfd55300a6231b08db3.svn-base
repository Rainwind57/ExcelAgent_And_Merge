"""枚举值解析器：将自然语言中文含义转换为 int 数值，或将数值反查回中文含义。

核心功能：
  1. load() 从 enum_mappings.yaml 加载所有枚举映射
  2. resolve_label(stem, sheet, col_name, label) → int | None
     中文标签→int值转换（如 "紫"→3）
  3. resolve_value(stem, sheet, col_name, value) → str | None
     int值→中文标签转换（如 3→"橙"）
  4. has_enum(stem, sheet, col_name) → bool
     查询某列是否有枚举映射
  5. get_labels(stem, sheet, col_name) → list[str]
     获取某列所有中文标签

用法：
    resolver = EnumResolver.load()
    val = resolver.resolve_label("pet", "Pet", "灵兽品质", "紫")  # → 3
    label = resolver.resolve_value("pet", "Pet", "灵兽品质", 3)  # → "橙"
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False

_SKILLS_DIR = Path(__file__).resolve().parents[1] / "skills"
# T12: L1 自动派生文件迁移到 L1_derived/（路径随 _SKILLS_DIR 动态算，兼容 monkeypatch）

# D10: pending 枚举候选文件（不修改 L1_derived/enum_mappings.yaml，经门禁后才合并）
_PENDING_ENUM_PATH = _SKILLS_DIR / "_pending" / "enum_candidates.yaml"
# D10/7.5: confidence 低于此值拒绝写 pending
ENUM_PENDING_MIN_CONF = 0.7


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _load_yaml(name: str) -> dict:
    """T12: L1 自动派生文件优先从 L1_derived/ 加载，回退根目录（兼容未迁移/测试）。"""
    if not _HAS_YAML:
        return {}
    p_l1 = _SKILLS_DIR / "L1_derived" / name
    if p_l1.exists():
        return yaml.safe_load(p_l1.read_text(encoding="utf-8")) or {}
    p = _SKILLS_DIR / name
    if not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


@dataclass
class EnumEntry:
    """单个枚举条目：label(中文)→value(int) 的映射对"""
    label: str
    value: int


@dataclass
class EnumResolver:
    """枚举值双向解析器。

    Attributes:
        _label_to_value: {(stem, sheet, col_name): {label: int}}
        _value_to_label: {(stem, sheet, col_name): {int: label}}
    """
    _label_to_value: dict[tuple[str, str, str], dict[str, int]] = field(default_factory=dict)
    _value_to_label: dict[tuple[str, str, str], dict[int, str]] = field(default_factory=dict)
    # D10: pending 候选（运行时缓存，register_label 写入，promote 后合并 L1 并清空）
    _pending_l2v: dict[tuple[str, str, str], dict[str, int]] = field(default_factory=dict)

    @classmethod
    def load(cls) -> "EnumResolver":
        data = _load_yaml("enum_mappings.yaml")
        inst = cls.from_dict(data)
        inst._load_pending_into_cache()
        return inst

    def _load_pending_into_cache(self) -> None:
        """D10: 从 _pending/enum_candidates.yaml 加载到内存 _pending_l2v（供 resolve_label 即时查）。"""
        self._pending_l2v = {}
        if not _HAS_YAML:
            return
        data = self._load_pending_raw()
        for c in data.get("candidates", []):
            try:
                key = (c["stem"], c["sheet"], c["col"])
                self._pending_l2v.setdefault(key, {})[c["label"]] = int(c["value"])
            except (KeyError, ValueError, TypeError):
                continue

    @staticmethod
    def _load_pending_raw() -> dict:
        """读 _pending/enum_candidates.yaml。"""
        if not _HAS_YAML or not _PENDING_ENUM_PATH.exists():
            return {"candidates": []}
        try:
            return yaml.safe_load(_PENDING_ENUM_PATH.read_text(encoding="utf-8")) or {"candidates": []}
        except Exception:
            return {"candidates": []}

    @staticmethod
    def _save_pending_raw(data: dict) -> None:
        """写 _pending/enum_candidates.yaml。"""
        if not _HAS_YAML:
            return
        _PENDING_ENUM_PATH.parent.mkdir(parents=True, exist_ok=True)
        body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        _PENDING_ENUM_PATH.write_text(body, encoding="utf-8")

    def clear_pending(self) -> None:
        """D10.3: promote 合并后清空 pending（文件 + 内存）。"""
        self._pending_l2v = {}
        if _PENDING_ENUM_PATH.exists():
            try:
                _PENDING_ENUM_PATH.write_text("candidates: []\n", encoding="utf-8")
            except Exception:
                pass

    def register_label(self, stem: str, sheet: str, col: str, label: str,
                       value: int, confidence: float) -> bool:
        """D10.1/7.5: 写 pending 候选（不修改 L1_derived/enum_mappings.yaml）。

        confidence < 0.7 拒绝（7.5）。
        内存 _pending_l2v 同步更新，resolve_label 即时可查。
        同 (stem,sheet,col,label) 已存在则替换（保留更高 confidence）。
        返回是否写入。
        """
        if not _HAS_YAML:
            return False
        try:
            conf = float(confidence)
        except (ValueError, TypeError):
            return False
        if conf < ENUM_PENDING_MIN_CONF:
            return False
        try:
            iv = int(value)
        except (ValueError, TypeError):
            return False
        # 已在 L1 严格映射中 → 无需写 pending
        key = self._make_keys(stem, sheet, col)
        if key in self._label_to_value and self._label_to_value[key].get(label) == iv:
            return False
        data = self._load_pending_raw()
        candidates = data.get("candidates", [])
        # 去重：同 (stem,sheet,col,label) 保留 confidence 更高者
        existing = next((c for c in candidates
                         if c.get("stem") == stem and c.get("sheet") == sheet
                         and c.get("col") == col and c.get("label") == label), None)
        if existing:
            if conf <= float(existing.get("confidence", 0.0)):
                return False
            existing.update({"value": iv, "confidence": conf, "ts": _now_iso()})
        else:
            candidates.append({
                "stem": stem, "sheet": sheet, "col": col,
                "label": label, "value": iv, "confidence": conf,
                "ts": _now_iso(), "source": "llm_infer",
            })
        data["candidates"] = candidates
        self._save_pending_raw(data)
        # 内存缓存同步
        self._pending_l2v.setdefault(key, {})[label] = iv
        return True

    @classmethod
    def from_dict(cls, data: dict) -> "EnumResolver":
        inst = cls()
        tables = data.get("tables", {})
        for stem, sheets in tables.items():
            for sheet, sheet_data in sheets.items():
                cols = sheet_data.get("columns", {})
                for col_name, col_info in cols.items():
                    values = col_info.get("values", [])
                    if not values:
                        continue
                    key = (stem, sheet, col_name)
                    l2v: dict[str, int] = {}
                    v2l: dict[int, str] = {}
                    for entry in values:
                        label = entry.get("label", "")
                        value = entry.get("value")
                        if label and value is not None:
                            l2v[label] = int(value)
                            v2l[int(value)] = label
                    if l2v:
                        inst._label_to_value[key] = l2v
                        inst._value_to_label[key] = v2l
        return inst

    def _make_keys(self, stem: str, sheet: str, col_name: str) -> tuple[str, str, str]:
        name = col_name.split(":")[0].strip()
        return (stem, sheet, name)

    def has_enum(self, stem: str, sheet: str, col_name: str) -> bool:
        """该列是否有枚举映射。"""
        key = self._make_keys(stem, sheet, col_name)
        return key in self._label_to_value

    def resolve_label(self, stem: str, sheet: str, col_name: str, label: str) -> Optional[int]:
        """中文标签 → int 值。未命中返回 None。

        D10: L1 严格映射未命中时，回退查 pending 候选（register_label 写入）。
        """
        key = self._make_keys(stem, sheet, col_name)
        l2v = self._label_to_value.get(key)
        if l2v is not None:
            # 精确匹配
            if label in l2v:
                return l2v[label]
            # 模糊匹配（忽略大小写、去空格）
            nl = label.strip()
            for k, v in l2v.items():
                if k.strip() == nl:
                    return v
        # D10: 回退 pending 候选
        pl2v = self._pending_l2v.get(key)
        if pl2v is not None:
            if label in pl2v:
                return pl2v[label]
            nl = label.strip()
            for k, v in pl2v.items():
                if k.strip() == nl:
                    return v
        return None

    def resolve_value(self, stem: str, sheet: str, col_name: str, value: Any) -> Optional[str]:
        """int 值 → 中文标签。未命中返回 None。"""
        key = self._make_keys(stem, sheet, col_name)
        v2l = self._value_to_label.get(key)
        if v2l is None:
            return None
        try:
            iv = int(value) if not isinstance(value, int) else value
            return v2l.get(iv)
        except (ValueError, TypeError):
            return None

    def get_labels(self, stem: str, sheet: str, col_name: str) -> list[str]:
        """获取某列所有中文标签。"""
        key = self._make_keys(stem, sheet, col_name)
        l2v = self._label_to_value.get(key)
        if l2v is None:
            return []
        return list(l2v.keys())

    def get_mapping(self, stem: str, sheet: str, col_name: str) -> dict[str, int]:
        """获取某列的 label→value 全量映射。"""
        key = self._make_keys(stem, sheet, col_name)
        return dict(self._label_to_value.get(key, {}))

    def format_for_llm(self, stem: str, sheet: str, col_name: str) -> str:
        """生成 LLM 可读的枚举说明文本。如: 灵兽品质: 蓝=1, 紫=2, 橙=3"""
        mapping = self.get_mapping(stem, sheet, col_name)
        if not mapping:
            return ""
        pairs = ", ".join(f"{k}={v}" for k, v in mapping.items())
        return f"{col_name}: {pairs}"

    def format_context(self, stem: str) -> str:
        """为该 stem 下所有 table 的所有 sheet 生成枚举上下文文本，供 LLM 注入。"""
        lines: list[str] = []
        for (s, sheet, col_name), l2v in self._label_to_value.items():
            if s != stem:
                continue
            pairs = ", ".join(f"{k}={v}" for k, v in l2v.items())
            lines.append(f"  {sheet}.{col_name}: {pairs}")
        if not lines:
            return ""
        return "\n".join([f"## 枚举值映射（{stem}）"] + lines)

    def format_all_context(self) -> str:
        """生成全量枚举上下文文本。"""
        by_stem: dict[str, list[str]] = {}
        for (stem, sheet, col_name), l2v in self._label_to_value.items():
            pairs = ", ".join(f"{k}={v}" for k, v in l2v.items())
            by_stem.setdefault(stem, []).append(f"  {sheet}.{col_name}: {pairs}")
        if not by_stem:
            return ""
        parts: list[str] = ["## 枚举值映射"]
        for stem, items in sorted(by_stem.items()):
            parts.append(f"### {stem}")
            parts.extend(items)
        return "\n".join(parts)

    # ── 更新与持久化 ───────────────────────────────────────────

    def update_column(self, stem: str, sheet: str, col_name: str,
                      mappings: dict[str, int] | list[dict]) -> int:
        """更新单列的枚举映射（内存）。返回条目数。

        Args:
            mappings: {"蓝":1,"紫":2} 或 [{"label":"蓝","value":1},...]
        """
        name = col_name.split(":")[0].strip()
        key = (stem, sheet, name)
        l2v: dict[str, int] = {}
        v2l: dict[int, str] = {}
        if isinstance(mappings, dict):
            items = mappings.items()
        else:
            items = ((e.get("label", ""), e.get("value")) for e in mappings)
        for label, value in items:
            if label and value is not None:
                l2v[str(label)] = int(value)
                v2l[int(value)] = str(label)
        if l2v:
            self._label_to_value[key] = l2v
            self._value_to_label[key] = v2l
        elif key in self._label_to_value:
            del self._label_to_value[key]
            del self._value_to_label[key]
        return len(l2v)

    def save(self) -> bool:
        """把当前内存中的映射持久化到 enum_mappings.yaml。"""
        try:
            import yaml
        except ImportError:
            return False
        tables: dict = {}
        for (stem, sheet, col_name), l2v in self._label_to_value.items():
            entries = [{"label": k, "value": v} for k, v in l2v.items()]
            tables.setdefault(stem, {}).setdefault(
                sheet, {}).setdefault("columns", {})[col_name] = {
                "type": "int",
                "values": entries,
            }
        data = {
            "version": "1.0",
            "auto_generated": False,
            "tables": tables,
        }
        p = _SKILLS_DIR / "enum_mappings.yaml"
        body = yaml.safe_dump(data, allow_unicode=True, sort_keys=True,
                              default_flow_style=False)
        p.write_text(body, encoding="utf-8")
        return True


# ── 单例缓存 ─────────────────────────────────────────────────

_enum_resolver: Optional[EnumResolver] = None


def get_enum_resolver() -> EnumResolver:
    global _enum_resolver
    if _enum_resolver is None:
        _enum_resolver = EnumResolver.load()
    return _enum_resolver


def reset_enum_resolver() -> None:
    """丢弃内存缓存，下次 get_enum_resolver() 重新从磁盘加载。"""
    global _enum_resolver
    _enum_resolver = None

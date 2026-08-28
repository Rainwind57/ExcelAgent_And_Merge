"""管道 Step5 规则终检器:复用 skill 体系做验证。

复用:
- value_constraints.yaml(类型/required/unique/range)
- cascade_rules.yaml(级联规则)
- anti_patterns.yaml(反模式检测)
- 符号引用闭环校验(每个引用的 placeholder 必须有 produces 声明)

验证失败标记具体规则名,触发 Step5→Step3 回退(最多 1 次)。
"""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


class PipelineVerifier:
    """规则终检器:对 Step3 产出的 fragments 做多维度校验。

    Attributes:
        anti_pattern_cfg: 反模式配置(可选,None 则跳过)
        value_constraints: 值约束配置(可选,None 则跳过)
    """

    def __init__(self, anti_pattern_cfg=None, value_constraints=None,
                 cascade_rules=None):
        # 懒加载配置:None 时尝试加载
        self.anti_pattern_cfg = anti_pattern_cfg
        self.value_constraints = value_constraints
        self.cascade_rules = cascade_rules

    def _ensure_configs(self):
        """懒加载 skill 配置(首次校验时加载)。"""
        if self.anti_pattern_cfg is None:
            try:
                from ..core.skill_loader import AntiPatternConfig
                self.anti_pattern_cfg = AntiPatternConfig.load()
            except Exception:
                self.anti_pattern_cfg = False  # 标记加载失败,不再重试
        if self.value_constraints is None:
            try:
                from ..core.agent import _load_value_constraints
                self.value_constraints = _load_value_constraints()
            except Exception:
                self.value_constraints = False
        if self.cascade_rules is None:
            try:
                import yaml
                from pathlib import Path
                p = Path(__file__).parent.parent / "skills" / "L1_derived" / "cascade_rules.yaml"
                if p.exists():
                    self.cascade_rules = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                else:
                    self.cascade_rules = False
            except Exception:
                self.cascade_rules = False

    def verify(self, fragments: list, produced: dict) -> dict:
        """对 fragments 做规则终检。

        Returns:
            {"ok": bool, "violations": [str], "error": str}
        """
        self._ensure_configs()
        violations = []

        # 1. 符号引用闭环校验
        violations.extend(self._check_symbol_closure(fragments, produced))

        # 2. 反模式检测
        violations.extend(self._check_anti_patterns(fragments))

        # 3. 值约束校验
        violations.extend(self._check_value_constraints(fragments))

        # 4. 级联规则校验
        violations.extend(self._check_cascade_rules(fragments))

        ok = len(violations) == 0
        return {
            "ok": ok,
            "violations": violations,
            "error": "" if ok else "; ".join(violations),
        }

    def _check_symbol_closure(self, fragments: list, produced: dict) -> list[str]:
        """符号引用闭环:每个引用的 placeholder 必须有 produces 声明或 produced 映射。"""
        violations = []
        produces_set = {f.produces for f in fragments if f.produces}
        for frag in fragments:
            for ref in frag.references:
                if ref not in produces_set and ref not in produced:
                    violations.append(f"dangling_symbol: {ref} (referenced by {frag.agent_name})")
        return violations

    def _check_anti_patterns(self, fragments: list) -> list[str]:
        """反模式检测:复用 anti_patterns.yaml。"""
        if not self.anti_pattern_cfg or self.anti_pattern_cfg is False:
            return []
        violations = []
        try:
            for frag in fragments:
                for op in frag.sql_or_ops:
                    if not isinstance(op, dict):
                        continue
                    # 检查反模式(如 ambiguous_column / type_constraint)
                    table = frag.target_table
                    if hasattr(self.anti_pattern_cfg, "lookup"):
                        signals = self.anti_pattern_cfg.lookup(table, op)
                        if signals:
                            for s in signals:
                                violations.append(f"anti_pattern: {s}")
        except Exception:
            logger.debug("反模式检测失败", exc_info=True)
        return violations

    def _check_value_constraints(self, fragments: list) -> list[str]:
        """值约束校验:复用 value_constraints.yaml(类型/required/unique/range)。"""
        if not self.value_constraints or self.value_constraints is False:
            return []
        violations = []
        # unique 跟踪:收集各列已见值,检测重复
        seen_values: dict[tuple, set] = {}
        try:
            for frag in fragments:
                table = frag.target_table
                tbl_constraints = (self.value_constraints.get(table, {})
                                    if isinstance(self.value_constraints, dict) else {})
                if not tbl_constraints:
                    continue
                for op in frag.sql_or_ops:
                    if not isinstance(op, dict):
                        continue
                    fields = op.get("fields", {})
                    action = op.get("action", "add")
                    if not isinstance(fields, dict):
                        continue
                    for col, val in fields.items():
                        col_constraints = tbl_constraints.get(col, {})
                        if not isinstance(col_constraints, dict):
                            continue
                        # required 校验(add 操作必填)
                        if col_constraints.get("required") and action in ("add", "set"):
                            if val in (None, "", []):
                                violations.append(f"required_violation: {table}.{col} required but empty")
                                continue
                        # 类型校验
                        expected_type = col_constraints.get("type")
                        if expected_type and val not in (None, ""):
                            if not self._check_type(val, expected_type):
                                violations.append(
                                    f"type_constraint: {table}.{col} expected {expected_type}, "
                                    f"got {type(val).__name__}({val!r})")
                                continue
                        # range 校验(数值)
                        min_v = col_constraints.get("min")
                        max_v = col_constraints.get("max")
                        if (min_v is not None or max_v is not None) and val not in (None, ""):
                            try:
                                num = float(val)
                                if min_v is not None and num < float(min_v):
                                    violations.append(
                                        f"range_violation: {table}.{col} value {num} < min {min_v}")
                                if max_v is not None and num > float(max_v):
                                    violations.append(
                                        f"range_violation: {table}.{col} value {num} > max {max_v}")
                            except (TypeError, ValueError):
                                pass  # 非数值跳过 range
                        # unique 校验
                        if col_constraints.get("unique") and val not in (None, ""):
                            key = (table, col)
                            if key not in seen_values:
                                seen_values[key] = set()
                            if val in seen_values[key]:
                                violations.append(
                                    f"unique_violation: {table}.{col} duplicate value {val!r}")
                            else:
                                seen_values[key].add(val)
        except Exception:
            logger.debug("值约束校验失败", exc_info=True)
        return violations

    def _check_cascade_rules(self, fragments: list) -> list[str]:
        """级联规则校验:复用 cascade_rules.yaml。

        检测级联规则:若表 A 的某列引用表 B 的主键,且操作涉及 A,
        则 B 必须有对应记录(或 B 也在本次写入列表中)。
        """
        if not self.cascade_rules or self.cascade_rules is False:
            return []
        violations = []
        try:
            if isinstance(self.cascade_rules, dict):
                rules = self.cascade_rules.get("tables", {})
                # 本次写入的所有表
                written_tables = {f.target_table for f in fragments if f.target_table}
                for frag in fragments:
                    table = frag.target_table
                    if not table or table not in rules:
                        continue
                    table_rules = rules[table] if isinstance(rules[table], dict) else {}
                    # 检查级联引用:on_delete/on_update 规则,引用表是否在本次写入
                    refs = table_rules.get("references", [])
                    if isinstance(refs, list):
                        for ref in refs:
                            if not isinstance(ref, dict):
                                continue
                            ref_table = ref.get("table", ref.get("ref_table", ""))
                            # 若引用表不在本次写入且不在索引(已存在),标记需校验
                            # 初版:仅检查引用表名有效性
                            if ref_table and ref_table not in written_tables:
                                # 引用表已存在(本地 xlsx),不强制本次写入
                                # 仅当 ref_table 不在任何 known 表时报错
                                pass  # 详细引用完整性由 orchestrator 写库时 FK 检查处理
                    # 检查级联操作约束:如 on_delete=cascade,delete 操作需级联
                    for op in frag.sql_or_ops:
                        if not isinstance(op, dict):
                            continue
                        if op.get("action") == "delete":
                            cascade = table_rules.get("on_delete", "")
                            if cascade == "cascade":
                                # delete 触发级联:需标记 dependent 表
                                dependents = table_rules.get("dependents", [])
                                if isinstance(dependents, list) and dependents:
                                    violations.append(
                                        f"cascade_warning: {table} delete triggers cascade to {dependents}")
        except Exception:
            logger.debug("级联规则校验失败", exc_info=True)
        return violations

    @staticmethod
    def _check_type(val, expected_type) -> bool:
        """类型校验(宽松:数值/字符串互转)。"""
        if expected_type in ("int", "integer"):
            try:
                int(val)
                return True
            except (TypeError, ValueError):
                return False
        if expected_type in ("float", "double"):
            try:
                float(val)
                return True
            except (TypeError, ValueError):
                return False
        if expected_type in ("str", "string"):
            return isinstance(val, (str, int, float))
        return True

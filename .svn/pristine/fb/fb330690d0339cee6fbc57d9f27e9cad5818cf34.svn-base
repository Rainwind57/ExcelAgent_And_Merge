"""Excel 引擎子包：TableAgent、解析器、匹配器、索引、CLI 等表格操作实现。

本子包承载配表增删查改的底层引擎；LangGraph 图编排层在父包 agent/ 根目录。

模块按职责拆到子目录（formula/ locator/ parser/ repair/ cli/ core/），
但通过本 __init__ 把子模块注册为 agent.excel.<name> 顶层属性，
保持 `from agent.excel.<name> import X` 旧路径兼容，外部代码零改动。
"""

import importlib
import sys

# 已迁移到子目录的模块：旧顶层名 → 新子目录路径
_MOVED = {
    "formula_ref_shifter": "formula.formula_ref_shifter",
    "formula_semantics": "formula.formula_semantics",
    "formula_cache_validator": "formula.formula_cache_validator",
    "table_index": "locator.table_index",
    "alias_mapping": "locator.alias_mapping",
    "fuzzy_matcher": "locator.fuzzy_matcher",
    "column_name_resolver": "locator.column_name_resolver",
    "column_matcher": "locator.column_matcher",
    "table_locator": "locator.table_locator",
    "error_classifier": "repair.error_classifier",
    "repair_context": "repair.repair_context",
    "repair_playbook": "repair.repair_playbook",
    "cascade_planner": "repair.cascade_planner",
    "nl_parser": "parser.nl_parser",
    "codemaker_parser": "parser.codemaker_parser",
    "schema_infer": "parser.schema_infer",
    "segmenter": "parser.segmenter",
    "multi_intent_splitter": "parser.multi_intent_splitter",
    "cli_interface": "cli.cli_interface",
    "cli_instrument": "cli.cli_instrument",
    "real_cli": "cli.real_cli",
    "xlsx_tool": "cli.xlsx_tool",
    "table_relations": "core.table_relations",
    "cascade_resolver": "core.cascade_resolver",
    "produces_inference": "core.produces_inference",
    "cross_table_splitter": "core.cross_table_splitter",
    "skill_loader": "core.skill_loader",
    "skill_updater": "core.skill_updater",
    "confidence_config": "core.confidence_config",
    "semantic_gate": "core.semantic_gate",
    "checkpoint": "core.checkpoint",
    "enum_resolver": "core.enum_resolver",
    "analyze_enum_columns": "core.analyze_enum_columns",
    "evidence_logger": "core.evidence_logger",
    "dialog_logger": "core.dialog_logger",
    "backup_audit": "core.backup_audit",
    "date_normalizer": "core.date_normalizer",
    "style_utils": "core.style_utils",
    "agent": "core.agent",
    "llm_context": "core.llm_context",
    "operation_orchestrator": "core.operation_orchestrator",
    "file_watcher": "core.file_watcher",
    "skill_context": "core.skill_context",
    "step_ai_enhancer": "core.step_ai_enhancer",
    "table_resolver": "core.table_resolver",
}


def _reexport() -> None:
    pkg = __package__  # "agent.excel" 或 "server.agent.excel"
    for short, sub in _MOVED.items():
        legacy = f"{pkg}.{short}"
        if legacy in sys.modules:
            continue
        target = f"{pkg}.{sub}"
        try:
            mod = importlib.import_module(target)
        except Exception:
            continue
        sys.modules[legacy] = mod
        setattr(sys.modules[pkg], short, mod)


_reexport()

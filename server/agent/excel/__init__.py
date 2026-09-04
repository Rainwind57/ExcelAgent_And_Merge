"""Excel 引擎子包：TableAgent、解析器、匹配器、索引、CLI 等表格操作实现。

本子包承载配表增删查改的底层引擎；LangGraph 图编排层在父包 agent/ 根目录。

模块按职责拆到子目录（formula/ locator/ parser/ repair/ cli/ core/），
但通过本 __init__ 把子模块注册为 agent.excel.<name> 顶层属性，
保持 `from agent.excel.<name> import X` 旧路径兼容，外部代码零改动。
"""

import importlib
import sys

# 已迁移到子目录的模块：旧顶层名 → 新子目录路径
# 仅保留 _compat.py 依赖（agent.<name> 旧路径）与生产代码直引的键；
# 其余引用方已统一改为真实子路径（agent.excel.<subdir>.<name>）。
_MOVED = {
    "formula_ref_shifter": "formula.formula_ref_shifter",
    "formula_semantics": "formula.formula_semantics",
    "formula_cache_validator": "formula.formula_cache_validator",
    "table_index": "locator.table_index",
    "alias_mapping": "locator.alias_mapping",
    "fuzzy_matcher": "locator.fuzzy_matcher",
    "column_matcher": "locator.column_matcher",
    "table_locator": "locator.table_locator",
    "nl_parser": "parser.nl_parser",
    "codemaker_parser": "parser.codemaker_parser",
    "schema_infer": "parser.schema_infer",
    "segmenter": "parser.segmenter",
    "cli_interface": "cli.cli_interface",
    "real_cli": "cli.real_cli",
    "xlsx_tool": "cli.xlsx_tool",
    "table_relations": "core.table_relations",
    "skill_loader": "core.skill_loader",
    "skill_updater": "core.skill_updater",
    "confidence_config": "core.confidence_config",
    "backup_audit": "core.backup_audit",
    "style_utils": "core.style_utils",
    "file_watcher": "core.file_watcher",
    "agent": "core.agent",
    "llm_context": "core.llm_context",
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

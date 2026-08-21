"""向后兼容：把已迁移到 agent.excel.* 的旧模块路径注册到 sys.modules。

保留 `from agent.<module> import X` 与 `from server.agent.<module> import X`
两种旧导入路径可用（含下划线私有名），无需为每个模块维护独立 shim 文件。

原理：在包导入时把 `agent.<m>` 映射到已加载的 `agent.excel.<m>` 模块对象，
Python 导入系统命中 sys.modules 后直接返回，getattr 取任意名称（含下划线）。
"""

from __future__ import annotations

import importlib
import sys

_MOVED_MODULES = (
    "agent",
    "alias_mapping",
    "backup_audit",
    "cli_interface",
    "codemaker_parser",
    "column_matcher",
    "confidence_config",
    "file_watcher",
    "formula_cache_validator",
    "formula_ref_shifter",
    "formula_semantics",
    "fuzzy_matcher",
    "llm_context",
    "nl_parser",
    "real_cli",
    "schema_infer",
    "segmenter",
    "skill_loader",
    "table_index",
    "table_locator",
    "table_relations",
    "table_resolver",
    "xlsx_tool",
)


def _install() -> None:
    pkg = __package__  # "agent" 或 "server.agent"（视导入方式而定）
    for name in _MOVED_MODULES:
        legacy = f"{pkg}.{name}"
        if legacy in sys.modules:
            continue
        target = f"{pkg}.excel.{name}"
        try:
            mod = importlib.import_module(target)
        except Exception:
            # 单个模块导入失败不阻断其余注册；失败模块的旧路径将自然抛 ImportError
            continue
        sys.modules[legacy] = mod


_install()

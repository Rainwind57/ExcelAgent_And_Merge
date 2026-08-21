"""SubAgent 并行派发器（兼容入口）。

方案 A1 双骨架统一：本模块已迁移至 ..engine_core.dispatcher，此处仅保留
shim re-export 维持原 import 路径（subagent.dispatcher / ..subagent.dispatcher）
继续可用，行为与迁移前完全一致。新增代码请直接 from ..engine_core.dispatcher。
"""
from ..engine_core.dispatcher import *  # noqa: F401,F403
from ..engine_core.dispatcher import dispatch  # noqa: F401  显式 re-export 主符号

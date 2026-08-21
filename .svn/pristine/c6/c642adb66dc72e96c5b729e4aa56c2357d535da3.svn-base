"""断点管理器（兼容入口）。

方案 A1 双骨架统一：本模块已迁移至 ..engine_core.checkpoint，此处仅保留
shim re-export 维持原 import 路径（core.checkpoint / ..checkpoint / agent.excel.checkpoint）
继续可用，行为与迁移前完全一致。新增代码请直接 from ..engine_core.checkpoint。
"""
from ..engine_core.checkpoint import *  # noqa: F401,F403
from ..engine_core.checkpoint import CheckpointManager  # noqa: F401  显式 re-export 主符号

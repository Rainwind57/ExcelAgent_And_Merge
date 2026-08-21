"""SubAgent 角色细化（兼容入口）。

方案 A1 双骨架统一：本模块已迁移至 ..engine_core.roles，此处仅保留
shim re-export 维持原 import 路径（subagent.roles / ..subagent.roles）
继续可用，行为与迁移前完全一致。新增代码请直接 from ..engine_core.roles。
"""
from ..engine_core.roles import *  # noqa: F401,F403
from ..engine_core.roles import (  # noqa: F401  显式 re-export 4 个角色 Agent
    DialogFillAgent, ItemNpcFillAgent, ButterflyEventFillAgent, GenericFillAgent,
)

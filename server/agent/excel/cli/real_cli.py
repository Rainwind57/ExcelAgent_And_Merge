"""RealCodeMakerCLI 占位实现。

原 real_cli（openpyxl 基座 + codemaker 增强）已从仓库移除，但 agent/__init__、
graph、agent_service 等多处仍硬导入 RealCodeMakerCLI。本占位继承 StubCodeMakerCLI
（纯 openpyxl 实现），保证导入链不断；调用方（agent_service / graph）本就有
try/except 降级到 StubCodeMakerCLI 的兜底，行为等价。

dry_run（preview）压测路径不触发写盘，Stub 实现完全够用。
真实写盘增强（codemaker 侧公式/校验）需恢复原 real_cli 后再替换本文件。
"""
from __future__ import annotations

from pathlib import Path

from .cli_interface import StubCodeMakerCLI


class RealCodeMakerCLI(StubCodeMakerCLI):
    """占位 RealCodeMakerCLI：行为等同 StubCodeMakerCLI。"""

    name = "real"

    def __init__(self, workspace: Path, header_row: int = 1, data_start_row: int = 5):
        super().__init__(workspace=workspace, header_row=header_row,
                         data_start_row=data_start_row)


__all__ = ["RealCodeMakerCLI"]

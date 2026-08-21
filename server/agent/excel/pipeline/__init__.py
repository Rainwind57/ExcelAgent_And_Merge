"""excel-agent 管道层：7 步固定工作流编排。

Step0 断点检查 → Step1 拆解 → Step2 分区 → Step3 并行填表
→ Step4 汇总 → Step5 验证 → Step6 写库 → Step7 清理+报告

StepResult 保留 7 步管道内部 status 语义（pending/done/failed，适合断点续跑），
不与 core/pipeline/contracts.StepResult 合并；出口经 pipeline_to_v2_event 桥接到
V2 SSE 形态（见 contracts.py docstring 边界说明）。
"""

from .pipeline import Pipeline, PipelineResult  # noqa: F401
from .types import (  # noqa: F401
    PipelineContext,
    StepResult,
    AgentFragment,
    DocIntent,
    StepCard,
    pipeline_to_v2_event,
)

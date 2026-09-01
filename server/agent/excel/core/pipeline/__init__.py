"""4-Step V2 流水线（§设计 S1+S2+S3）。

步间隔离（过渡期尽力保证，非绝对；详见 contracts.py docstring）：
Step1 解析 / Step2 校验 / Step3 执行 / Step4 汇总，串行互不干扰。

Step3 零 LLM（D4）：通过 no_llm 参数透传到 _run_single（替代原
CODEMAKER_EXECUTE_NO_LLM env 进程级突变）。

开关：CODEMAKER_EXCEL_PIPELINE_V2 默认 ON 走 V2；=0 显式降级到旧 run() 6 步。
"""
from .contracts import (
    SSE, STEP1_PARSE, STEP2_VALIDATE, STEP3_EXECUTE, STEP4_CONCLUDE,
    STEP_ORDER, STEP_TITLES,
    StepContext, StepError, StepHardError, StepResult,
)
from .orchestrator import ExcelAgentPipeline, is_v2_enabled
from .services import ExcelAgentServices
from .semantic_plan import (
    compile_semantic_plan_to_intents,
    compile_semantic_plan_to_operation_items,
)
from .plan_completeness import audit_plan_completeness
from .value_extractor import extract_fields_from_text
from .step1_parse_subagent import Step1ParseSubAgent
from .step2_validate_subagent import Step2ValidateSubAgent
from .step3_execute_subagent import Step3ExecuteSubAgent
from .step4_conclude_subagent import Step4ConcludeSubAgent

__all__ = [
    "SSE", "STEP1_PARSE", "STEP2_VALIDATE", "STEP3_EXECUTE", "STEP4_CONCLUDE",
    "STEP_ORDER", "STEP_TITLES",
    "StepContext", "StepError", "StepHardError", "StepResult",
    "ExcelAgentPipeline", "is_v2_enabled", "ExcelAgentServices",
    "compile_semantic_plan_to_intents",
    "compile_semantic_plan_to_operation_items",
    "audit_plan_completeness",
    "extract_fields_from_text",
    "Step1ParseSubAgent", "Step2ValidateSubAgent",
    "Step3ExecuteSubAgent", "Step4ConcludeSubAgent",
]

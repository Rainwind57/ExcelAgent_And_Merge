"""LangGraph 运行时配置。

可通过 graph.invoke(input, config={"configurable": {...}}) 覆盖默认值；
亦兼容 langgraph-cli / LangGraph Platform 读取（langgraph.json 入口）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


def _default_resources_dir() -> str:
    """默认资源目录：项目根 / resources（与 server/config.RESOURCES_DIR 一致）。"""
    return str(Path(__file__).resolve().parent.parent.parent / "resources")


class Configuration(BaseModel):
    """主智能体配置。"""

    codemaker_server_url: str = Field(
        default_factory=lambda: os.environ.get(
            "CODEMAKER_SERVER_URL", "http://127.0.0.1:8666"))
    codemaker_username: str = Field(
        default_factory=lambda: os.environ.get("CODEMAKER_USERNAME", ""))
    codemaker_password: str = Field(
        default_factory=lambda: os.environ.get("CODEMAKER_PASSWORD", ""))
    model_name: str = Field(
        default_factory=lambda: os.environ.get("CODEMAKER_MODEL", "codemaker"))
    directory: str = Field(default_factory=_default_resources_dir)

    # verify-repair 迭代环（capability: verify-repair-loop）
    enable_verify_repair_loop: bool = Field(
        default_factory=lambda: os.environ.get("ENABLE_VERIFY_REPAIR_LOOP", "1") != "0")
    # skill tools 恢复路径（capability: skill-executor-tools）
    enable_skill_tools_recovery: bool = Field(
        default_factory=lambda: os.environ.get("ENABLE_SKILL_TOOLS_RECOVERY", "1") != "0")
    # verify→repair 迭代环最大轮数（含首次执行共 max_rounds+1 次尝试）
    verify_repair_max_rounds: int = Field(
        default_factory=lambda: int(os.environ.get("VERIFY_REPAIR_MAX_ROUNDS", "3")))
    # 单次 repair Level 2 的 skill tool 调用上限
    skill_tool_call_limit: int = Field(
        default_factory=lambda: int(os.environ.get("SKILL_TOOL_CALL_LIMIT", "4")))

    # === 4-Step V2 流水线开关（统一单一开关）===
    # =1（默认）走 core/pipeline V2 硬隔离契约（step1_parse/step2_validate/
    # step3_execute/step4_conclude）；=0 退回旧 run() 6 步 _phase_* 路径（降级通道）。
    # 废弃 CODEMAKER_4STEP_LOOP/enable_4step_loop：旧合并分支（s1_parse 命名）
    # 与 V2 契约 step_id 不同导致前端双路由键混叠，已统一收敛到 V2。
    excel_pipeline_v2: bool = Field(
        default_factory=lambda: os.environ.get("CODEMAKER_EXCEL_PIPELINE_V2", "1") != "0")
    # Schema-driven 拆分开关（灰度总开关）：=1 时 ParseAgent 主导 schema 注入 LLM 拆分，
    # 取代 splitter fast-path 模板硬拆；=0 时 splitter 为主、LLM 兜底（现状）。
    schema_driven_decompose: bool = Field(
        default_factory=lambda: os.environ.get("CODEMAKER_SCHEMADRIVEN_DECOMPOSE", "0") != "0")
    # Schema 拉取并发上限（lazy 拉取候选 sheet 的 ThreadPool size）。
    schema_fetch_concurrency: int = Field(
        default_factory=lambda: int(os.environ.get("CODEMAKER_SCHEMA_FETCH_CONCURRENCY", "8")))
    # Schema 拉取候选 sheet 总数上限（防 token/IO 爆炸）。
    schema_fetch_sheet_limit: int = Field(
        default_factory=lambda: int(os.environ.get("CODEMAKER_SCHEMA_FETCH_SHEET_LIMIT", "15")))
    # splitter fast-path 阈值（env 可调）：cross_intents_nl 长度 < 此值时
    # 触发 _llm_chain_decompose 接管。默认 2（保持现状）；调到 99 强制 DecomposeAgent 接管所有命中 fast-path 的输入。
    splitter_decompose_threshold: int = Field(
        default_factory=lambda: int(os.environ.get("CODEMAKER_SPLITTER_DECOMPOSE_THRESHOLD", "2")))
    # === §3 ExecuteAgent 去 LLM 开关 ===
    # =1 时 _phase_execute 失败路径跳过 verify-repair loop + D3 retry-loop 的 LLM
    # 诊断/重试，失败直接结构化进 res.failures（#40），诊断+反模式归纳交 §5 ConcludeAgent。
    # 默认关（保持现状：失败路径含 LLM 诊断+重试+verify-repair 修复）。
    execute_no_llm: bool = Field(
        default_factory=lambda: os.environ.get("CODEMAKER_EXECUTE_NO_LLM", "0") != "0")


def get_config(config: Any | None = None) -> Configuration:
    """从 langgraph config['configurable'] 合并环境默认值。

    config 可为 langgraph 标准的 {"configurable": {...}}，也可直接是扁平 dict。
    """
    base = Configuration()
    if not config:
        return base
    configurable = (
        config.get("configurable", config) if isinstance(config, dict) else {}
    )
    if not isinstance(configurable, dict):
        return base
    data = base.model_dump()
    for k, v in configurable.items():
        if k in data and v is not None:
            data[k] = v
    return Configuration(**data)

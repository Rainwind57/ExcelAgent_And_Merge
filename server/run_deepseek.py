"""独立启动入口：用 DeepSeek 官方 API 直连跑后端（不经 codemaker serve）。

启动：
    uv run python server/run_deepseek.py

前置（在项目根 .env 追加，与 codemaker 配置互不影响）：
    DEEPSEEK_API_KEY=sk-xxxx
    DEEPSEEK_BASE_URL=https://api.deepseek.com
    DEEPSEEK_MODEL=deepseek-chat

原理：
    整个 agent 栈（TableAgent / CodemakerNLParser / StepAIEnhancer /
    CodemakerChatModel / QAHandler / OrchestratorAgent）都经 CodemakerClient
    调 LLM。本入口在 import main 之前，用 DeepSeekClient 的同名方法替换
    CodemakerClient 的方法，此后后端所有 LLM 调用即走 DeepSeek 官方接口，
    无需修改任何业务代码。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVER_DIR = Path(__file__).resolve().parent
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

# 加载项目根 .env（DEEPSEEK_* 在此读取）
try:
    from dotenv import load_dotenv
    load_dotenv(SERVER_DIR.parent / ".env")
except Exception:
    pass


def _install_deepseek_compat() -> None:
    """把 CodemakerClient 的 LLM 相关方法替换为 DeepSeekClient 的实现。"""
    from agent import codemaker_client as cm
    from agent.deepseek_client import DeepSeekClient

    ds = DeepSeekClient()
    cm.CodemakerClient.create_session = ds.create_session          # type: ignore[method-assign]
    cm.CodemakerClient.health_check = ds.health_check              # type: ignore[method-assign]
    cm.CodemakerClient.prompt = ds.prompt                          # type: ignore[method-assign]
    cm.CodemakerClient.extract_json_from_response = staticmethod(  # type: ignore[method-assign]
        ds.extract_json_from_response)
    cm.CodemakerClient.list_models = ds.list_models                # type: ignore[method-assign]
    cm.CodemakerClient.get_messages = ds.get_messages              # type: ignore[method-assign]
    print("[deepseek] CodemakerClient 已切换为 DeepSeek 直连："
          f"base_url={os.environ.get('DEEPSEEK_BASE_URL', 'https://api.deepseek.com')} "
          f"model={os.environ.get('DEEPSEEK_MODEL', 'deepseek-chat')}")


if __name__ == "__main__":
    _install_deepseek_compat()

    import uvicorn
    from main import app

    host = os.environ.get("BACKEND_HOST", "0.0.0.0")
    port = int(os.environ.get("BACKEND_PORT", "8000"))
    uvicorn.run(app, host=host, port=port, reload=False)

"""测试级公共 fixture。

autouse 清空 skill 模块级缓存，避免模块级缓存(_route_cache/_columns_cache/
_col_types_cache/_YAML_CACHE)跨测试泄漏导致假阳性。
"""
import sys
from pathlib import Path

# 收口 sys.path：测试统一 server/ 在 path → agent.* / engine.* / routers.* 命名空间
# （生产 main.py / agent_service.py 同此）。消除 server.agent.* 双命名空间导致的
# 同一物理文件被同时载入为 agent.X 与 server.agent.X 两份模块对象 → isinstance 断裂 /
# 模块级单例重复的运行态非确定性 ImportError。
_SERVER_DIR = str(Path(__file__).resolve().parent.parent)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

import os
# 测试跑完整 skill 路径（mini 回归 + 失败归纳），与 prod 默认相反：
# prod 默认 CODEMAKER_SKIP_REGRESSION=1 跳过同步 LLM 回归、CODEMAKER_INDUCE_PROD=0
# 关生产归纳（提速，见 skill_updater.promote_with_guard / agent.py induce_anti_patterns）；
# 测试需 exercising 这些路径，故 setdefault 显式开启（不覆盖显式设置）。
os.environ.setdefault("CODEMAKER_SKIP_REGRESSION", "0")
os.environ.setdefault("CODEMAKER_INDUCE_PROD", "1")

import pytest


@pytest.fixture(autouse=True)
def _reset_skill_caches():
    try:
        from agent.excel.core import skill_context
        skill_context.reset_skill_context_cache()
    except Exception:
        pass
    try:
        from agent.excel import skill_loader
        skill_loader._YAML_CACHE.clear()
    except Exception:
        pass
    try:
        from agent.excel import formula_cache_validator
        formula_cache_validator.reset_formula_snapshot_cache()
    except Exception:
        pass
    try:
        from routers import diff
        diff.reset_diff_cache()
    except Exception:
        pass
    yield

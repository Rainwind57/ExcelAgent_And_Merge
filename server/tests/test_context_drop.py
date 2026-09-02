"""_context_drop_set 单测（文档 #2/#3「context 默认不注入」）。

验证 DecomposeAgent 从 candidate_groups 计算 context 级剔除集的逻辑：
  - 默认启用，返回 context 级 stem 集合。
  - CODEMAKER_DECOMPOSE_DROP_CONTEXT=0 关闭 → 空集（回退零行为改动）。
  - 无 candidate_groups → 空集（安全降级）。

运行: python -m pytest server/tests/test_context_drop.py -q
"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from agent.excel.subagent.decompose_agent import DecomposeAgent


def _lr(groups):
    return SimpleNamespace(candidate_groups=groups)


def test_default_on_returns_context():
    os.environ.pop("CODEMAKER_DECOMPOSE_DROP_CONTEXT", None)
    da = DecomposeAgent(cli=None)
    lr = _lr({"required": ["pet"], "dependency": ["pet_evolve"],
              "context": ["interaction", "fabao"]})
    assert da._context_drop_set(lr) == {"interaction", "fabao"}


def test_disabled_returns_empty():
    da = DecomposeAgent(cli=None)
    lr = _lr({"context": ["x", "y"]})
    os.environ["CODEMAKER_DECOMPOSE_DROP_CONTEXT"] = "0"
    try:
        assert da._context_drop_set(lr) == set()
    finally:
        os.environ.pop("CODEMAKER_DECOMPOSE_DROP_CONTEXT", None)


def test_no_groups_returns_empty():
    os.environ.pop("CODEMAKER_DECOMPOSE_DROP_CONTEXT", None)
    da = DecomposeAgent(cli=None)
    assert da._context_drop_set(SimpleNamespace()) == set()
    assert da._context_drop_set(_lr({})) == set()


def test_required_dependency_never_dropped():
    """required/dependency 不进入剔除集（不伤动作主语表/多表写入依赖表）。"""
    os.environ.pop("CODEMAKER_DECOMPOSE_DROP_CONTEXT", None)
    da = DecomposeAgent(cli=None)
    lr = _lr({"required": ["pet"], "dependency": ["pet_evolve"], "context": ["spell"]})
    drop = da._context_drop_set(lr)
    assert "pet" not in drop and "pet_evolve" not in drop
    assert drop == {"spell"}

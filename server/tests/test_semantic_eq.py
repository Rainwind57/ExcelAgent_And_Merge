"""#24 语义相等归一单元测试（capability: merge-evaluation）。

直接测 engine.compare._semantic_key / _semantic_eq，验证：
- 数值归一：100 == "100" == "100.0" == "1e2" == 100.0
- 空白 trim："a " == "a" == " a "
- bool/None 统一：None == "" != 0；True != 1
- 非数值字符串按原值比较（不误归一）
- inf/nan 退回字符串（不参与数值归一）

这些 case 即 test_merge_eval 种子 id=2（100/"100.0"/"1e2"）从假冲突→非冲突的根因。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine.compare import _semantic_key, _semantic_eq


def test_numeric_normalization():
    """数值归一：int/float/数值字符串/科学记数法 互通。"""
    assert _semantic_eq(100, "100")
    assert _semantic_eq(100, "100.0")
    assert _semantic_eq(100, "1e2")
    assert _semantic_eq(100, 100.0)
    assert _semantic_eq(0.1, "0.10")
    assert _semantic_eq(0.1, "0.1")
    assert _semantic_eq(-5, "-5.0")
    assert _semantic_eq(50, " 50 ")   # 带空白也归一


def test_whitespace_trim():
    """字符串 trim 空白后比较。"""
    assert _semantic_eq("a", "a ")
    assert _semantic_eq("a", " a")
    assert _semantic_eq("a ", " a ")
    assert _semantic_eq("hello", "  hello  ")


def test_none_and_empty():
    """None 与空串等价；与 0 / False 不等价。"""
    assert _semantic_eq(None, "")
    assert _semantic_eq(None, "   ")   # 纯空白 trim 后为空
    assert not _semantic_eq(None, 0)
    assert not _semantic_eq("", 0)
    assert not _semantic_eq(None, False)


def test_bool_isolated():
    """bool 与数值分离：True != 1，False != 0。"""
    assert _semantic_eq(True, True)
    assert _semantic_eq(False, False)
    assert not _semantic_eq(True, 1)
    assert not _semantic_eq(False, 0)
    assert not _semantic_eq(True, "true")


def test_non_numeric_strings():
    """非数值字符串按 trim 后原值比较，不误归一。"""
    assert _semantic_eq("abc", "abc")
    assert not _semantic_eq("abc", "ABC")     # 大小写敏感
    assert not _semantic_eq("100abc", "100")  # 含字母不归一
    assert not _semantic_eq("abc", "abd")


def test_distinct_keys_for_set():
    """语义 key 入 set 去重：100/"100.0"/"1e2" 归一个 key。"""
    keys = {_semantic_key(v) for v in [100, "100.0", "1e2", 100.0]}
    assert len(keys) == 1, f"语义等值组应归一为 1 个 key，got {keys}"
    keys2 = {_semantic_key(v) for v in [10, 20, 30, "10"]}
    assert len(keys2) == 3, f"10/20/30 应归为 3 个 key（'10'与10 合并），got {keys2}"


def test_inf_nan_fallback():
    """inf/nan 不参与数值归一，退回字符串比较。"""
    # float('inf') 不 finite → 退回 str，两侧 inf 字符串相等
    assert _semantic_eq(float("inf"), "inf")
    # nan != nan（即便退回字符串，"nan"=="nan" 为 True，但语义上 nan 不应等任何值；
    # _semantic_key 对 nan 返回 ('str','nan')，故 _semantic_eq(nan, "nan") 为 True。
    # 这里仅验证不抛异常且不与数值误判）
    assert not _semantic_eq(float("nan"), 0)


def test_seed_id2_scenario():
    """复刻种子 id=2 场景：base=100, dev1="100.0", dev2="1e2" 全等价。"""
    base_val = 100
    versions = {"base": 100, "dev1": "100.0", "dev2": "1e2"}
    base_key = _semantic_key(base_val)
    changed = {fn: v for fn, v in versions.items() if _semantic_key(v) != base_key}
    assert changed == {}, f"id=2 三方语义等值，changed 应为空，got {changed}"
    distinct = {_semantic_key(v) for v in versions.values()}
    assert len(distinct) == 1, f"三方归一后应仅 1 个代表值，got {distinct}"


def test_seed_id1_scenario():
    """复刻种子 id=1 场景：base=10, dev1=20, dev2=30 → 真冲突。"""
    versions = {"base": 10, "dev1": 20, "dev2": 30}
    distinct = {_semantic_key(v) for v in versions.values()}
    assert len(distinct) == 3, f"id=1 三方值不同应归 3 个 key，got {distinct}"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ✓ {name}")
    print("全部 test_semantic_eq 通过")

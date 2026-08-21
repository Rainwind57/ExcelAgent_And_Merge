"""D10 LLM 辅助枚举发现：int 列硬错误时 LLM 推断 label→int 映射。

数据流（design D10）：
    _coerce_value int 列硬错误（resolve_label 未命中）
      → analyze_enum_column(stem, sheet, col, label, llm_call_fn)
      → LLM 推断 {label: {value, confidence}}
      → 0.7 → EnumResolver.register_label 写 pending
      → resolve_label 命中 → 重试 _coerce 成功

7.4: 每列每会话推断结果缓存（_session_cache），避免同列重复 LLM 调用。
7.5: confidence < 0.7 由 register_label 拒绝（不写 pending）。
"""
from __future__ import annotations

import re
from typing import Callable, Optional

try:
    import yaml
    _HAS_YAML = True
except ImportError:
    _HAS_YAML = False


# 7.4: 每会话缓存 (stem, sheet, col) -> {label: {value, confidence}}
_session_cache: dict[tuple[str, str, str], dict] = {}


def reset_session_cache() -> None:
    """清空会话缓存（测试 / 新会话开始时调）。"""
    global _session_cache
    _session_cache = {}


# 7.2: LLM prompt 模板
_ANALYZE_PROMPT = """你是 Excel 表格枚举分析助手。
表 {stem}.{sheet} 的列 [{col}] 是 int 枚举列，但用户输入了字符串值，无法直接转为整数。
请推断该字符串标签对应的 int 枚举值。

待推断标签：{label}

输出格式（YAML，仅输出 YAML 块，勿解释）：
```yaml
{label}:
  value: <推断的 int 值>
  confidence: <0.0-1.0，你对这个推断的把握>
```

规则：
- 若该标签明显是某枚举值（如"攻击"→1、"治疗"→2），给出高 confidence（≥0.7）
- 若无法确定，confidence < 0.7（系统将拒绝采用）
- value 必须是整数
"""


def _parse_llm_yaml(text: str, label: str) -> dict:
    """解析 LLM 输出的 YAML，提取指定 label 的 {value, confidence}。"""
    if not text or not _HAS_YAML:
        return {}
    try:
        # 提取 ```yaml ... ``` 块
        m = re.search(r"```ya?ml?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
        body = m.group(1) if m else text
        data = yaml.safe_load(body) or {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict = {}
    for lbl, info in data.items():
        if isinstance(info, dict):
            try:
                out[str(lbl)] = {"value": int(info.get("value")),
                                 "confidence": float(info.get("confidence", 0.0))}
            except (ValueError, TypeError):
                continue
    return out


def analyze_enum_column(stem: str, sheet: str, col: str, label: str,
                        llm_call_fn: Optional[Callable[[str], str]] = None) -> dict:
    """LLM 推断单标签的枚举映射。

    Args:
        stem/sheet/col: 列定位。
        label: 待推断的字符串标签。
        llm_call_fn: 输入 prompt 返回 LLM 响应文本的回调。
            None → 无 LLM 环境，返回空。

    Returns:
        {label: {value, confidence}} 或 {}（无 client/LLM 失败/缓存空）。

    7.4: 结果缓存到 _session_cache，同列同会话不重复调 LLM。
    """
    key = (stem, sheet, col)
    if key in _session_cache:
        cached = _session_cache[key]
        # 缓存是整列映射，返回其中 label 对应条目（若有）
        if label in cached:
            return {label: cached[label]}
        return {}

    if llm_call_fn is None:
        return {}

    try:
        prompt = _ANALYZE_PROMPT.format(stem=stem, sheet=sheet, col=col, label=label)
        resp_text = llm_call_fn(prompt)
        mapping = _parse_llm_yaml(resp_text, label)
    except Exception:
        mapping = {}

    # 写会话缓存（整列累积，后续同列其他 label 可复用已知映射）
    if mapping:
        existing = _session_cache.setdefault(key, {})
        existing.update(mapping)
    else:
        # 标记该列已尝试（空映射），避免重复调用
        _session_cache.setdefault(key, {})
    return {label: mapping[label]} if label in mapping else {}

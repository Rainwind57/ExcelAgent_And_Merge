"""CLI instrument:tool-call 可见性层。

包装 CodeMakerCLI(或任意 cli),每次 tool 调用生成 ToolRecord 推 SSE。
管道模式(Step6 写库)经 instrument 捕获 tool-call 事件流,供观测/审计消费。

契约(对齐 server/tests/test_pipeline_e2e.py::TestCliInstrument):
- _make_tool_record(name, args, kwargs, result, ok=True, error=None) -> dict
    {"name","ok","cmd","result","args","kwargs","error","ts"}
    ok=True  → result 字段为方法返回值
    ok=False → result 字段为 error 字符串(消费方统一从 result 取展示文本)
    cmd 含方法名,供前端高亮/过滤
- instrument(cli, sink=None, enabled=True) -> 代理 cli
    sink 签名 (event_type: str, payload: dict) -> None;event_type="tool_call"
    enabled=False 直返原 cli(不包代理)
    sink=None 时仍包代理但无推送(调用链一致,仅无事件流)
"""
from __future__ import annotations

import time
from typing import Any, Optional

# 视为 tool 调用需记录的方法名(CodeMakerCLI 操作集合)
_TOOL_METHODS = frozenset({
    "read_header", "list_tables", "get_sheets", "read_sheet",
    "search_rows", "locate_row", "write_cell", "read_cell",
    "append_row", "delete_row",
})

_EVENT_TYPE = "tool_call"


def _make_tool_record(name: str, args: tuple, kwargs: dict,
                      result: Any, ok: bool = True,
                      error: Optional[str] = None) -> dict:
    """构造 tool-call 记录。

    ok=True 时 result 字段为方法返回值;ok=False 时 result 字段为 error 字符串,
    便于消费方统一从 result 取展示文本。cmd 含方法名,供前端高亮/过滤。
    """
    try:
        arg_repr = ", ".join([repr(a) for a in args]
                             + [f"{k}={v!r}" for k, v in kwargs.items()])
    except Exception:
        arg_repr = "<unreprable>"
    return {
        "name": name,
        "ok": ok,
        "cmd": f"{name}({arg_repr})",
        "result": error if not ok else result,
        "args": list(args),
        "kwargs": dict(kwargs),
        "error": error,
        "ts": time.time(),
    }


def _emit_to_sink(sink: Any, record: dict) -> None:
    """统一派发:兼容 callable(event_type, payload) 与含 emit 方法的对象。"""
    if sink is None:
        return
    try:
        if hasattr(sink, "emit") and not callable(sink):
            sink.emit(_EVENT_TYPE, record)
        else:
            sink(_EVENT_TYPE, record)
    except Exception:
        pass


class ToolSink:
    """SSE tool 回调容器:聚合多个 sink 并以 (event_type, payload) 派发。

    兼容两种 sink 形态:
    - callable(event_type, payload):agent_service._tool_sink 形态(主流)
    - 含 emit(event_type, payload) 方法的对象:历史/兼容形态
    """

    def __init__(self, sinks: Optional[list] = None) -> None:
        self._sinks = [s for s in (sinks or []) if callable(s) or hasattr(s, "emit")]

    def emit(self, event_type: str, payload: dict) -> None:
        for s in self._sinks:
            try:
                if hasattr(s, "emit") and not callable(s):
                    s.emit(event_type, payload)
                else:
                    s(event_type, payload)
            except Exception:
                pass


class _InstrumentedCLI:
    """cli 代理:tool 方法调用生成 ToolRecord 推 sink,非 tool 属性透传原 cli。

    用 __getattr__ 拦截 _TOOL_METHODS 中的方法调用,其余属性(含 _cli/_sink
    等实例属性)走正常查找,无递归风险。
    """

    def __init__(self, cli: Any, sink: Any) -> None:
        # 用 object.__setattr__ 避免 __setattr__ 误触发(本类未自定义,仅防御)
        object.__setattr__(self, "_cli", cli)
        object.__setattr__(self, "_sink", sink)

    def _emit(self, record: dict) -> None:
        _emit_to_sink(self._sink, record)

    def __getattr__(self, name: str) -> Any:
        attr = getattr(self._cli, name)
        if name in _TOOL_METHODS and callable(attr):
            sink = self._sink

            def wrapper(*args, **kwargs):
                try:
                    result = attr(*args, **kwargs)
                except Exception as e:
                    err = f"{type(e).__name__}: {e}"
                    _emit_to_sink(sink, _make_tool_record(
                        name, args, kwargs, None, ok=False, error=err))
                    raise
                _emit_to_sink(sink, _make_tool_record(
                    name, args, kwargs, result, ok=True))
                return result

            return wrapper
        return attr


def instrument(cli: Any, sink: Any = None, enabled: bool = True) -> Any:
    """包装 cli 捕获每次 tool 调用为 ToolRecord 推 sink。

    enabled=False 直返原 cli(不包代理)。sink=None 时仍包代理但无推送
    (保持调用链一致,仅无事件流)。
    """
    if not enabled:
        return cli
    return _InstrumentedCLI(cli, sink)


__all__ = ["instrument", "ToolSink", "_make_tool_record"]

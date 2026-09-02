"""CodeMaker CLI Serve API 客户端。

通过 HTTP 调用 codemaker serve 的 REST API，实现：
  - 创建会话 (session)
  - 发送消息获取 AI 响应（同步）

基于 codemaker serve 的 OpenAPI 文档：
  POST /api/session                   创建会话
  POST /session/{id}/message          同步发消息，阻塞返回完整 assistant 回复
  GET  /api/session/{id}/message      获取历史消息列表
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

logger = logging.getLogger("aitable.codemaker")

# R: serve/SDK 侧偶发把原始 JS 异常堆栈（AI_NoOutputGeneratedError、
# vercel.ai.error、多行 "at xxx (...)" 调用栈等）原封不动塞进 info.error.message
# 或 HTTP 错误体，若不做归一化会被 resp.error 一路带到前端 thinking/step
# detail 里，把技术堆栈直接展示给终端用户。这里做特征识别 + 归一化，
# 完整原文仍写日志（供排障），只把简短友好提示返回给上层。
_STACK_TRACE_MARKERS = (
    "vercel.ai.error", "ai_nooutputgeneratederror", "no output generated",
    "at flush (", "at finalize (", "cause: undefined",
)


def _sanitize_provider_error(detail: str, *, max_len: int = 200) -> str:
    """把疑似原始堆栈/多行异常文本归一化为简短友好提示。

    普通短错误（如 "timed out"）原样返回；命中堆栈特征或过长/多行的
    文本才替换为通用提示，避免把 JS 调用栈糊给用户。
    """
    text = str(detail or "").strip()
    if not text:
        return "模型调用异常"
    lowered = text.lower()
    is_stacky = ("\n" in text and len(text) > max_len) or any(
        m in lowered for m in _STACK_TRACE_MARKERS)
    if is_stacky:
        return "模型调用异常（服务端未返回有效结果），请重试"
    if len(text) > max_len:
        return text[:max_len] + "…"
    return text

_CODEMAKER_SERVER_URL = os.environ.get("CODEMAKER_SERVER_URL", "http://127.0.0.1:8666")
_CODEMAKER_USERNAME = os.environ.get("CODEMAKER_USERNAME", "")
_CODEMAKER_PASSWORD = os.environ.get("CODEMAKER_PASSWORD", "")
_DEFAULT_MODEL = os.environ.get("CODEMAKER_MODEL", "")
_TIMEOUT = int(os.environ.get("CODEMAKER_API_TIMEOUT", "120"))

# 运行时模型覆盖（前端下拉框切换用）：优先级高于 .env 的 CODEMAKER_MODEL。
# 本工具是本地单进程单用户，用模块级变量即可；None/空表示未覆盖，回退 .env 默认。
_RUNTIME_MODEL: str | None = None

# ── token 级流式 ──
# chat_stream 注入 sink 后，_prompt_once 同步调用期间后台线程订阅 GET /event SSE，
# 按 sessionID 过滤 message.part.delta 实时推送 (field, delta)。None=不启用（零开销）。
_TOKEN_SINK = None  # 签名: (kind: str, delta: str) -> None，kind 为 text/reasoning


def set_token_sink(fn) -> None:
    """注入/清空 token 流式回调。本地单用户，模块级全局即可。"""
    global _TOKEN_SINK
    _TOKEN_SINK = fn


def _sse_token_pump(server_url: str, session_id: str, stop_evt) -> None:
    """后台线程：订阅 serve /event SSE，把目标会话的 token delta 推给 _TOKEN_SINK。

    注意：chunked 响应上设 socket timeout 会导致首次超时后所有后续 read
    永久 OSError（"cannot read from timed out object"），故用阻塞读，
    靠 serve 心跳（~10s）唤醒循环检查 stop_evt；线程 daemon，随进程退出。
    """
    sink = _TOKEN_SINK
    if sink is None:
        return
    try:
        req = _build_request(f"{server_url}/event", method="GET")
        req.add_header("Accept", "text/event-stream")
        resp = urlopen(req)
        while not stop_evt.is_set():
            line = resp.readline()
            if not line:
                break
            if not line.startswith(b"data: "):
                continue
            try:
                evt = json.loads(line[6:])
            except Exception:
                continue
            if evt.get("type") != "message.part.delta":
                continue
            props = evt.get("properties") or {}
            if props.get("sessionID") != session_id:
                continue
            delta = props.get("delta") or ""
            if delta:
                try:
                    sink(str(props.get("field") or "text"), delta)
                except Exception:
                    pass
    except Exception:
        pass


def get_effective_model() -> str:
    """返回当前生效的运行时覆盖模型（provider/modelID 格式），未设置返回空串。"""
    return _RUNTIME_MODEL or ""


def set_runtime_model(model: str | None) -> str:
    """设置运行时模型覆盖。

    传入 "provider/modelID"（如 netease-codemaker/deepseek-v4-flash）即生效；
    传 None/空串则清除覆盖，回退 .env 的 CODEMAKER_MODEL。返回当前生效值。
    """
    global _RUNTIME_MODEL
    _RUNTIME_MODEL = (model or "").strip() or None
    return get_effective_model()


def persist_model_to_env(model: str) -> bool:
    """把选中模型写回项目根 .env 的 CODEMAKER_MODEL 行（持久化）。

    存在该行则原位替换，否则追加到文件末尾；保留其余行与注释不动。
    写回后下次经 tools/start.bat 启动（自动加载 .env）即生效。
    返回是否写入成功。
    """
    env_path = Path(__file__).resolve().parent.parent.parent / ".env"
    if not env_path.exists():
        return False
    try:
        lines = env_path.read_text(encoding="utf-8").splitlines()
        out: list[str] = []
        replaced = False
        for line in lines:
            if line.strip().startswith("CODEMAKER_MODEL="):
                out.append(f"CODEMAKER_MODEL={model}")
                replaced = True
            else:
                out.append(line)
        if not replaced:
            out.append(f"CODEMAKER_MODEL={model}")
        env_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        logger.info("已写回 .env: CODEMAKER_MODEL=%s", model)
        return True
    except Exception as e:
        logger.warning("写回 .env 失败: %s", e)
        return False


# 默认 prompt 调用：单次尝试上限 + 最大尝试次数。
# 单次挂起时快速失败并重试，避免一次抖动耗尽上层（测试 120s / 预览串联调用）预算。
# 现网正常调用多在 15~45s 内完成（见 run_table_tests 报告）；回归实测本模型单次
# 常需 >30s，故上限取 45s（30s 会必超时误杀），兼顾快速失败与不误杀慢调用。
_PROMPT_ATTEMPT_TIMEOUT = int(os.environ.get("CODEMAKER_PROMPT_ATTEMPT_TIMEOUT", "45"))
_PROMPT_MAX_ATTEMPTS = int(os.environ.get("CODEMAKER_PROMPT_MAX_ATTEMPTS", "2"))

# 每阶段独立超时（秒）：parse/plan/validate/execute/verify，超时→降级规则路径
_STAGE_TIMEOUTS = {
    "parse":    int(os.environ.get("CODEMAKER_PARSE_TIMEOUT", "150")),
    "plan":     int(os.environ.get("CODEMAKER_PLAN_TIMEOUT", "30")),
    "validate": int(os.environ.get("CODEMAKER_VALIDATE_TIMEOUT", "30")),
    "execute":  int(os.environ.get("CODEMAKER_EXECUTE_TIMEOUT", "30")),
    "verify":   int(os.environ.get("CODEMAKER_VERIFY_TIMEOUT", "20")),
}

def get_stage_timeout(stage: str) -> int:
    """返回指定阶段的超时秒数，未知 stage 回退默认 45s。"""
    return _STAGE_TIMEOUTS.get(stage, _PROMPT_ATTEMPT_TIMEOUT)


def _basic_auth_header() -> str | None:
    import base64
    if _CODEMAKER_USERNAME and _CODEMAKER_PASSWORD:
        creds = base64.b64encode(f"{_CODEMAKER_USERNAME}:{_CODEMAKER_PASSWORD}".encode()).decode()
        return f"Basic {creds}"
    return None


def _build_request(url: str, method: str = "GET", body: dict | None = None) -> Request:
    """构建 HTTP 请求，自动附加 Basic Auth 头。"""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    auth = _basic_auth_header()
    if auth:
        headers["Authorization"] = auth

    data = json.dumps(body).encode("utf-8") if body else None
    return Request(url, data=data, headers=headers, method=method)


@dataclass
class SessionCreateResult:
    """创建 codemaker 会话的结果。"""

    ok: bool
    session_id: str = ""
    message_id: str = ""
    error: str = ""
    error_type: str = ""  # 见 CodemakerError.*


@dataclass
class PromptResult:
    """发送 prompt 的结果。"""

    ok: bool
    session_id: str = ""
    message_id: str = ""
    response_text: str = ""
    error: str = ""
    error_type: str = ""  # 见 CodemakerError.*，便于上层映射具体提示
    raw_events: list[dict] = field(default_factory=list)


class CodemakerError:
    """codemaker 调用错误类型分类，供上层映射具体中文提示。

    值与 PromptResult.error_type 对应，agent_service 据 error_type 走分支。
    """

    SERVE_DOWN = "serve_down"          # codemaker serve 不可用 / 连接失败
    AUTH_FAILED = "auth_failed"        # 401/403 鉴权失败（serve 侧）
    PROVIDER_ERROR = "provider_error"  # serve 转发到底层 LLM provider 失败（余额不足/限流/模型不存在）
    TIMEOUT = "timeout"               # 等待响应超时
    BAD_REQUEST = "bad_request"       # 400 请求格式错误（如 model 字段非法）
    CANCELLED = "cancelled"           # 调用方取消（cancel_event 触发，非错误）
    UNKNOWN = "unknown"               # 其它未分类错误


@dataclass
class CodemakerClientConfig:
    """codemaker serve 连接配置。"""

    server_url: str = _CODEMAKER_SERVER_URL
    username: str = _CODEMAKER_USERNAME
    password: str = _CODEMAKER_PASSWORD
    default_model: str = _DEFAULT_MODEL
    timeout: int = _TIMEOUT


class CodemakerClient:
    """CodeMaker Serve API 客户端。

    Usage:
        client = CodemakerClient()
        session = client.create_session(directory="C:/project")
        result = client.prompt(session.session_id, "查询饕餮的攻击力")
        print(result.response_text)
    """

    def __init__(self, config: CodemakerClientConfig | None = None):
        self.cfg = config or CodemakerClientConfig()

    # ── 会话管理 ──

    @staticmethod
    def _to_model_ref(model: Any) -> dict | None:
        """把模型标识规整为 serve 创建会话要求的 Model.Ref 对象。

        serve 的 /api/session 校验 model 为 `Model.Ref | null`（对象），
        直接传字符串 "netease-codemaker/deepseek-v4-flash" 会 400。
        约定：字符串按首个 "/" 拆成 providerID/id。
        （注意：真正生成回复的 /session/{id}/message 用 modelID，见 _to_message_model_ref）

          - 空值        → None（不传，serve 用其默认模型）
          - dict        → 原样返回（已是 Model.Ref）
          - "a/b/c"     → {"providerID": "a", "id": "b/c"}
          - "b"（无 /） → None（无法确定 provider，退回默认）
        """
        if not model:
            return None
        if isinstance(model, dict):
            return model
        text = str(model).strip()
        if not text or "/" not in text:
            return None
        provider, model_id = text.split("/", 1)
        provider, model_id = provider.strip(), model_id.strip()
        if not provider or not model_id:
            return None
        return {"providerID": provider, "id": model_id}

    def health_check(self) -> bool:
        """探测 codemaker serve 是否可用（GET /api/health）。

        用于主智能体判断 codemaker 是否可调用。
        超时 5s，任何异常均视为不可用。
        """
        url = f"{self.cfg.server_url}/api/health"
        try:
            req = _build_request(url, method="GET")
            resp = urlopen(req, timeout=5)
            return getattr(resp, "status", 200) == 200
        except Exception:
            return False

    def create_session(self, directory: str = "", model: str = "") -> SessionCreateResult:
        """创建新 codemaker 会话。

        Args:
            directory: 工作目录（项目路径）。codemaker 会在此目录上下文中执行。
            model: 使用的模型，空则用默认模型。

        Returns:
            SessionCreateResult，包含 session_id 和初始 message_id。
        """
        url = f"{self.cfg.server_url}/api/session"
        body = {}
        if directory:
            body["directory"] = directory
        # 运行时覆盖优先于调用方传入的 model 与 .env 默认值。
        effective_model = get_effective_model() or model or self.cfg.default_model
        model_ref = self._to_model_ref(effective_model)
        if model_ref is not None:
            body["model"] = model_ref

        try:
            req = _build_request(url, method="POST", body=body)
            resp = urlopen(req, timeout=self.cfg.timeout)
            data = json.loads(resp.read().decode("utf-8"))

            session_id = ""
            message_id = ""
            if isinstance(data, dict):
                inner = data.get("data", data) if isinstance(data.get("data"), dict) else data
                session_id = str(inner.get("id", data.get("sessionID", "")))
                message_id = str(inner.get("messageID", ""))

            if session_id:
                return SessionCreateResult(ok=True, session_id=session_id, message_id=message_id)
            else:
                return SessionCreateResult(
                    ok=False,
                    error=f"无法从响应中提取 session_id: {json.dumps(data, ensure_ascii=False)[:300]}",
                )
        except HTTPError as e:
            # HTTPError 是 URLError 子类，必须先于 URLError 捕获，
            # 否则真实的 400/401/... 响应体会被"无法连接"的模糊提示掩盖。
            if e.code in (401, 403):
                et = CodemakerError.AUTH_FAILED
            elif e.code == 400:
                et = CodemakerError.BAD_REQUEST
            else:
                et = CodemakerError.UNKNOWN
            return SessionCreateResult(ok=False, error=self._http_error_detail(e), error_type=et)
        except URLError as e:
            return SessionCreateResult(
                ok=False,
                error=f"无法连接到 codemaker serve ({url}): {e}",
                error_type=CodemakerError.SERVE_DOWN)
        except Exception as e:
            return SessionCreateResult(ok=False, error=str(e),
                                       error_type=CodemakerError.UNKNOWN)

    def prompt(self, session_id: str, message: str, timeout: int | None = None,
               model: str = "", stage: str = "",
               cancel_event=None) -> PromptResult:
        """向 codemaker session 发送消息并**同步**返回 AI 回复。

        走 codemaker 原生同步端点：POST /session/{id}/message，请求体带
        parts 文本块与 model={providerID, modelID}，服务器阻塞直到生成完成后
        一次性返回整条 assistant 消息（含 reasoning / text / tool 等 part）。

        ⚠ 历史坑：旧实现用异步 POST /api/session/{id}/prompt + 轮询 /message，
          对 netease-codemaker 只入队（delivery=steer）不产出回复，永远空轮询
          直到超时。改用同步端点后可稳定拿到回复。

        Args:
            session_id: 会话 ID（以 ses 开头）
            message: 用户消息文本
            timeout: 本次等待上限（秒）。显式传值 → 单次不重试；
                     None → 用 _PROMPT_ATTEMPT_TIMEOUT 并按 _PROMPT_MAX_ATTEMPTS 重试。
            model: 模型 "provider/model-id"，空则用 cfg.default_model。
            stage: 阶段名（parse/plan/validate/execute/verify），用于选阶段超时。
                   仅当 timeout 未显式传入时生效。
            cancel_event: 可选 threading.Event。传入后单次 LLM 调用期间轮询，
                   set 即尽快返回 error_type=CANCELLED（urlopen 子线程 daemon
                   自生自灭，socket 阻塞无法强 kill，本地单会话可接受泄漏）。
                   None → 行为不变（不可中断）。

        Returns:
            PromptResult，包含 AI 响应文本。
        """
        if timeout is not None:
            return self._prompt_once(session_id, message, timeout, model,
                                     cancel_event=cancel_event)
        # stage 超时优先于默认 prompt 超时，单次不重试（阶段快速失败→降级规则路径）
        if stage and stage in _STAGE_TIMEOUTS:
            return self._prompt_once(session_id, message, _STAGE_TIMEOUTS[stage], model,
                                     cancel_event=cancel_event)
        last: PromptResult | None = None
        for attempt in range(1, _PROMPT_MAX_ATTEMPTS + 1):
            r = self._prompt_once(session_id, message, _PROMPT_ATTEMPT_TIMEOUT, model,
                                  cancel_event=cancel_event)
            # 用户取消：立即返回，不再重试
            if r.error_type == CodemakerError.CANCELLED:
                return r
            if r.ok:
                return r
            # provider 侧错误（余额不足/鉴权/模型不存在）重试无意义，立即返回
            if r.error_type == CodemakerError.PROVIDER_ERROR:
                return r
            last = r
            logger.warning("prompt 第%d/%d次失败：%s", attempt, _PROMPT_MAX_ATTEMPTS, r.error)
        return last or PromptResult(ok=False, session_id=session_id, error="prompt 未执行")

    def _prompt_once(self, session_id: str, message: str, attempt_timeout: int,
                     model: str = "", cancel_event=None) -> PromptResult:
        """单次同步调用 POST /session/{id}/message，带耗时与结果日志。"""
        t0 = time.time()
        # 运行时覆盖优先于调用方传入的 model 与 .env 默认值，使前端下拉框即时切换生效。
        effective_model = get_effective_model() or model or self.cfg.default_model
        model_ref = self._to_message_model_ref(effective_model)
        body: dict = {"parts": [{"type": "text", "text": message}]}
        if model_ref is not None:
            body["model"] = model_ref

        url = f"{self.cfg.server_url}/session/{session_id}/message"
        logger.info("→ 调用 codemaker: POST /session/%s/message model=%s msglen=%d",
                    session_id, (effective_model or "默认"), len(message))

        # token 流式：sink 已注入时，同步阻塞期间后台订阅 /event SSE 推 delta
        stop_evt = None
        if _TOKEN_SINK is not None:
            import threading as _th
            stop_evt = _th.Event()
            _th.Thread(target=_sse_token_pump,
                       args=(self.cfg.server_url, session_id, stop_evt),
                       daemon=True).start()
        try:
            return self._prompt_once_inner(session_id, url, body, attempt_timeout, t0,
                                           cancel_event=cancel_event)
        finally:
            if stop_evt is not None:
                stop_evt.set()

    def _prompt_once_inner(self, session_id: str, url: str, body: dict,
                           attempt_timeout: int, t0: float,
                           cancel_event=None) -> PromptResult:
        """同步 POST /session/{id}/message 的实际请求与结果解析。

        cancel_event 非 None 时，把阻塞的 urlopen+read 放进 daemon 子线程，
        主线程轮询 cancel_event（每 0.2s）；set 即返回 CANCELLED。子线程因
        socket 阻塞无法被 kill，随进程退出回收（本地单会话可接受）。
        """

        def _do_request():
            """子线程：阻塞执行 HTTP 请求（urlopen + read 均在此），结果写入 holder[0]。"""
            try:
                req = _build_request(url, method="POST", body=body)
                resp = urlopen(req, timeout=attempt_timeout)
                raw = resp.read()  # 读 body 也在子线程，使 urlopen+read 全受取消轮询覆盖
                holder[0] = ("ok", raw)
            except HTTPError as e:
                holder[0] = ("http", e)
            except URLError as e:
                holder[0] = ("url", e)
            except Exception as e:  # noqa: BLE001
                holder[0] = ("exc", e)

        holder: list = [None]
        if cancel_event is None:
            _do_request()
        else:
            # 先快速检查（避免无谓起线程）
            if cancel_event.is_set():
                return PromptResult(ok=False, session_id=session_id,
                                    error_type=CodemakerError.CANCELLED,
                                    error="用户取消（调用前已 set）")
            import threading as _th
            sub = _th.Thread(target=_do_request, daemon=True)
            sub.start()
            while not cancel_event.is_set():
                sub.join(timeout=0.2)
                if holder[0] is not None:
                    break
            else:
                # 取消：子线程仍在阻塞 urlopen/read，daemon 随进程退出
                return PromptResult(ok=False, session_id=session_id,
                                    error_type=CodemakerError.CANCELLED,
                                    error="用户取消（LLM 调用进行中）")

        kind, payload = holder[0]
        if kind == "http":
            e = payload
            if e.code in (401, 403):
                et = CodemakerError.AUTH_FAILED
            elif e.code == 400:
                et = CodemakerError.BAD_REQUEST
            else:
                et = CodemakerError.UNKNOWN
            return PromptResult(ok=False, session_id=session_id,
                                error_type=et, error=self._http_error_detail(e))
        if kind == "url":
            e = payload
            return PromptResult(ok=False, session_id=session_id,
                                error_type=CodemakerError.SERVE_DOWN,
                                error=f"codemaker serve 连接失败: {e}")
        if kind == "exc":
            e = payload
            return PromptResult(ok=False, session_id=session_id,
                                error_type=CodemakerError.UNKNOWN, error=str(e))

        # kind == "ok"：payload 已是 read() 后的字节
        try:
            data = json.loads(payload.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return PromptResult(ok=False, session_id=session_id,
                                error_type=CodemakerError.UNKNOWN, error=str(e))

        elapsed = time.time() - t0
        info = data.get("info", {}) if isinstance(data, dict) else {}
        if info.get("finish") == "error" or info.get("error"):
            err = info.get("error") or {}
            detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            logger.warning("← codemaker provider 错误 sid=%s elapsed=%.1fs err=%s",
                           session_id, elapsed, detail)
            return PromptResult(ok=False, session_id=session_id,
                                error_type=CodemakerError.PROVIDER_ERROR,
                                error=f"底层 LLM 调用失败：{_sanitize_provider_error(detail)}")

        response_text = self._extract_message_text(data)
        if not response_text:
            return PromptResult(ok=False, session_id=session_id,
                                error_type=CodemakerError.PROVIDER_ERROR,
                                error="codemaker 回复为空（无 text part）")
        logger.info("← codemaker 回复 sid=%s elapsed=%.1fs resplen=%d",
                    session_id, elapsed, len(response_text))
        return PromptResult(ok=True, session_id=session_id, response_text=response_text)

    @staticmethod
    def _to_message_model_ref(model: Any) -> dict | None:
        """规整为同步 message 接口要求的 model 对象 {providerID, modelID}。

        注意与 /api/session 的 Model.Ref（用 id）不同，这里键名是 **modelID**。
          - 空 / 无 "/"  → None（serve 回退默认模型）
          - dict         → 原样返回
          - "a/b/c"      → {"providerID": "a", "modelID": "b/c"}
        """
        if not model:
            return None
        if isinstance(model, dict):
            return model
        text = str(model).strip()
        if not text or "/" not in text:
            return None
        provider, model_id = text.split("/", 1)
        provider, model_id = provider.strip(), model_id.strip()
        if not provider or not model_id:
            return None
        return {"providerID": provider, "modelID": model_id}

    @staticmethod
    def _extract_message_text(data: dict) -> str:
        """从同步 message 响应的 parts 数组中拼出纯文本回复。

        响应结构：{"info": {...}, "parts": [{"type":"text","text":...}, ...]}
        只取 type=="text" 的 part（忽略 reasoning / step-start / tool 等）。
        """
        parts = data.get("parts", []) if isinstance(data, dict) else []
        texts = [
            p.get("text", "")
            for p in parts
            if isinstance(p, dict) and p.get("type") == "text" and p.get("text")
        ]
        return "\n".join(texts)

    @staticmethod
    def _http_error_detail(e: "HTTPError") -> str:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        logger.warning("← codemaker HTTP 错误 code=%s detail=%s", e.code, detail or str(e))
        return f"codemaker serve 返回 {e.code}: {_sanitize_provider_error(detail or str(e))}"

    def get_messages(self, session_id: str) -> list[dict]:
        """获取会话的所有消息。

        Returns:
            消息列表，每个消息包含 role, content 等字段。
        """
        url = f"{self.cfg.server_url}/api/session/{session_id}/message"
        try:
            req = _build_request(url, method="GET")
            resp = urlopen(req, timeout=30)
            data = json.loads(resp.read().decode("utf-8"))
            if isinstance(data, list):
                return data
            if isinstance(data, dict):
                return data.get("messages", data.get("data", []))
            return []
        except Exception:
            return []

    # ── 工具方法 ──

    @staticmethod
    def _extract_response_text(data: dict) -> str:
        """从 codemaker 响应中提取 AI 的文本回复。"""
        # 可能的响应结构：
        # {"data": {"text": "..."}}
        # {"content": "..."}
        # {"message": {"content": "..."}}
        # {"data": {"inputAdmitted": ...}}

        if "data" in data and isinstance(data["data"], dict):
            inner = data["data"]
            # SessionInputAdmitted 结构
            if "inputAdmitted" in inner:
                inner = inner["inputAdmitted"]
            # 尝试提取文本
            if "content" in inner:
                return str(inner["content"])
            if "text" in inner:
                return str(inner["text"])
            if "message" in inner:
                msg = inner["message"]
                if isinstance(msg, dict):
                    return CodemakerClient._extract_content(msg)
                return str(msg)

        if "content" in data:
            return str(data["content"])
        if "text" in data:
            return str(data["text"])
        if "message" in data:
            msg = data["message"]
            if isinstance(msg, dict):
                return CodemakerClient._extract_content(msg)
            return str(msg)

        return json.dumps(data, ensure_ascii=False)

    @staticmethod
    def _extract_content(msg: dict) -> str:
        """从消息对象中提取 content 文本。"""
        content = msg.get("content", "")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            texts = []
            for block in content:
                if isinstance(block, dict):
                    if block.get("type") == "text":
                        texts.append(block.get("text", ""))
            return "\n".join(texts)
        return str(content)

    def extract_json_from_response(self, response_text: str) -> Any:
        """从 AI 响应文本中提取 JSON（对象或数组）。

        依次尝试：
          1. 整段直接 json.loads（最理想：纯 JSON 响应）
          2. 栈匹配提取首个完整 JSON 数组或对象（支持嵌套、跳过字符串内的括号）
          3. 修复式解析：中文字符串内混入未转义 ASCII 双引号时修复（serve LLM 常见）
          4. YAML 兼容：serve LLM 偶返 fenced ```yaml 或裸 YAML（无 {/[ 可栈匹配）
        返回 dict / list / None。多指令解析依赖此函数能正确返回数组。
        """
        if not response_text:
            return None

        text = response_text.strip()
        # 1. 直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        # 2. 栈匹配提取首个完整 JSON 数组或对象（按出现位置取最早）
        candidates: list[tuple[int, str]] = []
        for open_ch, close_ch in (("[", "]"), ("{", "}")):
            depth = 0
            start = -1
            in_str = False
            esc = False
            for i, ch in enumerate(response_text):
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                    continue
                if ch == open_ch:
                    if depth == 0:
                        start = i
                    depth += 1
                elif ch == close_ch:
                    if depth > 0:
                        depth -= 1
                        if depth == 0 and start >= 0:
                            candidates.append((start, response_text[start:i + 1]))
                            start = -1
        candidates.sort(key=lambda x: x[0])
        for _, cand in candidates:
            try:
                return json.loads(cand)
            except json.JSONDecodeError:
                continue

        # 3. 修复式解析：中文字符串内混入未转义 ASCII 双引号
        repaired = self._repair_quoted_json(response_text)
        if repaired is not None:
            return repaired

        # 4. YAML 兼容：serve LLM 偶返 fenced ```yaml 或裸 YAML（无 {/[ 可栈匹配）。
        #    先剥 fence，再 yaml.safe_load，成功则返回（dict/list 均可）。
        #    失败静默降级返 None（保持原 None 语义，调用方走空响应处理）。
        yaml_text = response_text
        _fence_match = re.match(
            r"^\s*```(?:ya?ml|json)?\s*\n(.*)\n```\s*$",
            response_text, re.DOTALL)
        if _fence_match:
            yaml_text = _fence_match.group(1)
        try:
            import yaml as _yaml
            _loaded = _yaml.safe_load(yaml_text)
            if isinstance(_loaded, (dict, list)):
                return _loaded
        except Exception:
            pass
        return None

    @staticmethod
    def _repair_quoted_json(response_text: str):
        """修复中文字符串内未转义 ASCII 双引号的 JSON（serve LLM 常见故障）。

        策略：定位最外层 { … }，逐字符扫描串值区域，把「串值内部成对的
        裸 ASCII 双引号」还原为中文引号，再 json.loads。失败返回 None。
        """
        try:
            open_idx = response_text.find("{")
            if open_idx < 0:
                return None
            # 定位匹配的最外层 { }
            depth = 0
            close_idx = -1
            in_str = False
            esc = False
            for i in range(open_idx, len(response_text)):
                ch = response_text[i]
                if in_str:
                    if esc:
                        esc = False
                    elif ch == "\\":
                        esc = True
                    elif ch == '"':
                        in_str = False
                    continue
                if ch == '"':
                    in_str = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        close_idx = i
                        break
            if close_idx < 0:
                return None
            body = response_text[open_idx:close_idx + 1]

            # 逐字符重建：在串值内部遇到裸 "（前后都是非结构字符）→ 换中文引号
            out = []
            i = 0
            n = len(body)
            in_str = False
            # 闭合引号后紧跟的结构字符（真实字符串结束的标志）
            close_struct = (":", ",", "}", "]", " ", "\n", "\t")
            while i < n:
                ch = body[i]
                if in_str:
                    if ch == "\\" and i + 1 < n:
                        out.append(ch)
                        out.append(body[i + 1])
                        i += 2
                        continue
                    if ch == '"':
                        nxt = body[i + 1] if i + 1 < n else ""
                        prev = body[i - 1] if i > 0 else ""
                        # 真实闭合：后面是结构字符（: , } ] 空白/结尾）
                        if nxt in close_struct or nxt == "":
                            in_str = False
                            out.append(ch)
                        else:
                            # 串值内部的裸引号 → 还原为中文引号
                            out.append("\u201c")
                        i += 1
                        continue
                    out.append(ch)
                    i += 1
                    continue
                if ch == '"':
                    in_str = True
                    out.append(ch)
                    i += 1
                    continue
                out.append(ch)
                i += 1
            try:
                return json.loads("".join(out))
            except json.JSONDecodeError:
                return None
        except Exception:
            return None

    def list_models(self) -> list[dict]:
        """列出 codemaker serve 可用模型（GET /api/model）。

        返回原始模型项列表（已从 {data:...}/[...] 等常见包装中提取），
        供端点归一化为前端下拉选项。任何异常返回空列表，端点据此降级展示。
        """
        url = f"{self.cfg.server_url}/api/model"
        try:
            req = _build_request(url, method="GET")
            resp = urlopen(req, timeout=15)
            data = json.loads(resp.read().decode("utf-8"))
        except HTTPError as e:
            if e.code in (401, 403):
                logger.error(
                    "list_models 鉴权失败 (%d)：serve 拒绝 CODEMAKER_USERNAME/PASSWORD。"
                    "检查 .env 凭据是否与 codemaker serve 启动时的 OPENCODE_SERVER_USERNAME/PASSWORD 一致。", e.code)
            else:
                logger.error("list_models 拉取失败: serve 返回 %d", e.code)
            return []
        except URLError as e:
            logger.error("list_models 连接失败：codemaker serve 不可达 (%s)。请确认 tools\\start.bat 已拉起 serve。", e)
            return []
        except Exception as e:
            logger.warning("list_models 拉取失败: %s", e)
            return []
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("data", "models", "items"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
        return []

    def close(self):
        """释放资源（当前无状态，预留接口）。"""
        pass

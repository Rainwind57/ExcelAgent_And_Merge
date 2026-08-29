"""DeepSeek 官方 API 直连客户端（与 CodemakerClient 接口兼容）。

不经过 codemaker serve 转发，直接用 DEEPSEEK_API_KEY 调 DeepSeek 官方
OpenAI 兼容接口（POST {DEEPSEEK_BASE_URL}/chat/completions）。

与 CodemakerClient 保持相同的方法签名（create_session / prompt /
health_check / extract_json_from_response / list_models / get_messages /
close），因此 TableAgent、CodemakerNLParser、StepAIEnhancer、
CodemakerChatModel、QAHandler、OrchestratorAgent 等既有调用方**无需任何改动**。

由 server/run_deepseek.py 在 import main 之前，把 CodemakerClient 的这些
方法替换为本类的方法（monkey-patch），即可让整套前端/agent 走 DeepSeek 直连。

环境变量（在项目根 .env 追加，与 CODEMAKER_* 互不影响）：
    DEEPSEEK_API_KEY=sk-xxx
    DEEPSEEK_BASE_URL=https://api.deepseek.com
    DEEPSEEK_MODEL=deepseek-chat          # deepseek-chat / deepseek-reasoner
    DEEPSEEK_MAX_TOKENS=8192
    DEEPSEEK_THINKING=disabled            # enabled / disabled（仅 deepseek-reasoner 系有效）
    DEEPSEEK_TEMPERATURE=0
    DEEPSEEK_TIMEOUT=120
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from . import codemaker_client as _cm

logger = logging.getLogger("aitable.deepseek")

_DEFAULT_BASE_URL = "https://api.deepseek.com"
_DEFAULT_MODEL = "deepseek-chat"
_DEFAULT_MAX_TOKENS = 8192
_DEFAULT_TIMEOUT = 120


def _base_url() -> str:
    return os.environ.get("DEEPSEEK_BASE_URL", _DEFAULT_BASE_URL).rstrip("/")


def _api_key() -> str:
    return os.environ.get("DEEPSEEK_API_KEY", "").strip()


def _model_id() -> str:
    """解析当前生效模型 id。

    优先级：前端下拉运行时覆盖（仅当 provider 为 deepseek 时采用，"provider/modelID"
    取尾段）→ DEEPSEEK_MODEL 环境变量 → deepseek-chat。
    忽略 codemaker 风格 provider（netease-codemaker/...），因 DeepSeek 直连只认
    deepseek-chat / deepseek-reasoner 等官方 id。
    """
    eff = _cm.get_effective_model()
    if eff:
        provider, _, mid = eff.partition("/")
        if provider.strip().lower() == "deepseek" and mid.strip():
            return mid.strip()
    m = os.environ.get("DEEPSEEK_MODEL", "").strip()
    return m or _DEFAULT_MODEL


def _max_tokens() -> int:
    raw = os.environ.get("DEEPSEEK_MAX_TOKENS", str(_DEFAULT_MAX_TOKENS)).strip()
    try:
        return max(256, int(raw))
    except ValueError:
        return _DEFAULT_MAX_TOKENS


def _temperature() -> float:
    raw = os.environ.get("DEEPSEEK_TEMPERATURE", "0").strip()
    try:
        return float(raw)
    except ValueError:
        return 0.0


def _timeout() -> int:
    raw = os.environ.get("DEEPSEEK_TIMEOUT", str(_DEFAULT_TIMEOUT)).strip()
    try:
        return max(10, int(raw))
    except ValueError:
        return _DEFAULT_TIMEOUT


def _build_request(url: str, method: str = "GET", body: dict | None = None,
                   headers: dict | None = None) -> Request:
    h = {"Content-Type": "application/json", "Accept": "application/json"}
    if headers:
        h.update(headers)
    key = _api_key()
    if key:
        h["Authorization"] = f"Bearer {key}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body else None
    return Request(url, data=data, headers=h, method=method)


class DeepSeekClient:
    """DeepSeek 直连客户端，模拟 CodemakerClient 的对外接口。"""

    # ── 会话管理 ──

    def create_session(self, directory: str = "", model: str = ""):
        """DeepSeek 无服务端会话概念，返回伪 session_id（供调用方缓存/日志）。"""
        key = _api_key()
        if not key:
            return _cm.SessionCreateResult(
                ok=False, error="缺少 DEEPSEEK_API_KEY，请在项目根 .env 中配置后重启",
                error_type=_cm.CodemakerError.AUTH_FAILED)
        return _cm.SessionCreateResult(ok=True, session_id=f"ds-{uuid.uuid4().hex[:16]}")

    def health_check(self) -> bool:
        """DeepSeek 无长驻服务，可达性以实际调用为准；恒返回 True。

        （避免 CodemakerChatModel._generate 因误判 serve 不可用而抛出
        "codemaker serve 不可用" 的错误；真实鉴权/网络错误会在 prompt
        调用时以 auth_failed / provider_error 精确返回。）
        """
        return True

    # ── 消息调用 ──

    def prompt(self, session_id: str, message: str, timeout: int | None = None,
               model: str = "", stage: str = "", cancel_event=None) -> _cm.PromptResult:
        """同步调用 DeepSeek chat/completions 返回整段回复。

        若 _cm 模块级 token sink 已注入（chat_stream 设置 set_token_sink），
        则走 stream=True 并实时推送 text/reasoning delta；否则 stream=False。
        """
        key = _api_key()
        if not key:
            return _cm.PromptResult(
                ok=False, session_id=session_id, error_type=_cm.CodemakerError.AUTH_FAILED,
                error="缺少 DEEPSEEK_API_KEY，请在项目根 .env 中配置后重启")

        model_id = _model_id()
        payload: dict = {
            "model": model_id,
            "messages": [{"role": "user", "content": message}],
            "temperature": _temperature(),
            "max_tokens": _max_tokens(),
        }
        thinking = os.environ.get("DEEPSEEK_THINKING", "").strip().lower()
        if thinking in ("enabled", "disabled"):
            payload["thinking"] = {"type": thinking}

        sink = getattr(_cm, "_TOKEN_SINK", None)
        if sink is not None:
            payload["stream"] = True
            return self._prompt_stream(session_id, payload, model_id, cancel_event, sink)
        payload["stream"] = False
        return self._prompt_once(session_id, payload, model_id, cancel_event)

    def _prompt_once(self, session_id: str, payload: dict, model_id: str,
                     cancel_event=None) -> _cm.PromptResult:
        url = f"{_base_url()}/chat/completions"
        req = _build_request(url, method="POST", body=payload)
        timeout = _timeout()

        def _do_request():
            try:
                resp = urlopen(req, timeout=timeout)
                holder[0] = ("ok", resp.read())
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
            if cancel_event.is_set():
                return _cm.PromptResult(ok=False, session_id=session_id,
                                        error_type=_cm.CodemakerError.CANCELLED,
                                        error="用户取消（调用前已 set）")
            import threading as _th
            sub = _th.Thread(target=_do_request, daemon=True)
            sub.start()
            while not cancel_event.is_set():
                sub.join(timeout=0.2)
                if holder[0] is not None:
                    break
            else:
                return _cm.PromptResult(ok=False, session_id=session_id,
                                        error_type=_cm.CodemakerError.CANCELLED,
                                        error="用户取消（LLM 调用进行中）")

        kind, payload_v = holder[0]
        if kind == "http":
            return self._http_prompt_error(session_id, payload_v)
        if kind == "url":
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.PROVIDER_ERROR,
                                    error=f"DeepSeek 连接失败: {payload_v}")
        if kind == "exc":
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.UNKNOWN,
                                    error=str(payload_v))

        try:
            data = json.loads(payload_v.decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.UNKNOWN, error=str(e))

        choices = data.get("choices") or []
        if not choices:
            err = (data.get("error") or {})
            detail = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.PROVIDER_ERROR,
                                    error=f"DeepSeek 返回无结果：{detail}")
        msg = choices[0].get("message") or {}
        text = msg.get("content") or ""
        if not text:
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.PROVIDER_ERROR,
                                    error="DeepSeek 回复为空（无 content）")
        return _cm.PromptResult(ok=True, session_id=session_id, response_text=text)

    def _prompt_stream(self, session_id: str, payload: dict, model_id: str,
                       cancel_event, sink) -> _cm.PromptResult:
        url = f"{_base_url()}/chat/completions"
        req = _build_request(url, method="POST", body=payload,
                             headers={"Accept": "text/event-stream"})
        try:
            resp = urlopen(req, timeout=_timeout())
        except HTTPError as e:
            return self._http_prompt_error(session_id, e)
        except URLError as e:
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.PROVIDER_ERROR,
                                    error=f"DeepSeek 连接失败: {e}")
        except Exception as e:  # noqa: BLE001
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.UNKNOWN, error=str(e))

        parts: list[str] = []
        try:
            for raw in resp:
                if cancel_event is not None and cancel_event.is_set():
                    return _cm.PromptResult(
                        ok=False, session_id=session_id,
                        error_type=_cm.CodemakerError.CANCELLED,
                        error="用户取消（LLM 调用进行中）",
                        response_text="".join(parts))
                line = raw.decode("utf-8", errors="replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    obj = json.loads(data)
                except Exception:
                    continue
                choices = obj.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                content = delta.get("content")
                reasoning = delta.get("reasoning_content") or delta.get("reasoning")
                if isinstance(content, str) and content:
                    parts.append(content)
                    try:
                        sink("text", content)
                    except Exception:
                        pass
                if isinstance(reasoning, str) and reasoning:
                    try:
                        sink("reasoning", reasoning)
                    except Exception:
                        pass
        except Exception as e:  # noqa: BLE001
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.UNKNOWN, error=str(e))
        text = "".join(parts)
        if not text:
            return _cm.PromptResult(ok=False, session_id=session_id,
                                    error_type=_cm.CodemakerError.PROVIDER_ERROR,
                                    error="DeepSeek 回复为空（流式无 content）")
        return _cm.PromptResult(ok=True, session_id=session_id, response_text=text)

    def _http_prompt_error(self, session_id: str, e: HTTPError) -> _cm.PromptResult:
        try:
            detail = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            detail = ""
        if e.code in (401, 403):
            et = _cm.CodemakerError.AUTH_FAILED
            detail = f"DeepSeek API Key 无效或无权限：{detail}"
        elif e.code == 400:
            et = _cm.CodemakerError.BAD_REQUEST
        elif e.code == 429:
            et = _cm.CodemakerError.PROVIDER_ERROR
            detail = f"DeepSeek 限流（429）：{detail}"
        elif 500 <= e.code < 600:
            et = _cm.CodemakerError.PROVIDER_ERROR
        else:
            et = _cm.CodemakerError.UNKNOWN
        return _cm.PromptResult(ok=False, session_id=session_id, error_type=et,
                                error=f"DeepSeek 返回 {e.code}: {detail or str(e)}")

    # ── JSON 提取 ──

    @staticmethod
    def extract_json_from_response(response_text: str) -> Any:
        """从 LLM 响应文本中提取 JSON（对象或数组），失败返回 None。

        与 CodemakerClient.extract_json_from_response 行为一致：
        整段解析 → 栈匹配首个完整 {} / [] → 剥 fence 后 YAML 兜底。
        """
        if not response_text:
            return None
        text = response_text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

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

        yaml_text = response_text
        _fence = re.match(r"^\s*```(?:ya?ml|json)?\s*\n(.*)\n```\s*$",
                          response_text, re.DOTALL)
        if _fence:
            yaml_text = _fence.group(1)
        try:
            import yaml as _yaml
            loaded = _yaml.safe_load(yaml_text)
            if isinstance(loaded, (dict, list)):
                return loaded
        except Exception:
            pass
        return None

    # ── 模型列表 ──

    def list_models(self) -> list[dict]:
        """返回 DeepSeek 可用模型（扁平形态，供 /api/agent/models 归一化）。

        优先拉取 GET {base}/models 真实清单，失败回退到 DEEPSEEK_MODEL +
        deepseek-chat/deepseek-reasoner。
        """
        out: list[dict] = []
        try:
            req = _build_request(f"{_base_url()}/models", method="GET")
            resp = urlopen(req, timeout=15)
            data = json.loads(resp.read().decode("utf-8"))
            items = data.get("data", data) if isinstance(data, dict) else data
            if isinstance(items, list):
                for it in items:
                    if isinstance(it, dict) and it.get("id"):
                        out.append({"providerID": "deepseek",
                                    "id": str(it["id"]),
                                    "name": str(it.get("id"))})
        except Exception as e:
            logger.warning("DeepSeek list_models 拉取失败：%s", e)

        seen = {o["id"] for o in out}
        defaults = [os.environ.get("DEEPSEEK_MODEL", "").strip(),
                    "deepseek-chat", "deepseek-reasoner"]
        for mid in defaults:
            if mid and mid not in seen:
                out.append({"providerID": "deepseek", "id": mid, "name": mid})
                seen.add(mid)
        return out

    def get_messages(self, session_id: str) -> list[dict]:
        """DeepSeek 直连为无状态调用，历史由上层 LangGraph/会话管理维护。"""
        return []

    def close(self):
        pass

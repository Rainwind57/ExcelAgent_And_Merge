#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Step1 全流程 trace：一条输入跑完 Step1ParseSubAgent.execute，逐阶段打印中间产物。

设计目标：一个输入 → 看到 Step1 每个过程的输入/输出，用于定位"漏意图/错路由/字段缺失"
发生在哪一环，而不是只看最终 NLIntent[] 猜。

阶段划分：
  STAGE 0  环境与输入
  STAGE 1  LLM 路由分类        LocatorAgent._llm_classify_route
  STAGE 2  LLM 分段+指代消解   ParseAgent._ai_plan_segments
  STAGE 3  每段粗路由定位      LocatorAgent.locate（候选表/FK 边/列名信号）
  STAGE 4  每段 LLM 拆解       DecomposeAgent.decompose_segment
            4a 段内候选裁剪    _prune_segment_candidates
            4b 单 prompt/并发  _decompose_single_prompt / _decompose_parallel
            4c 段产 SplitIntent
  STAGE 5  组装尾部            ParseAgent._assemble
            5a 全局 backfill + 表覆盖自检
            5b SplitIntent → NLIntent
            5c 去重/空壳丢弃/灌值守卫
            5d 引用编译 _compile_step1_references
            5e 字段自检补漏 _llm_complete_fields
  STAGE 6  Step1 SubAgent 后处理 plan_graph / quality / semantic_plan / audit / errors
  STAGE 7  最终 NLIntent[]
  STAGE 8  LLM 调用统计

用法（仓库根）：
    python server/tests/trace_step1_full.py                    # 默认 cases/planner_style_inputs.json[5]
    python server/tests/trace_step1_full.py --case 0
    python server/tests/trace_step1_full.py --text "新增灵兽叫朱雀，model_id 1020"
    python server/tests/trace_step1_full.py --case-file downloads/task_chain.json --case 2
    python server/tests/trace_step1_full.py --backend serve    # 走 codemaker serve 而非 DeepSeek 直连

输出：控制台 + server/tests/reports/step1_trace_latest.txt；
     LLM 完整 prompt/响应落盘 server/tests/reports/step1_trace_dump/。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_TESTS_DIR = Path(__file__).resolve().parent
_SERVER_DIR = _TESTS_DIR.parent
_ROOT = _SERVER_DIR.parent
_REPORTS = _TESTS_DIR / "reports"
_DEFAULT_CASES = _TESTS_DIR / "cases" / "planner_style_inputs.json"


def _load_dotenv(env_path: Path) -> None:
    """极简 .env 加载（必须早于 import agent，凭据/开关在模块级固化）。"""
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and k not in os.environ:
            os.environ[k] = v


_load_dotenv(_ROOT / ".env")

# 观测开关必须在 import agent 之前（decompose_agent 模块级只读一次）
os.environ.setdefault("CODEMAKER_DECOMPOSE_TRACE", "1")
os.environ.setdefault("CODEMAKER_DECOMPOSE_DUMP_DIR",
                      str(_REPORTS / "step1_trace_dump"))

if str(_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_SERVER_DIR))

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass


# ── 输出（线程安全 + 可落盘）──────────────────────────────────

class Trace:
    def __init__(self, fh=None, quiet: bool = False):
        self._lock = threading.Lock()
        self._fh = fh
        self.quiet = quiet      # 批量模式：只统计不打印（LLM 详情会刷屏）
        self.t0 = time.time()
        self.llm: list[dict] = []

    def emit(self, line: str = "") -> None:
        with self._lock:
            if not self.quiet:
                print(line, flush=True)
            if self._fh:
                try:
                    self._fh.write(line + "\n")
                    self._fh.flush()
                except Exception:
                    pass

    def banner(self, title: str) -> None:
        self.emit()
        self.emit("=" * 78)
        self.emit(f"== {title}")
        self.emit("=" * 78)

    def kv(self, k: str, v: Any) -> None:
        self.emit(f"  {k:<16}= {v}")


def _clipv(v: Any, n: int = 46) -> str:
    s = "" if v is None else str(v)
    s = s.replace("\n", "\\n")
    return s if len(s) <= n else s[:n] + "…"


def _fmt_fields(fields: Any, limit: int = 16) -> str:
    if not isinstance(fields, dict) or not fields:
        return "{}"
    items = list(fields.items())
    s = ", ".join(f"{k}={_clipv(v)}" for k, v in items[:limit])
    if len(items) > limit:
        s += f", …(+{len(items) - limit - 1})"
    return "{" + s + "}"


def _j(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)
    except Exception:
        return repr(obj)


def _si_brief(si: Any) -> str:
    """SplitIntent 一行摘要。"""
    return (f"{getattr(si, 'action', '?')} "
            f"{getattr(si, 'table_hint', '?')}/{getattr(si, 'sheet_hint', '') or '-'}")


def _dump_si(trace: Trace, intents: list, title: str) -> None:
    trace.emit(f"  {title}: {len(intents)} 条")
    for i, si in enumerate(intents or []):
        trace.emit(f"    [{i}] {_si_brief(si)}")
        trace.emit(f"        fields  = {_fmt_fields(getattr(si, 'fields', None))}")
        trace.emit(f"        produces= {getattr(si, 'produces', None)!r} "
                   f"consumes= {getattr(si, 'consumes', None)!r}")
        trace.emit(f"        raw     = {_clipv(getattr(si, 'raw', ''), 90)}")


def _dump_nl(trace: Trace, intents: list) -> None:
    trace.emit(f"  最终 NLIntent: {len(intents)} 条")
    for i, it in enumerate(intents or []):
        extras = getattr(it, "extras", None) or {}
        trace.emit(f"    [{i}] action={getattr(it, 'action', '?')!r} "
                   f"table={getattr(it, 'table_hint', '?')!r} "
                   f"sheet={getattr(it, 'sheet_hint', '') or '-'!r}")
        trace.emit(f"        locator  = {getattr(it, 'locator_field', None)!r}"
                   f"={_clipv(getattr(it, 'locator_value', None), 30)}")
        trace.emit(f"        produces = {getattr(it, 'produces_label', None)!r} "
                   f"consumes= {list(getattr(it, 'consumes_labels', None) or [])}")
        trace.emit(f"        fields   = {_fmt_fields(extras.get('fields'))}")
        trace.emit(f"        raw      = {_clipv(getattr(it, 'raw', ''), 90)}")


def _dump_candidates(trace: Trace, lr: Any) -> None:
    cands = getattr(lr, "candidates", None) or []
    trace.emit(f"  candidates ({len(cands)}):")
    for i, c in enumerate(cands):
        trace.emit(f"    #{i} stem={getattr(c, 'stem', '?')!r} "
                   f"sheet={(getattr(c, 'sheet', '') or '-')!r} "
                   f"conf={round(getattr(c, 'confidence', 0), 3)} "
                   f"level={getattr(c, 'level', '')!r} "
                   f"term={getattr(c, 'matched_term', '')!r}")
    edges = getattr(lr, "fk_edges", None) or []
    trace.emit(f"  fk_edges ({len(edges)}):")
    for e in edges:
        trace.emit(f"    {getattr(e, 'from_stem', '')}/{getattr(e, 'from_sheet', '')}"
                   f".{getattr(e, 'from_column', '')} -> "
                   f"{getattr(e, 'to_stem', '')}/{getattr(e, 'to_sheet', '')}"
                   f".{getattr(e, 'to_column', '')}")
    sig = getattr(lr, "column_signal", None)
    if sig is not None:
        trace.emit(f"  column_signal: has_signal={getattr(sig, 'has_signal', None)} "
                   f"terms={getattr(sig, 'extracted_terms', None)} "
                   f"stems={getattr(sig, 'candidate_stems', None)}")


# ── DeepSeek 直连 client（带 trace）───────────────────────────

@dataclass
class _SessionResult:
    ok: bool
    session_id: str = "step1-trace"
    error: str = ""


@dataclass
class _PromptResult:
    ok: bool
    response_text: str = ""
    error: str = ""
    error_type: str = ""


class _TracedClient:
    """统一 client：DeepSeek 直连 或 包装 CodemakerClient，记录每次 LLM IO。"""

    def __init__(self, trace: Trace, inner: Any, dump_dir: Path):
        self._trace = trace
        self._inner = inner
        self._dump_dir = dump_dir
        self._n = 0

    def __getattr__(self, item):
        return getattr(self._inner, item)

    def create_session(self, *a, **kw):
        # 调用方有位置传参（parse_agent._ai_plan_segments: create(directory, model)），
        # 必须透传 *a，否则 TypeError 会被上层 logger.debug 静默吞掉 → 分段层假死。
        return self._inner.create_session(*a, **kw)

    def extract_json_from_response(self, text: str) -> Any:
        return self._inner.extract_json_from_response(text)

    def prompt(self, sid: str, prompt: str, timeout: int = 90,
               model: str = "", **kw):
        self._n += 1
        idx = self._n
        t0 = time.time()
        self._trace.emit()
        self._trace.emit(f">>> [LLM #{idx}] timeout={timeout}s "
                         f"prompt={len(prompt or '')}B")
        self._trace.emit("    prompt head: " + (prompt or "")[:200].replace("\n", " ⏎ "))
        try:
            r = self._inner.prompt(sid, prompt, timeout=timeout, model=model, **kw)
        except Exception as e:
            dt = time.time() - t0
            self._trace.emit(f"<<< [LLM #{idx}] EXCEPTION {dt:.1f}s "
                             f"{type(e).__name__}: {e}")
            self._trace.llm.append({"idx": idx, "dur": dt, "prompt": len(prompt or ""),
                                    "resp": 0, "ok": False})
            raise
        dt = time.time() - t0
        txt = getattr(r, "response_text", "") or ""
        self._trace.llm.append({"idx": idx, "dur": dt, "prompt": len(prompt or ""),
                                "resp": len(txt), "ok": bool(getattr(r, "ok", False))})
        self._trace.emit(f"<<< [LLM #{idx}] {dt:.1f}s ok={getattr(r, 'ok', None)} "
                         f"resp={len(txt)}B err={_clipv(getattr(r, 'error', ''), 120)}")
        self._trace.emit("    resp head: " + txt[:500].replace("\n", " ⏎ "))
        try:
            self._dump_dir.mkdir(parents=True, exist_ok=True)
            (self._dump_dir / f"llm_{idx:03d}.prompt.txt").write_text(
                prompt or "", encoding="utf-8")
            (self._dump_dir / f"llm_{idx:03d}.resp.txt").write_text(txt, encoding="utf-8")
        except Exception:
            pass
        return r


class _DeepSeekRaw:
    def __init__(self, *, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    def create_session(self, *a, **kw) -> _SessionResult:
        # 产品代码以位置参数调用 create(directory, model)（parse_agent.py:280），
        # 签名必须兼容，否则 TypeError 被 logger.debug 吞掉 → 分段层静默失效。
        return _SessionResult(ok=True)

    def extract_json_from_response(self, text: str) -> Any:
        cleaned = (text or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned)
            cleaned = re.sub(r"\s*```$", "", cleaned).strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass
        start = min([i for i in [cleaned.find("["), cleaned.find("{")] if i >= 0]
                    or [-1])
        if start >= 0:
            for end in range(len(cleaned), start, -1):
                try:
                    return json.loads(cleaned[start:end].strip())
                except json.JSONDecodeError:
                    continue
        return []

    def prompt(self, _sid: str, prompt: str, timeout: int = 120,
               model: str = "", **_kw) -> _PromptResult:
        payload = {
            "model": model or self.model,
            "temperature": 0,
            "stream": False,
            "max_tokens": int(os.environ.get("DEEPSEEK_MAX_TOKENS", "8192")),
            "messages": [
                {"role": "system",
                 "content": "You are an Excel config decomposition engine. "
                            "Return only JSON. Do not add markdown."},
                {"role": "user", "content": prompt},
            ],
        }
        if os.environ.get("DEEPSEEK_THINKING", "disabled").strip().lower() in (
                "enabled", "disabled"):
            payload["thinking"] = {"type": os.environ.get(
                "DEEPSEEK_THINKING", "disabled").strip().lower()}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return _PromptResult(ok=True,
                                 response_text=data["choices"][0]["message"]["content"])
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            return _PromptResult(ok=False, error=detail, error_type=f"http_{exc.code}")
        except Exception as exc:  # noqa: BLE001
            return _PromptResult(ok=False, error=str(exc),
                                 error_type=type(exc).__name__)


# ── hook 安装 ────────────────────────────────────────────────

def _wrap(trace: Trace, cls: Any, name: str, *,
          pre=None, post=None) -> None:
    """包装方法并打印 in/out。兼容 instance / static / class method。

    注意：被包装对象的调用约定必须保持原样，否则内部调用点会因签名变化
    崩溃（踩过：_dedupe_nl_intents 是 staticmethod，强行加 self 会 TypeError）。
    """
    import inspect
    kind = "inst"
    try:
        raw = inspect.getattr_static(cls, name)
        if isinstance(raw, staticmethod):
            kind = "static"
        elif isinstance(raw, classmethod):
            kind = "class"
    except Exception:
        pass
    orig = getattr(cls, name)

    def _run(recv, a, kw):
        if pre is not None:
            try:
                pre(trace, recv, a, kw)
            except Exception:
                pass
        t0 = time.time()
        try:
            # static 包装时不带 recv，inst/class 包装时把 recv 拼回首位
            r = orig(*((a if recv is None else (recv,) + tuple(a))), **kw)
        except Exception as e:
            trace.emit(f"  !! {cls.__name__}.{name} 抛异常 "
                       f"{type(e).__name__}: {e}")
            raise
        if post is not None:
            try:
                post(trace, recv, a, kw, r, (time.time() - t0) * 1000)
            except Exception as e:
                trace.emit(f"  !! {cls.__name__}.{name} post 打印失败 {e}")
        return r

    if kind == "static":
        def wrapper(*a, **kw):
            return _run(None, a, kw)
        setattr(cls, name, staticmethod(wrapper))
    elif kind == "class":
        def wrapper(cls_, *a, **kw):  # noqa: N805
            return _run(cls_, a, kw)
        setattr(cls, name, classmethod(wrapper))
    else:
        def wrapper(self, *a, **kw):
            return _run(self, a, kw)
        setattr(cls, name, wrapper)


def install_hooks(trace: Trace) -> None:
    from agent.excel.parse_agent import ParseAgent
    from agent.excel.subagent.decompose_agent import DecomposeAgent
    from agent.excel.subagent.locator_agent import LocatorAgent

    # ── STAGE 1 路由分类 ──
    def _pre_route(tr, self, a, kw):
        tr.banner("STAGE 1 · LLM 路由分类  LocatorAgent._llm_classify_route")
        tr.emit(f"  in : {_clipv(a[0] if a else kw.get('text', ''), 300)}")

    def _post_route(tr, self, a, kw, r, dt):
        tr.emit(f"  out: {_j(r)}   ({dt:.0f}ms)")

    _wrap(trace, LocatorAgent, "_llm_classify_route",
          pre=_pre_route, post=_post_route)

    # ── STAGE 2 LLM 分段 ──
    def _pre_plan(tr, self, a, kw):
        tr.banner("STAGE 2 · LLM 分段 + 指代消解  ParseAgent._ai_plan_segments")
        tr.plan_llm0 = len(tr.llm)

    def _post_plan(tr, self, a, kw, r, dt):
        segs = r or []
        n_llm = len(tr.llm) - getattr(tr, "plan_llm0", 0)
        tr.emit(f"  切分 {len(segs)} 段，期间 LLM 调用 {n_llm} 次  ({dt:.0f}ms)")
        for i, s in enumerate(segs):
            txt = getattr(s, "text", s) if not isinstance(s, str) else s
            tr.emit(f"    seg[{i}] action={getattr(s, 'action', '?')!r}")
            tr.emit(f"            text = {_clipv(txt, 400)}")
        if not segs:
            if n_llm == 0:
                tr.emit("  !! 未发起任何 LLM 调用即返回空 → 检查 parser.client / "
                        "create_session 是否可用（否则分段层形同虚设）")
            else:
                tr.emit("  → LLM 判为单段（或解析失败），走 _parse_whole 整段单 prompt")
        else:
            tr.emit("  → 多段路径 _parse_segments：每段独立 locate + decompose_segment")

    _wrap(trace, ParseAgent, "_ai_plan_segments", pre=_pre_plan, post=_post_plan)

    # ── STAGE 3 每段 locate ──
    _loc_seq = {"n": 0}

    def _pre_locate(tr, self, a, kw):
        n = _loc_seq["n"]
        _loc_seq["n"] += 1
        tr.banner(f"STAGE 3 · 粗路由定位 #{n}  LocatorAgent.locate")
        tr.emit(f"  text: {_clipv(a[0] if a else '', 300)}")
        tr.emit(f"  route: {_j(kw.get('route'))}")

    def _post_locate(tr, self, a, kw, r, dt):
        tr.emit(f"  ({dt:.0f}ms)")
        if r is None:
            tr.emit("  !! locate 返回 None")
            return
        _dump_candidates(tr, r)
        if not (getattr(r, "candidates", None) or []):
            tr.emit("  !! 无候选表 → 该段直接返回空（后续 decompose 不会跑）")

    _wrap(trace, LocatorAgent, "locate", pre=_pre_locate, post=_post_locate)

    # ── STAGE 4a 段内候选裁剪 ──
    def _pre_prune(tr, self, a, kw):
        cands = a[1] if len(a) > 1 else kw.get("candidates")
        tr.emit(f"  4a 段内候选裁剪 in : {[getattr(c, 'stem', '?') for c in (cands or [])]}")

    def _post_prune(tr, self, a, kw, r, dt):
        tr.emit(f"  4a 段内候选裁剪 out: {[getattr(c, 'stem', '?') for c in (r or [])]}")

    _wrap(trace, DecomposeAgent, "_prune_segment_candidates",
          pre=_pre_prune, post=_post_prune)

    # ── STAGE 4b 单 prompt / 并发每表 ──
    def _pre_single(tr, self, a, kw):
        cands = a[1] if len(a) > 1 else kw.get("candidates")
        tr.emit(f"  4b 单 prompt 拆解: stems="
                f"{[getattr(c, 'stem', '?') for c in (cands or [])]} "
                f"timeout={a[3] if len(a) > 3 else kw.get('per_to')}")

    def _post_single(tr, self, a, kw, r, dt):
        intents = r[0] if isinstance(r, tuple) else r
        dropped = r[1] if isinstance(r, tuple) and len(r) > 1 else []
        tr.emit(f"  4b 单 prompt 产出: {len(intents or [])} 条 "
                f"dropped_stems={dropped}  ({dt:.0f}ms)")
        _dump_si(tr, intents or [], "SplitIntent")

    _wrap(trace, DecomposeAgent, "_decompose_single_prompt",
          pre=_pre_single, post=_post_single)

    def _pre_par(tr, self, a, kw):
        cands = a[1] if len(a) > 1 else kw.get("candidates")
        tr.emit(f"  4b 并发每表拆解: stems="
                f"{[getattr(c, 'stem', '?') for c in (cands or [])]}")

    def _post_par(tr, self, a, kw, r, dt):
        intents = r[0] if isinstance(r, tuple) else r
        dropped = r[1] if isinstance(r, tuple) and len(r) > 1 else []
        tr.emit(f"  4b 并发每表产出: {len(intents or [])} 条 "
                f"dropped={dropped}  ({dt:.0f}ms)")
        _dump_si(tr, intents or [], "SplitIntent")

    _wrap(trace, DecomposeAgent, "_decompose_parallel", pre=_pre_par, post=_post_par)

    # ── STAGE 4 段 decompose_segment / 整段 decompose ──
    def _pre_seg(tr, self, a, kw):
        tr.banner("STAGE 4 · 段 LLM 拆解  DecomposeAgent.decompose_segment")
        tr.emit(f"  seg: {_clipv(a[0] if a else kw.get('seg', ''), 300)}")

    def _post_seg(tr, self, a, kw, r, dt):
        tr.emit(f"  段产出 {len(r or [])} 条 SplitIntent  ({dt:.0f}ms)")
        if not r:
            tr.emit("  !! 该段产空 → 上层会重跑一次，仍空则该段漏覆盖")

    _wrap(trace, DecomposeAgent, "decompose_segment", pre=_pre_seg, post=_post_seg)

    def _pre_dec(tr, self, a, kw):
        tr.banner("STAGE 4 · 整段 LLM 拆解  DecomposeAgent.decompose（_parse_whole 路径）")
        tr.emit(f"  text: {_clipv(a[0] if a else '', 300)}")

    def _post_dec(tr, self, a, kw, r, dt):
        tr.emit(f"  产出 {len(r or [])} 条 SplitIntent  ({dt:.0f}ms)")
        _dump_si(tr, r or [], "SplitIntent")

    _wrap(trace, DecomposeAgent, "decompose", pre=_pre_dec, post=_post_dec)

    # ── STAGE 5a 全局 backfill / 表覆盖自检 ──
    def _pre_bf(tr, self, a, kw):
        intents = a[1] if len(a) > 1 else kw.get("intents")
        tr.emit(f"  5a 全局 backfill in : {len(intents or [])} 条")

    def _post_bf(tr, self, a, kw, r, dt):
        tr.emit(f"  5a 全局 backfill out: {len(r or [])} 条  ({dt:.0f}ms)")

    _wrap(trace, DecomposeAgent, "_backfill_missing", pre=_pre_bf, post=_post_bf)

    def _post_cov(tr, self, a, kw, r, dt):
        tr.emit(f"  5a 表覆盖自检 out: {len(r or [])} 条  ({dt:.0f}ms)")

    _wrap(trace, DecomposeAgent, "_llm_verify_table_coverage", post=_post_cov)

    # ── STAGE 5d 引用编译 ──
    def _pre_compile(tr, self, a, kw):
        tr.emit(f"  5d 引用编译 in : {len(a[0] or []) if a else 0} 条")

    def _post_compile(tr, self, a, kw, r, dt):
        tr.emit(f"  5d 引用编译 out: {len(r or [])} 条  ({dt:.0f}ms)")

    _wrap(trace, ParseAgent, "_compile_step1_references",
          pre=_pre_compile, post=_post_compile)

    # ── STAGE 5e 字段自检补漏 ──
    def _post_complete(tr, self, a, kw, r, dt):
        before = len(a[1] or []) if len(a) > 1 else 0
        tr.emit(f"  5e 字段自检补漏: {before} → {len(r or [])} 条  ({dt:.0f}ms)")

    _wrap(trace, DecomposeAgent, "_llm_complete_fields", post=_post_complete)

    # ── STAGE 5 组装尾部 ──
    def _pre_asm(tr, self, a, kw):
        tr.banner("STAGE 5 · 组装尾部  ParseAgent._assemble")
        tr.emit(f"  in : {len(a[0] or []) if a else 0} 条 SplitIntent "
                f"(do_backfill={kw.get('do_backfill')})")

    def _post_asm(tr, self, a, kw, r, dt):
        tr.emit(f"  out: {len(r or [])} 条 NLIntent  ({dt:.0f}ms)")

    _wrap(trace, ParseAgent, "_assemble", pre=_pre_asm, post=_post_asm)

    # ── 去重 / 空壳丢弃 ──
    def _post_dedup(tr, self, a, kw, r, dt):
        """打印被去重删掉的具体意图（定位"期望 N 条实际 M 条"的关键）。"""
        before = a[0] if a else []
        after = r or []
        if len(after) == len(before):
            return
        after_ids = {id(x) for x in after}
        gone = [x for x in (before or []) if id(x) not in after_ids]
        tr.emit(f"  5c 去重: {len(before)} → {len(after)} 条 "
                f"（删 {len(gone)} 条）")
        for g in gone:
            fields = getattr(g, "extras", None) or {}
            fields = fields.get("fields") if isinstance(fields, dict) else None
            tr.emit(f"      ✗ {getattr(g, 'action', '?')} "
                    f"{getattr(g, 'table_hint', '?')}/{getattr(g, 'sheet_hint', '') or '-'} "
                    f"produces={getattr(g, 'produces_label', None)!r}")
            tr.emit(f"          fields= {_fmt_fields(fields)}")
            tr.emit(f"          raw   = {_clipv(getattr(g, 'raw', ''), 80)}")

    _wrap(trace, ParseAgent, "_dedupe_nl_intents", post=_post_dedup)
    _wrap(trace, ParseAgent, "_dedupe_same_sheet_shadows", post=_post_dedup)

    def _post_orphan(tr, self, a, kw, r, dt):
        kept, dropped = (r if isinstance(r, tuple) and len(r) == 2 else (r, []))
        if dropped:
            tr.emit(f"  5c 丢弃孤立空壳 add: {len(dropped)} 条 "
                    f"{[(getattr(d, 'table_hint', '?'), getattr(d, 'sheet_hint', '?')) for d in dropped]}")

    _wrap(trace, ParseAgent, "_partition_orphan_empty_adds", post=_post_orphan)


# ── 主流程 ───────────────────────────────────────────────────

def _resolve_text(args) -> tuple[str, str]:
    """返回 (text, 来源描述)。"""
    if args.text:
        return args.text.strip(), "--text"
    path = Path(args.case_file)
    if not path.is_absolute():
        path = _ROOT / path
    if not path.exists():
        raise SystemExit(f"用例文件不存在: {path}")
    cases = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cases, list) or args.case >= len(cases):
        raise SystemExit(f"--case {args.case} 越界（共 {len(cases) if isinstance(cases, list) else 0} 条）")
    c = cases[args.case]
    return (c.get("input") or c.get("text") or "").strip(), f"{path.name}[{args.case}]"


def main() -> int:
    ap = argparse.ArgumentParser(description="Step1 全流程 trace（单输入）")
    ap.add_argument("--text", default="", help="直接指定输入文本")
    ap.add_argument("--case-file", default=str(_DEFAULT_CASES), help="用例 JSON 路径")
    ap.add_argument("--case", type=int, default=5, help="用例下标（0-based）")
    ap.add_argument("--backend", choices=["deepseek", "serve"], default="deepseek",
                    help="LLM 后端：deepseek 直连 / codemaker serve")
    ap.add_argument("--model", default="", help="覆盖模型名")
    ap.add_argument("--out", default=str(_REPORTS / "step1_trace_latest.txt"),
                    help="trace 输出文件")
    ap.add_argument("--dump-dir", default="", help="LLM prompt/响应落盘目录")
    ap.add_argument("--timeout", type=int, default=120,
                    help="单次 decompose LLM 超时（写入 CODEMAKER_DECOMPOSE_TIMEOUT）")
    args = ap.parse_args()

    text, src = _resolve_text(args)

    dump_dir = Path(args.dump_dir) if args.dump_dir else \
        Path(os.environ.get("CODEMAKER_DECOMPOSE_DUMP_DIR")
             or (_REPORTS / "step1_trace_dump"))
    dump_dir.mkdir(parents=True, exist_ok=True)
    try:
        for f in dump_dir.glob("llm_*"):
            f.unlink()
    except Exception:
        pass

    os.environ["CODEMAKER_DECOMPOSE_TIMEOUT"] = str(args.timeout)

    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = _ROOT / out_path
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fh = out_path.open("w", encoding="utf-8")
    trace = Trace(fh)

    # ── import agent（须在 env 设置之后）──
    import agent.excel.subagent.decompose_agent as _da_mod
    from agent.excel.cli.real_cli import RealCodeMakerCLI
    from agent.excel.core.pipeline import Step1ParseSubAgent, StepContext
    from tests.smoke_step1_deepseek import _SmokeParser

    # 观测开关（模块级变量，import 后按 args 修正）
    _da_mod._TRACE_ON = True
    _da_mod._DUMP_DIR = str(dump_dir)

    if args.backend == "deepseek":
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            raise SystemExit("缺 DEEPSEEK_API_KEY（.env 未配置或 --backend serve）")
        raw = _DeepSeekRaw(
            api_key=key,
            base_url=os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
            model=args.model or os.environ.get("DEEPSEEK_MODEL", "deepseek-chat"))
    else:
        from agent.codemaker_client import CodemakerClient
        raw = CodemakerClient()
    client = _TracedClient(trace, raw, dump_dir)

    cli = RealCodeMakerCLI(workspace=_ROOT / "resources")

    def _sink(*a):
        if len(a) >= 2:
            phase, detail = a[0], a[1]
            if str(phase).startswith("__json:"):
                detail = f"<json {str(phase)[7:]}> {_clipv(detail, 400)}"
            trace.emit(f"  · [{phase}] {_clipv(detail, 300)}")

    _model_shown = getattr(raw, "model", "") or getattr(
        getattr(raw, "cfg", None), "default_model", "")
    trace.banner("STAGE 0 · 环境与输入")
    trace.kv("backend", args.backend)
    trace.kv("model", _model_shown)
    trace.kv("resources", str(_ROOT / "resources"))
    trace.kv("dump_dir", str(dump_dir))
    trace.kv("out", str(out_path))
    trace.kv("source", src)
    trace.emit(f"  input ({len(text)} 字):")
    trace.emit("    " + text)

    install_hooks(trace)

    parser = _SmokeParser(client=client,
                          model=args.model or os.environ.get("DEEPSEEK_MODEL", ""))
    parser._cancel_event = None
    sub = Step1ParseSubAgent(parser=parser, thinking_sink=_sink, cli=cli)
    ctx = StepContext(session_id="step1-trace", user_text=text, thinking_sink=_sink)

    trace.banner("RUN · Step1ParseSubAgent.execute")
    t0 = time.time()
    try:
        result = sub.execute(ctx)
    except Exception as e:  # noqa: BLE001
        trace.emit(f"!! execute 抛异常 {type(e).__name__}: {e}")
        import traceback
        trace.emit(traceback.format_exc())
        fh.close()
        return 2
    total_ms = (time.time() - t0) * 1000

    trace.banner("STAGE 6 · Step1 SubAgent 后处理")
    trace.kv("ok", result.ok)
    trace.kv("dur_ms", int(total_ms))
    trace.emit("  errors:")
    if not result.errors:
        trace.emit("    （无）")
    for e in result.errors or []:
        trace.emit(f"    - [{e.error_type}] hard={e.is_hard} seg={e.segment_idx} "
                   f"{e.message}")
        if e.root_cause:
            trace.emit(f"        root_cause: {_clipv(e.root_cause, 300)}")
    trace.emit(f"  warnings: {result.warnings or '（无）'}")

    trace.emit("  metrics:")
    for k, v in sorted((result.metrics or {}).items()):
        trace.emit(f"    {k:<34}= {v}")

    art = result.artifacts or {}
    intents = art.get("intents") or []

    quality = art.get("step1_quality") or {}
    trace.emit(f"  quality: hard={quality.get('hard_count')} "
               f"issues={quality.get('issue_count')}")
    for iss in (quality.get("issues") or [])[:12]:
        trace.emit(f"    - {_j(iss)}")

    pg = art.get("plan_graph") or {}
    trace.emit(f"  plan_graph: nodes={pg.get('node_count')} "
               f"edges={pg.get('edge_count')} cycles={pg.get('cycle_count')}")
    for c in (pg.get("cycles") or [])[:6]:
        trace.emit(f"    - cycle {_j(c)}")

    sp = art.get("semantic_plan") or {}
    trace.emit(f"  semantic_plan: entities={sp.get('entity_count')} "
               f"relations={sp.get('relation_count')} "
               f"unresolved_refs={sp.get('unresolved_ref_count')}")

    audit = art.get("step1_audit") or {}
    trace.emit(f"  audit.metrics: {_j(audit.get('metrics') or {})}")
    for iss in (audit.get("issues") or [])[:12]:
        trace.emit(f"    - {_j(iss)}")

    trace.banner("STAGE 7 · 最终 NLIntent[]")
    _dump_nl(trace, intents)

    trace.banner("STAGE 8 · LLM 调用统计")
    trace.emit(f"  共 {len(trace.llm)} 次 LLM 调用")
    for c in trace.llm:
        trace.emit(f"    #{c['idx']:>2} {c['dur']:>6.1f}s "
                   f"prompt={c['prompt']:>6}B resp={c['resp']:>6}B ok={c['ok']}")
    trace.emit(f"  Step1 总耗时: {total_ms / 1000:.1f}s")
    trace.emit(f"  完整 trace: {out_path}")
    trace.emit(f"  LLM 原文落盘: {dump_dir}")
    fh.close()
    return 0 if intents else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Step1 全链路 trace 脚本：一个例子跑完 ParseAgent.parse，详细打印中间所有过程。

阶段输出：
  0. LLM 路由分类（_llm_classify_route）
  1. LLM 分段 + 指代消解（_ai_plan_segments）——新主路径
  2. 每段 LocatorAgent.locate（候选表/FK 边/列名信号）
  3. 每段 DecomposeAgent.decompose_segment（LLM 拆 SplitIntent）
  4. _assemble 尾部（去重/守卫/引用编译/补漏）
  5. 最终 NLIntent[]

运行（项目根 .env 需有 DEEPSEEK_API_KEY）：
    cd server && python tests/trace_step1_debug.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except Exception:
    pass

TEXT = "新增灵兽叫朱雀，灵兽model_id是1020，灵兽类型是神兽。然后新增一个奖励叫首充礼包，奖励包含它。"


# ── DeepSeek 直连 client（同 smoke_step1_deepseek.py） ──────────

@dataclass
class _SessionResult:
    ok: bool
    session_id: str = "trace"
    error: str = ""


@dataclass
class _PromptResult:
    ok: bool
    response_text: str = ""
    error: str = ""
    error_type: str = ""


class _DeepSeekClient:
    def __init__(self, *, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.prompts: list[str] = []
        self.responses: list[str] = []

    def create_session(self, **_kw) -> _SessionResult:
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
        start = min([i for i in [cleaned.find("["), cleaned.find("{")] if i >= 0] or [-1])
        if start >= 0:
            for end in range(len(cleaned), start, -1):
                frag = cleaned[start:end].strip()
                try:
                    return json.loads(frag)
                except json.JSONDecodeError:
                    continue
        return []

    def prompt(self, _sid: str, prompt: str, timeout: int = 120,
               model: str = "", **_kw) -> _PromptResult:
        self.prompts.append(prompt)
        payload = {
            "model": model or self.model,
            "temperature": 0,
            "stream": False,
            "max_tokens": 8192,
            "messages": [
                {"role": "system",
                 "content": "你是 Excel 配表解析引擎。只输出 JSON，不要 markdown。"},
                {"role": "user", "content": prompt},
            ],
            "thinking": {"type": "disabled"},
        }
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"}, method="POST")
        print(f"\n>>> [LLM 调用 #{len(self.prompts)}] prompt {len(prompt)} chars "
              f"(前 300):")
        print("    " + prompt[:300].replace("\n", "\n    "))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            self.responses.append(text)
            print(f"<<< [LLM 响应 #{len(self.responses)}] {len(text)} chars:")
            print("    " + text[:600].replace("\n", "\n    "))
            return _PromptResult(ok=True, response_text=text)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.responses.append(f"ERROR http_{exc.code}: {detail}")
            print(f"<<< [LLM 错误] http_{exc.code}: {detail[:300]}")
            return _PromptResult(ok=False, error=detail, error_type=f"http_{exc.code}")
        except Exception as exc:  # noqa: BLE001
            self.responses.append(f"ERROR {type(exc).__name__}: {exc}")
            print(f"<<< [LLM 错误] {type(exc).__name__}: {exc}")
            return _PromptResult(ok=False, error=str(exc), error_type=type(exc).__name__)


class _Parser:
    def __init__(self, client):
        self.client = client
        self.model = ""
        self.directory = ""
        self._session_id = ""


# ── thinking sink：全量收集打印 ────────────────────────────────

class ThinkingSink:
    def __init__(self):
        self.events: list[tuple] = []

    def __call__(self, *args):
        self.events.append(args)
        stage = args[0] if args else ""
        msg = args[1] if len(args) > 1 else ""
        print(f"  [thinking|{stage}] {msg}")


def _banner(title: str) -> None:
    print(f"\n{'=' * 70}\n== {title}\n{'=' * 70}")


def main() -> int:
    key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not key:
        print("缺 DEEPSEEK_API_KEY", file=sys.stderr)
        return 2
    base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
    client = _DeepSeekClient(api_key=key, base_url=base_url, model=model)

    from agent.excel.cli_interface import StubCodeMakerCLI
    from agent.excel.parse_agent import ParseAgent
    from agent.excel.subagent.decompose_agent import DecomposeAgent
    from agent.excel.subagent.locator_agent import LocatorAgent

    cli = StubCodeMakerCLI(workspace=RESOURCES)
    parser = _Parser(client)
    sink = ThinkingSink()
    locator = LocatorAgent(parser=parser, thinking_sink=sink, cli=cli)
    decomposer = DecomposeAgent(parser=parser, thinking_sink=sink, cli=cli)
    pa = ParseAgent(parser=parser, thinking_sink=sink, cli=cli,
                    locator_agent=locator, decompose_agent=decomposer)

    _banner("输入")
    print(TEXT)

    _banner("Step1 parse 开始（thinking 全量跟踪）")
    intents = pa.parse(TEXT)

    _banner("阶段产物：segments（LLM 分段 + 指代消解）")
    for i, seg in enumerate(getattr(pa, "_last_segments", [])):
        print(f"  seg[{i}] action={getattr(seg, 'action', '?')!r} "
              f"text={getattr(seg, 'text', seg)!r}")

    _banner("阶段产物：locator_results（每段候选表 + FK 边 + 列名信号）")
    for i, lr in enumerate(getattr(pa, "_last_locator_results", [])):
        cands = [(c.stem, c.sheet, round(getattr(c, "confidence", 0), 2))
                 for c in getattr(lr, "candidates", [])]
        print(f"  locator[{i}] candidates={cands}")
        print(f"           fk_edges={[(e.from_table, e.from_column, e.to_table) "
              f"for e in getattr(lr, 'fk_edges', [])]}")
        sig = getattr(lr, "column_signal", None)
        if sig is not None:
            print(f"           column_signal.has_signal={getattr(sig, 'has_signal', None)} "
                  f"terms={getattr(sig, 'extracted_terms', None)} "
                  f"stems={getattr(sig, 'candidate_stems', None)}")

    _banner("最终 NLIntent[]")
    print(f"  共 {len(intents)} 条")
    for i, it in enumerate(intents):
        print(f"\n  intent[{i}]:")
        print(f"    action      = {it.action!r}")
        print(f"    table/sheet = {it.table_hint!r} / {it.sheet_hint!r}")
        print(f"    raw         = {getattr(it, 'raw', '')!r}")
        print(f"    produces    = {getattr(it, 'produces_label', None)!r}")
        print(f"    consumes    = {getattr(it, 'consumes_labels', None)!r}")
        extras = getattr(it, "extras", {}) or {}
        print(f"    source      = {extras.get('source')!r}")
        fields = extras.get("fields")
        print(f"    fields      = {json.dumps(fields, ensure_ascii=False) if fields else None}")
        for k in ("produces", "consumes", "extracted_columns"):
            if k in extras:
                print(f"    extras.{k} = {json.dumps(extras[k], ensure_ascii=False)}")

    _banner("LLM 调用统计")
    print(f"  共 {len(client.prompts)} 次 LLM 调用")
    return 0 if intents else 1


if __name__ == "__main__":
    sys.exit(main())

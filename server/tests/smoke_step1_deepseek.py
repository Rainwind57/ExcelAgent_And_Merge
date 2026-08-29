from __future__ import annotations

import argparse
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

from agent.excel.cli_interface import StubCodeMakerCLI
from agent.excel.parse_agent import ParseAgent
from agent.excel.subagent.decompose_agent import DecomposeAgent
from agent.excel.subagent.locator_agent import CandidateTable, FKEdge, LocatorResult


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parents[2]
RESOURCES = ROOT / "resources"


@dataclass
class _SessionResult:
    ok: bool
    session_id: str = "step1-smoke"
    error: str = ""


@dataclass
class _PromptResult:
    ok: bool
    response_text: str = ""
    error: str = ""
    error_type: str = ""


class _OfflineClient:
    def __init__(self, response_text: str):
        self.response_text = response_text
        self.prompts: list[str] = []
        self.responses: list[str] = []

    def create_session(self, **_kwargs) -> _SessionResult:
        return _SessionResult(ok=True)

    def prompt(self, _session_id: str, prompt: str, **_kwargs) -> _PromptResult:
        self.prompts.append(prompt)
        self.responses.append(self.response_text)
        return _PromptResult(ok=True, response_text=self.response_text)


class _DeepSeekClient:
    def __init__(self, *, api_key: str, base_url: str, model: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.prompts: list[str] = []
        self.responses: list[str] = []

    def create_session(self, **_kwargs) -> _SessionResult:
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

    def prompt(self, _session_id: str, prompt: str, timeout: int = 90,
               model: str = "", **_kwargs) -> _PromptResult:
        self.prompts.append(prompt)
        max_tokens_raw = os.environ.get("DEEPSEEK_MAX_TOKENS", "8192").strip()
        try:
            max_tokens = max(256, int(max_tokens_raw))
        except ValueError:
            max_tokens = 8192
        payload = {
            "model": model or self.model,
            "temperature": 0,
            "stream": False,
            "max_tokens": max_tokens,
            "messages": [
                {
                    "role": "system",
                    "content": (
                        "You are an Excel config decomposition engine. "
                        "Return only a JSON array. Do not add markdown."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
        }
        if os.environ.get("DEEPSEEK_JSON_OBJECT", "").strip() == "1":
            payload["response_format"] = {"type": "json_object"}
            payload["messages"][0]["content"] = (
                "You are an Excel config decomposition engine. "
                "Return only a JSON object like {\"intents\": [...]}. Do not add markdown."
            )
        thinking = os.environ.get("DEEPSEEK_THINKING", "disabled").strip().lower()
        if thinking in {"enabled", "disabled"}:
            payload["thinking"] = {"type": thinking}
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            text = data["choices"][0]["message"]["content"]
            self.responses.append(text)
            return _PromptResult(ok=True, response_text=text)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            self.responses.append(f"ERROR http_{exc.code}: {detail}")
            return _PromptResult(ok=False, error=detail, error_type=f"http_{exc.code}")
        except Exception as exc:  # noqa: BLE001
            self.responses.append(f"ERROR {type(exc).__name__}: {exc}")
            return _PromptResult(ok=False, error=str(exc), error_type=type(exc).__name__)


class _SmokeParser:
    def __init__(self, client, model: str = ""):
        self.client = client
        self.model = model
        self.directory = ""
        self._session_id = ""
        self._cancel_event = None


class _FixedLocator:
    def __init__(self, result: LocatorResult):
        self.result = result
        self.called_with: list[str] = []

    def locate(self, text: str) -> LocatorResult:
        self.called_with.append(text)
        return self.result


def _pet_evolve_case() -> tuple[str, LocatorResult, str, dict[str, Any]]:
    text = (
        "新增灵兽饕餮，灵兽model_id是1020，灵兽类型是神兽，"
        "并配置一条进化链：饕餮进化成饕餮王，进化等级是1。"
    )
    locator = LocatorResult(
        candidates=[
            CandidateTable("pet", "Pet", 1.0),
            CandidateTable("pet_evolve", "PetEvolveData", 0.9),
        ],
        fk_edges=[
            FKEdge("pet_evolve", "PetEvolveData", "宠物id", "pet", "Pet", "灵兽id"),
            FKEdge("pet_evolve", "PetEvolveData", "进化后的灵兽ID", "pet", "Pet", "灵兽id"),
        ],
    )
    offline_json = json.dumps([
        {
            "table": "pet",
            "sheet": "Pet",
            "action": "add",
            "fields": {"名称": "饕餮", "灵兽model_id": "1020", "灵兽类型": "神兽"},
            "produces": "new_pet_id",
            "consumes": {},
        },
        {
            "table": "pet_evolve",
            "sheet": "PetEvolveData",
            "action": "add",
            "fields": {"宠物id": "", "进化后的灵兽ID": "", "进化等级": "1"},
            "produces": "",
            "consumes": {"宠物id": "new_pet_id", "进化后的灵兽ID": "new_pet_id"},
        },
    ], ensure_ascii=False)
    expected = {
        "min_intents": 2,
        "tables": {("pet", "Pet"), ("pet_evolve", "PetEvolveData")},
        "must_have_consumes": True,
    }
    return text, locator, offline_json, expected


def _nl_to_dict(intent) -> dict[str, Any]:
    data = intent.to_checkpoint_dict()
    extras = dict(data.get("extras") or {})
    extras.pop("extracted_columns_signal", None)
    data["extras"] = extras
    return data


def _validate_step1(intents, expected: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    allowed = {"add", "set", "delete", "get", "col"}
    producers = {it.produces_label for it in intents if getattr(it, "produces_label", None)}
    if len(intents) < int(expected.get("min_intents") or 1):
        errors.append(
            f"expected at least {expected.get('min_intents')} intents, got {len(intents)}")
    actual_tables = {
        (getattr(it, "table_hint", None), getattr(it, "sheet_hint", None))
        for it in intents
    }
    for table in expected.get("tables") or set():
        if table not in actual_tables:
            errors.append(f"missing expected table/sheet {table[0]}/{table[1]}")
    if expected.get("must_have_consumes"):
        has_consumes = any(getattr(it, "consumes_labels", None) for it in intents)
        if not has_consumes:
            errors.append("expected at least one consumes label, got none")
    for idx, it in enumerate(intents):
        prefix = f"intent[{idx}]"
        if it.action not in allowed:
            errors.append(f"{prefix}: bad action {it.action!r}")
        if not it.table_hint:
            errors.append(f"{prefix}: missing table_hint")
        fields = (it.extras or {}).get("fields")
        if it.action in {"add", "set"} and not isinstance(fields, dict):
            errors.append(f"{prefix}: extras.fields must be dict")
            continue
        if isinstance(fields, dict):
            for key, value in fields.items():
                if isinstance(key, int) or (isinstance(key, str) and key.strip().isdigit()):
                    errors.append(f"{prefix}: numeric field key {key!r}")
                if isinstance(value, (dict, list)):
                    errors.append(f"{prefix}: nested field value at {key!r}")
        for label in getattr(it, "consumes_labels", []) or []:
            if label not in producers:
                errors.append(f"{prefix}: consumes unknown label {label!r}")
    return errors


def run(mode: str) -> int:
    text, locator, offline_json, expected = _pet_evolve_case()
    os.environ.setdefault("CODEMAKER_DECOMPOSE_SINGLE_RETRY", "0")
    if mode == "offline":
        client = _OfflineClient(offline_json)
        model = "offline"
    else:
        key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
        if not key:
            print("Set DEEPSEEK_API_KEY before running --mode deepseek", file=sys.stderr)
            return 2
        base_url = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com").strip()
        model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash").strip()
        client = _DeepSeekClient(api_key=key, base_url=base_url, model=model)

    cli = StubCodeMakerCLI(workspace=RESOURCES)
    parser = _SmokeParser(client=client, model=model)
    decomposer = DecomposeAgent(parser=parser, thinking_sink=lambda *_args: None, cli=cli)
    parse_agent = ParseAgent(
        parser=parser,
        thinking_sink=lambda *_args: None,
        cli=cli,
        locator_agent=_FixedLocator(locator),
        decompose_agent=decomposer,
    )
    intents = parse_agent.parse(text)
    errors = _validate_step1(intents, expected)
    payload = {
        "mode": mode,
        "model": model,
        "input": text,
        "prompt_count": len(getattr(client, "prompts", [])),
        "prompt_has_fill_rules": any("填表规则" in p or "fill" in p.lower() for p in getattr(client, "prompts", [])),
        "raw_responses": [
            (r if len(r) <= 4000 else r[:4000] + f"... <+{len(r) - 4000} chars>")
            for r in getattr(client, "responses", [])
        ],
        "intent_count": len(intents),
        "errors": errors,
        "intents": [_nl_to_dict(it) for it in intents],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if errors or not intents else 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Smoke test Step1 decompose_agent + parse_agent.")
    ap.add_argument("--mode", choices=["offline", "deepseek"], default="offline")
    args = ap.parse_args()
    return run(args.mode)


if __name__ == "__main__":
    raise SystemExit(main())
